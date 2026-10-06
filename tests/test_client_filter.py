"""P1-14: coding-agent sessions (Claude Code) are not archived.

Claude Code loads the user's claude.ai connectors into every coding session and
ThreadVault's instructions then archived those sessions. The server identifies
the client from the MCP handshake, the per-request `_meta` clientInfo
(protocol 2026-07-28+, what Claude Code 2.1 sends) or the User-Agent, withholds
the write tools from skipped clients and turns a forced call into a no-op.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest
import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from thread_save.tools.clients import (
    NOT_ARCHIVED_RESULT,
    client_identity,
    is_skipped_client,
    skipped_clients,
)

CLAUDE_CODE_UA = "claude-code/2.1.289 (claude-vscode, agent-sdk/0.3.289)"


# ── identity and matching ─────────────────────────────────────────────────

def test_default_skip_list_and_override(monkeypatch):
    monkeypatch.delenv("THREADVAULT_SKIP_CLIENTS", raising=False)
    assert skipped_clients() == ("claude-code",)
    monkeypatch.setenv("THREADVAULT_SKIP_CLIENTS", "none")
    assert skipped_clients() == ()
    monkeypatch.setenv("THREADVAULT_SKIP_CLIENTS", "Claude-Code, cursor")
    assert skipped_clients() == ("claude-code", "cursor")


def test_matching():
    skip = ("claude-code",)
    assert is_skipped_client("claude-code", "", skip)
    assert is_skipped_client("", CLAUDE_CODE_UA, skip)
    assert not is_skipped_client("claude-ai", "Claude-User", skip)
    assert not is_skipped_client("", "", skip)
    assert not is_skipped_client("claude-code", CLAUDE_CODE_UA, ())


def _ctx(handshake_name=None, meta_name=None, ua=None):
    params = None
    if handshake_name:
        params = SimpleNamespace(client_info=SimpleNamespace(name=handshake_name, version="1"))
    meta = {"io.modelcontextprotocol/clientInfo": {"name": meta_name, "version": "2.1.289"}} if meta_name else {}
    request = SimpleNamespace(headers={"user-agent": ua}) if ua else None
    return SimpleNamespace(session=SimpleNamespace(client_params=params), meta=meta, request=request)


def test_identity_from_handshake_meta_and_user_agent():
    assert client_identity(_ctx(handshake_name="claude-ai")) == ("claude-ai", "1", "")
    # 2026-07-28+: no handshake, clientInfo rides in every request's _meta
    assert client_identity(_ctx(meta_name="claude-code")) == ("claude-code", "2.1.289", "")
    assert client_identity(_ctx(ua=CLAUDE_CODE_UA))[2] == CLAUDE_CODE_UA


# ── stdio server end to end ───────────────────────────────────────────────

async def _session(vault: Path, client_name: str, env_extra: dict | None = None):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "thread_save.server"],
        env={**os.environ, "THREAD_SAVE_VAULT_ROOT": str(vault), **(env_extra or {})},
    )
    out = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, client_info=types.Implementation(name=client_name, version="1")) as s:
            await s.initialize()
            out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)
            r = await s.call_tool("vault_local_log_turn", {"user_message": "hello", "reply": "hi"})
            out["call"] = json.loads(r.content[0].text)
    out["pages"] = sorted(p.name for p in vault.rglob("*_p[0-9][0-9].md"))
    return out


@pytest.fixture
def vault():
    path = Path(tempfile.mkdtemp(prefix="tv_clients_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_claude_code_session_is_not_archived(vault):
    out = asyncio.run(_session(vault, "claude-code"))
    assert out["tools"] == ["vault_local_find", "vault_local_stats"]
    assert out["call"] == NOT_ARCHIVED_RESULT
    assert out["pages"] == []


def test_chat_client_is_archived(vault):
    out = asyncio.run(_session(vault, "claude-ai"))
    assert "vault_local_log_turn" in out["tools"]
    assert out["call"]["ok"] is True and "thread_id" in out["call"]
    assert len(out["pages"]) == 1


def test_skip_list_can_be_turned_off(vault):
    out = asyncio.run(_session(vault, "claude-code", {"THREADVAULT_SKIP_CLIENTS": "none"}))
    assert "vault_local_log_turn" in out["tools"]
    assert len(out["pages"]) == 1


# ── deployed HTTP server end to end (file backend, as on Render) ──────────

def _sse_json(resp) -> dict:
    line = next(l for l in resp.text.splitlines() if l.startswith("data:"))
    return json.loads(line[len("data:"):].strip())


async def _http_session(vault: Path, client_name: str, user_agent: str | None = None) -> dict:
    import httpx
    from thread_save.config import VaultConfig
    from thread_save.web.app import create_app

    app = create_app(config=VaultConfig(vault_root=vault, default_account="http-user"), enforce_auth=False)
    headers = {"Accept": "application/json, text/event-stream"}
    if user_agent:
        headers["User-Agent"] = user_agent
    out = {}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000", headers=headers) as c:
            init = await c.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": client_name, "version": "1"}},
            })
            session = {"mcp-session-id": init.headers["mcp-session-id"], "X-Account-ID": "http-user"}
            await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
            listed = _sse_json(await c.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=session
            ))
            out["tools"] = sorted(t["name"] for t in listed["result"]["tools"])
            called = _sse_json(await c.post("/mcp", json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "vault_log_turn", "arguments": {"user_message": "hello", "reply": "hi"}},
            }, headers=session))
            out["call"] = json.loads(called["result"]["content"][0]["text"])
    out["pages"] = sorted(p.name for p in vault.rglob("*_p[0-9][0-9].md"))
    return out


@pytest.fixture
def http_env(monkeypatch):
    monkeypatch.setenv("THREADVAULT_STORAGE_BACKEND", "file")
    monkeypatch.setenv("THREADVAULT_GH_REPO", "")  # never sync test data to the real archive
    monkeypatch.delenv("THREADVAULT_SKIP_CLIENTS", raising=False)


def test_http_claude_code_is_not_archived(vault, http_env):
    out = asyncio.run(_http_session(vault, "claude-code", CLAUDE_CODE_UA))
    assert "vault_log_turn" not in out["tools"] and "vault_backfill" not in out["tools"]
    assert "vault_find" in out["tools"]
    assert out["call"] == NOT_ARCHIVED_RESULT
    assert out["pages"] == []


def test_http_claude_code_known_by_user_agent_only(vault, http_env):
    # e.g. a proxy that rewrites clientInfo but passes the User-Agent through
    out = asyncio.run(_http_session(vault, "mcp-proxy", CLAUDE_CODE_UA))
    assert "vault_log_turn" not in out["tools"]
    assert out["call"] == NOT_ARCHIVED_RESULT
    assert out["pages"] == []


def test_http_chat_client_is_archived(vault, http_env):
    out = asyncio.run(_http_session(vault, "claude-ai", "Claude-User"))
    assert "vault_log_turn" in out["tools"]
    assert out["call"]["ok"] is True and "thread_id" in out["call"]
    assert len(out["pages"]) == 1
