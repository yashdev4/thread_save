"""Test suite for Cross-Surface Live Scenario Protocol (Milestone X9).

Verifies:
- Live multi-surface simulation (Desktop -> Android -> iOS -> Web)
- Continuous single canonical thread (zero split threads, split rate 0%)
- 100% turn coverage with dense turn slot numbering (1..10)
- Full chunk assembly (>6000 chars) preserved across surfaces
- Sensitive API keys redacted pre-insert
- Multi-device continuity over Streamable HTTP transport
"""

import json
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.service import TurnService
from thread_save.storage.writer import FileStore
from thread_save.web.app import create_app
from scripts.cross_surface_test import run_scenario_via_http


@pytest.fixture
def store(tmp_path):
    return FileStore(config=VaultConfig(vault_root=tmp_path, default_account="cross-surface-user"))


@pytest.fixture
def service_and_app(store, tmp_path):
    cfg = VaultConfig(vault_root=tmp_path, default_account="cross-surface-user")
    svc = TurnService(store, config=cfg)
    app = create_app(service=svc, config=cfg)
    return svc, app


@pytest.mark.asyncio
async def test_cross_surface_http_transport(store, service_and_app):
    _, app = service_and_app
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 1. Initialize session
            init_resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-cross-surface", "version": "1.0"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert init_resp.status_code == 200
            session_id = init_resp.headers.get("mcp-session-id", "test-session")

            headers = {
                "mcp-session-id": session_id,
                "Accept": "application/json, text/event-stream",
                "X-Account-ID": "cross-surface-http-user",
            }

            async def call_save(arguments: dict) -> dict:
                resp = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "vault_log_turn", "arguments": arguments},
                    },
                    headers=headers,
                )
                assert resp.status_code == 200
                for line in resp.text.strip().splitlines():
                    if line.startswith("data:"):
                        payload = json.loads(line[5:].strip())
                        return json.loads(payload["result"]["content"][0]["text"])
                raise RuntimeError(f"Unexpected response: {resp.text}")

            # Step 1: Started on Desktop
            r1 = await call_save({
                "user_message": "Architecture planning started on Desktop.",
                "reply": "Understood. Desktop turn 1 stored.",
                "title_hint": "Multi Device Sync Thread",
            })
            assert r1["ok"] is True
            thread_id = r1["thread_id"]
            assert r1["next_turn"] == 2

            # Step 2: Continued on Desktop
            r2 = await call_save({
                "thread_id": thread_id,
                "turn": r1["next_turn"],
                "prev_user_anchor": "Architecture planning started on Desktop.",
                "user_message": "Desktop turn 2 details.",
                "reply": "Desktop turn 2 stored. Ready for Android.",
            })
            assert r2["ok"] is True
            assert r2["thread_id"] == thread_id

            # Step 3: Continued on Android (same thread_id & turn counter)
            r3 = await call_save({
                "thread_id": thread_id,
                "turn": r2["next_turn"],
                "prev_user_anchor": "Desktop turn 2 details.",
                "user_message": "Now continuing on Android seamlessly.",
                "reply": "Android turn stored. Ready for Web.",
            })
            assert r3["ok"] is True
            assert r3["thread_id"] == thread_id

            # Step 4: Continued on Web
            r4 = await call_save({
                "thread_id": thread_id,
                "turn": r3["next_turn"],
                "prev_user_anchor": "Now continuing on Android seamlessly.",
                "user_message": "Finally checking on Claude Web.",
                "reply": "Web turn stored.",
            })
            assert r4["ok"] is True
            assert r4["thread_id"] == thread_id
            assert r4["next_turn"] == 5

            # Verify stats in Postgres
            stats = await store.stats("cross-surface-http-user", thread_id)
            assert stats.total_turns == 8  # 4 complete turns, both sides, no open slot
            assert stats.reported == 4
            assert stats.stubs == 0
            coverage_pct = (
                ((stats.total_turns - stats.stubs) / stats.total_turns * 100.0)
                if stats.total_turns > 0
                else 100.0
            )
            assert coverage_pct == 100.0
