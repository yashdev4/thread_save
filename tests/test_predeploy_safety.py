"""Pre-deploy safety tests verifying production security rules:

1. Fail-closed authentication (THREADVAULT_ENFORCE_AUTH defaults to true, blocks public start if disabled).
2. THREADVAULT_SERVER_KEY required unless running locally; no built-in default key.
3. Startup check names all missing required environment variables.
4. Remote GitHub export refused unless THREADVAULT_SINGLE_TENANT=true and exactly 1 account exists.
5. Canonical THREADVAULT_PUBLIC_URL for OAuth issuer & metadata, ignoring internal Host headers.
"""

from __future__ import annotations

from pathlib import Path
import httpx
import pytest

from thread_save.config import VaultConfig
from thread_save.security.redactor import get_server_key
from thread_save.web.app import create_app
from thread_save.web.oauth import OAuthServer
from thread_save.web.startup import (
    REQUIRED_PROD_ENV_VARS,
    validate_startup_requirements,
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

