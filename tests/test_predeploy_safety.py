"""Pre-deploy safety tests verifying production security rules:

1. Fail-closed authentication (THREADVAULT_ENFORCE_AUTH defaults to true, blocks public start if disabled).
2. THREADVAULT_SERVER_KEY required unless running locally; no built-in default key.
3. Startup check names all missing required environment variables.
4. Remote GitHub export refused unless THREADVAULT_SINGLE_TENANT=true and exactly 1 account exists.
5. Canonical THREADVAULT_PUBLIC_URL for OAuth issuer & metadata, ignoring internal Host headers.
"""

from __future__ import annotations

import os
from pathlib import Path
import httpx
import pytest

from thread_save.config import VaultConfig
from thread_save.export.worker import MockExportTarget, OutboxWorker
from thread_save.security.redactor import get_server_key, redact_text
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.web.app import create_app
from thread_save.web.oauth import OAuthServer
from thread_save.web.startup import (
    REQUIRED_PROD_ENV_VARS,
    is_localhost_bound,
    validate_startup_requirements,
)

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


# ── 1. Auth Fail-Closed Tests ─────────────────────────────────────────────


def test_auth_fail_closed_refuses_public_start_when_disabled(monkeypatch):
    """Server refuses to start with auth disabled unless bound to localhost."""
    # Attempting to start on 0.0.0.0 with auth disabled must fail
    with pytest.raises(RuntimeError, match="THREADVAULT_ENFORCE_AUTH cannot be disabled unless bound to localhost"):
        validate_startup_requirements(host="0.0.0.0", enforce_auth=False)

    # Attempting with public URL / production host must fail
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="THREADVAULT_ENFORCE_AUTH cannot be disabled unless bound to localhost"):
        validate_startup_requirements(host="127.0.0.1", enforce_auth=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    # Permitted when bound strictly to localhost in local dev
    validate_startup_requirements(host="127.0.0.1", enforce_auth=False)
    validate_startup_requirements(host="localhost", enforce_auth=False)


# ── 2. Server Key & Startup Missing Variables Check ───────────────────────


def test_server_key_required_unless_running_locally(monkeypatch):
    """THREADVAULT_SERVER_KEY is required in non-local environments; no default key anywhere."""
    monkeypatch.delenv("THREADVAULT_SERVER_KEY", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(RuntimeError, match="THREADVAULT_SERVER_KEY is required in non-local environments"):
        get_server_key()

    monkeypatch.delenv("ENVIRONMENT", raising=False)

    # In local development without env var, an ephemeral random key is generated dynamically
    local_key = get_server_key()
    assert isinstance(local_key, bytes)
    assert len(local_key) == 32
    # Verify no built-in static default string
    assert local_key != b"threadvault_server_key"


def test_startup_check_names_all_missing_required_variables(monkeypatch):
    """Startup check explicitly names all missing required environment variables."""
    # Simulate non-local environment (host=0.0.0.0)
    for var in REQUIRED_PROD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        validate_startup_requirements(host="0.0.0.0", enforce_auth=True)

    err_msg = str(exc_info.value)
    assert "Production startup failed: missing required environment variables:" in err_msg
    for var in REQUIRED_PROD_ENV_VARS:
        assert var in err_msg, f"Expected {var} to be named in error message"


def test_startup_check_accepts_render_external_url(monkeypatch):
    """Startup check accepts RENDER_EXTERNAL_URL as fallback for THREADVAULT_PUBLIC_URL."""
    for var in REQUIRED_PROD_ENV_VARS:
        monkeypatch.setenv(var, "mock_val")
    monkeypatch.delenv("THREADVAULT_PUBLIC_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://threadvault-web.onrender.com")

    # Should not raise
    validate_startup_requirements(host="0.0.0.0", enforce_auth=True)



# ── 3. Canonical THREADVAULT_PUBLIC_URL & Metadata Issuer ─────────────────


@pytest.mark.asyncio
async def test_metadata_issuer_uses_public_url_with_internal_host(monkeypatch):
    """Metadata issuer equals THREADVAULT_PUBLIC_URL when request arrives with internal Host and http scheme."""
    public_url = "https://vault.example.com"
    monkeypatch.setenv("THREADVAULT_PUBLIC_URL", public_url)

    cfg = VaultConfig(
        vault_root=Path("./test_vault"),
        jwt_secret="mock-secret-for-issuer-test-min-32-bytes!!",
        allowed_hosts=("internal-service", "localhost", "testserver"),
    )
    oa_server = OAuthServer(jwt_secret=cfg.jwt_secret, public_url=public_url)
    app = create_app(config=cfg, oauth_server=oa_server, enforce_auth=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Request arrives with internal cluster host and http scheme
            headers = {"Host": "internal-service:8080", "X-Forwarded-Proto": "http"}

            resp = await client.get("/.well-known/oauth-authorization-server", headers=headers)
            assert resp.status_code == 200
            data = resp.json()

            # Issuer and endpoints MUST equal canonical THREADVAULT_PUBLIC_URL, not internal-service:8080
            assert data["issuer"] == public_url
            assert data["authorization_endpoint"] == f"{public_url}/oauth/authorize"
            assert data["token_endpoint"] == f"{public_url}/oauth/token"
            assert data["registration_endpoint"] == f"{public_url}/oauth/register"
            assert data["revocation_endpoint"] == f"{public_url}/oauth/revoke"

            # Check protected resource endpoint (RFC 9704)
            res_resp = await client.get("/.well-known/oauth-protected-resource", headers=headers)
            assert res_resp.status_code == 200
            res_data = res_resp.json()
            assert res_data["resource"] == public_url
            assert public_url in res_data["authorization_servers"]


# ── 4. Remote GitHub Export Single-Tenant Guard ────────────────────────────


@pytest.mark.asyncio
async def test_remote_github_export_guard_two_accounts_blocks_export(monkeypatch):
    """Remote GitHub export: refuse to run unless THREADVAULT_SINGLE_TENANT=true and exactly one account exists."""
    store = PgStore(dsn=TEST_DSN)
    await store.connect()

    async with store.pool.acquire() as conn:
        await conn.execute("TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE")

        # 1. Seed two distinct accounts in Postgres
        await conn.execute(
            "INSERT INTO accounts (id, oauth_sub, slug, created_at) VALUES (gen_random_uuid(), 'sub_user1', 'user-one', now())"
        )
        acc2_id = await conn.fetchval(
            "INSERT INTO accounts (id, oauth_sub, slug, created_at) VALUES (gen_random_uuid(), 'sub_user2', 'user-two', now()) RETURNING id"
        )

        # Create a thread for user-one
        await conn.execute(
            """INSERT INTO threads (id, account_id, title, slug, delim, current_page)
               SELECT '01M_TEST_GH_GUARD_0000001', id, 'Test Export Thread', 'test-thread', 'k7Qx', 1
               FROM accounts WHERE slug = 'user-one'"""
        )
        await conn.execute(
            """INSERT INTO turns (thread_id, n, role, body, fidelity, chars, hash, page)
               VALUES ('01M_TEST_GH_GUARD_0000001', 1, 'user', 'Hello export', 4, 12, 'h1', 1)"""
        )

        # Enqueue job targeting github
        await conn.execute(
            """INSERT INTO outbox (thread_id, target, due_at, attempts)
               VALUES ('01M_TEST_GH_GUARD_0000001', 'github', now() - interval '1 minute', 0)"""
        )

    mock_target = MockExportTarget()
    worker = OutboxWorker(store=store, targets={"github": mock_target, "default": mock_target})

    # Case A: THREADVAULT_SINGLE_TENANT=true, BUT two accounts exist in DB
    monkeypatch.setenv("THREADVAULT_SINGLE_TENANT", "true")
    processed_count = await worker.process_batch()

    # Nothing must be exported because account count == 2
    assert processed_count == 0
    assert len(mock_target.exports) == 0, "Expected zero exports when two accounts exist in DB"

    # Case B: Delete second account, leaving exactly 1 account
    async with store.pool.acquire() as conn:
        await conn.execute("DELETE FROM accounts WHERE id = $1", acc2_id)

    # Now with THREADVAULT_SINGLE_TENANT=true and exactly 1 account, export succeeds!
    processed_count = await worker.process_batch()
    assert processed_count == 1
    assert len(mock_target.exports) == 1
    assert mock_target.exports[0]["thread_id"] == "01M_TEST_GH_GUARD_0000001"

    await store.close()
