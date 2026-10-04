"""Test suite for Streamable HTTP transport and Web API (Milestone X3).

Verifies:
- /health endpoint status & database check
- Origin validation (allow claude.ai/claude.com/localhost, reject unauthorized origins)
- Max body size enforcement (413 Payload Too Large)
- Rate limiting enforcement (429 Too Many Requests)
- Streamable HTTP protocol over /mcp:
  - initialize handshake
  - tools/list returning vault_* tools with honest descriptions
  - tools/call executing vault_save_turn and vault_find against PgStore
"""

import asyncio
import json
import os
from pathlib import Path
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.web.app import create_app
from thread_save.web.middleware import RateLimiter

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
    yield store
    await store.close()


@pytest.fixture
def app(pg_store):
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="http-user")
    svc = TurnService(pg_store, config=cfg)
    return create_app(pg_store=pg_store, service=svc, config=cfg)


@pytest.mark.asyncio
async def test_health_endpoint(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"
            assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_origin_validation(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 1. Allowed origin https://claude.ai
            r1 = await client.get("/health", headers={"Origin": "https://claude.ai"})
            assert r1.status_code == 200

            # 2. Allowed origin https://claude.com
            r2 = await client.get("/health", headers={"Origin": "https://claude.com"})
            assert r2.status_code == 200

            # 3. Allowed localhost origin
            r3 = await client.get("/health", headers={"Origin": "http://localhost:5173"})
            assert r3.status_code == 200

            # 4. Forbidden origin
            r4 = await client.get("/health", headers={"Origin": "https://attacker-site.com"})
            assert r4.status_code == 403
            assert "not allowed" in r4.json()["detail"]


@pytest.mark.asyncio
async def test_body_size_limit(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            oversized_content = b"X" * (3 * 1024 * 1024)  # 3 MB > 2 MB limit
            resp = await client.post(
                "/health",
                content=oversized_content,
                headers={"Content-Length": str(len(oversized_content))},
            )
            assert resp.status_code == 413
            assert "too large" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rate_limiting(pg_store):
    limiter = RateLimiter(max_requests=3, window_seconds=60.0)
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="rate-user")
    svc = TurnService(pg_store, config=cfg)
    rate_app = create_app(pg_store=pg_store, service=svc, config=cfg, rate_limiter=limiter)

    async with rate_app.router.lifespan_context(rate_app):
        transport = httpx.ASGITransport(app=rate_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            headers = {"X-Account-ID": "test-rate-account"}
            r1 = await client.get("/health", headers=headers)
            r2 = await client.get("/health", headers=headers)
            r3 = await client.get("/health", headers=headers)
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r3.status_code == 200

            # 4th request exceeds rate limit
            r4 = await client.get("/health", headers=headers)
            assert r4.status_code == 429
            assert r4.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_streamable_http_mcp_flow(app, pg_store):
    """Complete Streamable HTTP handshake, tool listing, and tool execution."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 1. Initialize
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-harness", "version": "1.0"},
                },
            }
            init_resp = await client.post(
                "/mcp",
                json=init_req,
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert init_resp.status_code == 200
            session_id = init_resp.headers.get("mcp-session-id")
            assert session_id is not None, "Missing mcp-session-id in init response"

            # Parse event-stream / jsonrpc response
            lines = init_resp.text.strip().splitlines()
            data_line = next(line for line in lines if line.startswith("data:"))
            init_data = json.loads(data_line[len("data:"):].strip())
            assert init_data["result"]["serverInfo"]["name"] == "threadvault-remote"

            common_headers = {
                "mcp-session-id": session_id,
                "Accept": "application/json, text/event-stream",
                "X-Account-ID": "http-user",
            }

            # 2. List tools
            tools_req = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
            tools_resp = await client.post("/mcp", json=tools_req, headers=common_headers)
            assert tools_resp.status_code == 200
            lines = tools_resp.text.strip().splitlines()
            data_line = next(line for line in lines if line.startswith("data:"))
            tools_data = json.loads(data_line[len("data:"):].strip())

            tool_names = [t["name"] for t in tools_data["result"]["tools"]]
            assert "vault_save_turn" in tool_names
            assert "vault_backfill" in tool_names
            assert "vault_find" in tool_names

            # Verify honest remote description (§2 S8)
            save_desc = next(t["description"] for t in tools_data["result"]["tools"] if t["name"] == "vault_save_turn")
            assert "ThreadVault account" in save_desc

            # 3. Call tool: vault_save_turn
            call_req = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "vault_save_turn",
                    "arguments": {
                        "user_query": "Hello via HTTP Streamable transport!",
                        "title_hint": "HTTP Test Thread",
                    },
                },
            }
            call_resp = await client.post("/mcp", json=call_req, headers=common_headers)
            assert call_resp.status_code == 200
            lines = call_resp.text.strip().splitlines()
            data_line = next(line for line in lines if line.startswith("data:"))
            call_data = json.loads(data_line[len("data:"):].strip())

            content = call_data["result"]["content"][0]["text"]
            result_dict = json.loads(content)
            assert result_dict["ok"] is True
            assert result_dict["n"] == 1
            tid = result_dict["thread_id"]

            # Verify turn was written to Postgres
            stats = await pg_store.stats("http-user", tid)
            assert stats is not None
            assert stats.total_turns == 1

            # 4. Call tool: vault_find
            find_req = {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "vault_find",
                    "arguments": {
                        "query": "HTTP",
                    },
                },
            }
            find_resp = await client.post("/mcp", json=find_req, headers=common_headers)
            assert find_resp.status_code == 200
            lines = find_resp.text.strip().splitlines()
            data_line = next(line for line in lines if line.startswith("data:"))
            find_data = json.loads(data_line[len("data:"):].strip())
            find_dict = json.loads(find_data["result"]["content"][0]["text"])
            assert find_dict["ok"] is True
            assert find_dict["count"] >= 1
            assert any(t["thread_id"] == tid for t in find_dict["threads"])
