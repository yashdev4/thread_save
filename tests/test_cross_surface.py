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
import os
from pathlib import Path
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.web.app import create_app
from scripts.cross_surface_test import run_scenario_via_service, run_scenario_via_http

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def clean_pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
    yield store
    await store.close()


@pytest.fixture
def service_and_app(clean_pg_store):
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="cross-surface-user")
    svc = TurnService(clean_pg_store, config=cfg)
    app = create_app(pg_store=clean_pg_store, service=svc, config=cfg)
    return svc, app


@pytest.mark.asyncio
async def test_cross_surface_full_cycle_service(clean_pg_store, service_and_app):
    svc, _ = service_and_app
    res = await run_scenario_via_service(svc, account="cross-surface-user", verbose=False)

    assert res.success is True
    assert res.total_turns >= 10
    assert res.split_count == 0
    assert res.coverage_pct == 100.0
    assert res.redaction_verified is True
    assert res.chunk_assembly_verified is True
    assert "Desktop" in res.surfaces_tested
    assert "Android" in res.surfaces_tested
    assert "iOS" in res.surfaces_tested
    assert "Web" in res.surfaces_tested

    # Verify database state directly
    turns = await clean_pg_store.get_turns("cross-surface-user", res.thread_id)
    assert len(turns) >= 10

    # Verify no plaintext secret leaked
    for t in turns:
        assert "ak_live_99887766554433221100aabbccdd" not in t.body
        assert "AKIAIOSFODNN7EXAMPLE" not in t.body


@pytest.mark.asyncio
async def test_cross_surface_http_transport(clean_pg_store, service_and_app):
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
                        "params": {"name": "vault_save_turn", "arguments": arguments},
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
                "user_query": "Architecture planning started on Desktop.",
                "title_hint": "Multi Device Sync Thread",
            })
            assert r1["ok"] is True
            thread_id = r1["thread_id"]

            # Step 2: Continued on Desktop
            r2 = await call_save({
                "thread_id": thread_id,
                "prev_user_anchor": "Architecture planning started on Desktop.",
                "prev_response": "Understood. Desktop turn 1 stored.",
                "user_query": "Desktop turn 2 details.",
            })
            assert r2["ok"] is True
            assert r2["thread_id"] == thread_id

            # Step 3: Continued on Android (same thread_id & anchor)
            r3 = await call_save({
                "thread_id": thread_id,
                "prev_user_anchor": "Desktop turn 2 details.",
                "prev_response": "Desktop turn 2 stored. Ready for Android.",
                "user_query": "Now continuing on Android seamlessly.",
            })
            assert r3["ok"] is True
            assert r3["thread_id"] == thread_id

            # Step 4: Continued on Web
            r4 = await call_save({
                "thread_id": thread_id,
                "prev_user_anchor": "Now continuing on Android seamlessly.",
                "prev_response": "Android turn stored. Ready for Web.",
                "user_query": "Finally checking on Claude Web.",
            })
            assert r4["ok"] is True
            assert r4["thread_id"] == thread_id

            # Verify stats in Postgres
            stats = await clean_pg_store.stats("cross-surface-http-user", thread_id)
            assert stats.total_turns == 7  # 4 user turns + 3 assistant turns
            assert stats.stubs == 0
            coverage_pct = (
                ((stats.total_turns - stats.stubs) / stats.total_turns * 100.0)
                if stats.total_turns > 0
                else 100.0
            )
            assert coverage_pct == 100.0
