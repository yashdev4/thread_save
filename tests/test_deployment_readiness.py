"""Verification suite for Milestone X8 (Deployment Readiness, Latency Budget, & Health).

Covers:
- /health probe behavior under healthy and degraded database states
- Latency budget: p95 server processing latency < 300 ms across HTTP saves
- Deployment artifact verification (Dockerfile, fly.toml with always-on non-sleeping instance)
"""

import json
import os
from pathlib import Path
import time
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.web.app import create_app

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


@pytest.mark.asyncio
async def test_x8_health_probe_healthy_and_degraded(pg_store):
    """Verify /health returns healthy with connected DB, and degraded when disconnected."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="deploy-account")
    svc = TurnService(pg_store, config=cfg)
    app = create_app(pg_store=pg_store, service=svc, config=cfg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 1. Healthy state
            resp_healthy = await client.get("/health")
            assert resp_healthy.status_code == 200
            data = resp_healthy.json()
            assert data["status"] == "healthy"
            assert data["database"] == "connected"

    # 2. Degraded state with disconnected store
    mock_disconnected_store = PgStore(dsn="postgresql://invalid:invalid@127.0.0.1:5432/nonexistent")
    # Store pool is None or failed
    app_degraded = create_app(pg_store=mock_disconnected_store, config=cfg)
    transport_deg = httpx.ASGITransport(app=app_degraded)
    async with httpx.AsyncClient(transport=transport_deg, base_url="http://localhost:8000") as client:
        resp_deg = await client.get("/health")
        assert resp_deg.status_code == 200
        data_deg = resp_deg.json()
        assert data_deg["status"] == "degraded"
        assert data_deg["database"] == "disconnected"


@pytest.mark.asyncio
async def test_x8_latency_budget_p95_under_300ms(pg_store):
    """'Done when' condition: p95 latency < 300 ms for turn saves over HTTP."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="bench-account")
    svc = TurnService(pg_store, config=cfg)
    app = create_app(pg_store=pg_store, service=svc, config=cfg)

    latencies_ms: list[float] = []

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Initialize MCP session
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "bench-client", "version": "1.0"},
                },
            }
            init_resp = await client.post(
                "/mcp",
                json=init_req,
                headers={"Accept": "application/json, text/event-stream"},
            )
            session_id = init_resp.headers["mcp-session-id"]

            headers = {
                "mcp-session-id": session_id,
                "X-Account-ID": "bench-account",
                "Accept": "application/json, text/event-stream",
            }

            thread_id = None
            next_turn = None
            # Benchmark 20 consecutive turn saves
            for i in range(1, 21):
                prev_anc = f"Query number {i-1}" if i > 1 else None

                call_req = {
                    "jsonrpc": "2.0",
                    "id": i + 10,
                    "method": "tools/call",
                    "params": {
                        "name": "vault_log_turn",
                        "arguments": {
                            "user_message": f"Query number {i}",
                            "reply": f"Answer to query {i}",
                            "prev_user_anchor": prev_anc,
                            "thread_id": thread_id,
                            "turn": next_turn,
                            "title_hint": "Benchmark Conversation",
                        },
                    },
                }

                start = time.perf_counter()
                resp = await client.post("/mcp", json=call_req, headers=headers)
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                latencies_ms.append(elapsed_ms)

                assert resp.status_code == 200
                lines = resp.text.strip().splitlines()
                data_line = next(l for l in lines if l.startswith("data:"))
                res_dict = json.loads(data_line[len("data:"):].strip())
                turn_res = json.loads(res_dict["result"]["content"][0]["text"])
                assert turn_res["ok"] is True
                if thread_id is None:
                    thread_id = turn_res["thread_id"]
                next_turn = turn_res["next_turn"]

    latencies_sorted = sorted(latencies_ms)
    p95_index = int(len(latencies_sorted) * 0.95)
    p95_ms = latencies_sorted[p95_index]

    # Verify p95 latency is strictly under the 300 ms budget
    assert p95_ms < 300.0, f"p95 latency was {p95_ms:.2f} ms (exceeded 300 ms budget)"


def test_x8_deployment_configuration_files():
    """Verify presence and correctness of production deployment configs."""
    # 1. Dockerfile
    dockerfile_path = Path("Dockerfile")
    assert dockerfile_path.exists(), "Dockerfile is missing"
    df_content = dockerfile_path.read_text(encoding="utf-8")
    assert "useradd" in df_content or "threadvault" in df_content, "Non-root user missing in Dockerfile"
    assert "HEALTHCHECK" in df_content, "Docker HEALTHCHECK missing"
    assert "alembic upgrade head" in df_content, "Automated migration missing in Docker entrypoint"

    # 2. fly.toml
    fly_path = Path("fly.toml")
    assert fly_path.exists(), "fly.toml is missing"
    fly_content = fly_path.read_text(encoding="utf-8")
    assert "auto_stop_machines = false" in fly_content, "Always-on instance setting missing in fly.toml"
    assert 'path = "/health"' in fly_content, "Health check path missing in fly.toml"

    # 3. docs/DEPLOYMENT.md
    docs_path = Path("docs/DEPLOYMENT.md")
    assert docs_path.exists(), "docs/DEPLOYMENT.md is missing"
    docs_content = docs_path.read_text(encoding="utf-8")
    assert "p95 < 300 ms" in docs_content
    assert "Cold Starts" in docs_content
    assert "PITR" in docs_content


def test_h7_deploy_config_migrations_and_non_sleeping_instance():
    """Milestone H7: Verify Alembic migrations moved to release/pre-deploy commands and non-sleeping instance."""
    # 1. fly.toml release_command
    fly_path = Path("fly.toml")
    assert fly_path.exists()
    fly_content = fly_path.read_text(encoding="utf-8")
    assert "[deploy]" in fly_content
    assert 'release_command = "python -m alembic upgrade head"' in fly_content

    # 2. render.yaml preDeployCommand & non-sleeping tier
    render_path = Path("render.yaml")
    assert render_path.exists()
    render_content = render_path.read_text(encoding="utf-8")
    assert "preDeployCommand: python -m alembic upgrade head" in render_content
    # Non-sleeping paid plan (free tier sleeps after 15m)
    assert "plan: starter" in render_content
    assert "plan: free" not in render_content
