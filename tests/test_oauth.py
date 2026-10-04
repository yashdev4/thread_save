"""Verification suite for OAuth 2.1 implementation (§4, Milestone X4).

Covers:
- RFC 8414 & OpenID Connect Discovery metadata
- RFC 7591 Dynamic Client Registration (DCR) & validation
- RFC 7636 PKCE Authorization Code flow (S256 enforcement, consent form)
- Token issuance with PKCE validation & single-use code enforcement
- Refresh token rotation & reuse prevention
- Account identity binding from JWT access token
- RLS isolation per request across distinct OAuth tokens
- 401 Unauthorized on invalid/expired/missing tokens
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.web.app import create_app
from thread_save.web.oauth import OAuthServer

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and S256 code_challenge."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


@pytest.fixture
async def pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts, oauth_clients, oauth_auth_codes, oauth_refresh_tokens CASCADE"
        )
    yield store
    await store.close()


@pytest.fixture
def oauth_setup(pg_store):
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="oauth-default")
    svc = TurnService(pg_store, config=cfg)
    oa_server = OAuthServer(jwt_secret="test-oauth-secret-key-32bytes-minimum!!", pg_store=pg_store)
    app = create_app(
        pg_store=pg_store,
        service=svc,
        config=cfg,
        oauth_server=oa_server,
        enforce_auth=True,
    )
    return app, oa_server


@pytest.mark.asyncio
async def test_oauth_discovery_metadata(oauth_setup):
    app, _ = oauth_setup
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            for path in [
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ]:
                resp = await client.get(path)
                assert resp.status_code == 200, f"Failed on {path}"
                data = resp.json()
                assert "http://localhost:8000" in data["issuer"]
                assert data["authorization_endpoint"] == "http://localhost:8000/oauth/authorize"
                assert data["token_endpoint"] == "http://localhost:8000/oauth/token"
                assert data["registration_endpoint"] == "http://localhost:8000/oauth/register"
                assert "code" in data["response_types_supported"]
                assert "S256" in data["code_challenge_methods_supported"]
                assert "authorization_code" in data["grant_types_supported"]
                assert "refresh_token" in data["grant_types_supported"]


@pytest.mark.asyncio
async def test_oauth_dynamic_client_registration(oauth_setup):
    app, _ = oauth_setup
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 1. Successful DCR with Claude web callback
            reg_payload = {
                "client_name": "Claude Web App",
                "redirect_uris": [
                    "https://claude.ai/api/mcp/auth_callback",
                    "https://claude.com/api/mcp/auth_callback",
                ],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }
            resp = await client.post("/oauth/register", json=reg_payload)
            assert resp.status_code == 201
            data = resp.json()
            assert "client_id" in data
            assert data["client_name"] == "Claude Web App"
            assert "https://claude.ai/api/mcp/auth_callback" in data["redirect_uris"]

            # 2. Rejection of insecure non-localhost HTTP redirect URI
            bad_payload = {
                "client_name": "Insecure Client",
                "redirect_uris": ["http://insecure-site.com/callback"],
            }
            bad_resp = await client.post("/oauth/register", json=bad_payload)
            assert bad_resp.status_code == 400
            assert "must use https or localhost" in bad_resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_oauth_authorization_pkce_and_token_flow(oauth_setup):
    app, oa_server = oauth_setup
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Register client
            reg_resp = await client.post(
                "/oauth/register",
                json={
                    "client_name": "Test Client",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                },
            )
            client_id = reg_resp.json()["client_id"]

            verifier, challenge = generate_pkce_pair()

            # 1. Authorize GET with auto_approve
            auth_url = (
                f"/oauth/authorize?client_id={client_id}&redirect_uri=https://claude.ai/api/mcp/auth_callback"
                f"&response_type=code&code_challenge={challenge}&code_challenge_method=S256"
                f"&state=test_state_123&account=alice_user&auto_approve=1"
            )
            auth_resp = await client.get(auth_url, follow_redirects=False)
            assert auth_resp.status_code == 302
            location = auth_resp.headers["location"]
            assert "https://claude.ai/api/mcp/auth_callback" in location
            assert "state=test_state_123" in location

            # Extract authorization code
            code = location.split("code=")[1].split("&")[0]
            assert code.startswith("code_")

            # 2. Exchange code for tokens with WRONG code_verifier -> Fails
            bad_token_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": "wrong_verifier_12345",
                },
            )
            assert bad_token_resp.status_code == 400
            assert bad_token_resp.json()["error"] == "invalid_grant"

            # Re-authorize to get new code for valid exchange
            auth_resp2 = await client.get(auth_url, follow_redirects=False)
            code2 = auth_resp2.headers["location"].split("code=")[1].split("&")[0]

            # 3. Exchange code with CORRECT code_verifier -> Succeeds
            token_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code2,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier,
                },
            )
            assert token_resp.status_code == 200
            token_data = token_resp.json()
            access_token = token_data["access_token"]
            refresh_token = token_data["refresh_token"]
            assert token_data["token_type"] == "Bearer"
            assert token_data["expires_in"] == 3600

            # 4. Single-use check: reusing code2 fails
            reuse_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code2,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier,
                },
            )
            assert reuse_resp.status_code == 400
            assert reuse_resp.json()["error"] == "invalid_grant"

            # 5. Token refresh flow
            refresh_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            assert refresh_resp.status_code == 200
            refreshed_data = refresh_resp.json()
            new_access_token = refreshed_data["access_token"]
            new_refresh_token = refreshed_data["refresh_token"]
            assert new_access_token != access_token
            assert new_refresh_token != refresh_token

            # 6. Reusing old refresh token fails (rotation enforced)
            old_rt_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            assert old_rt_resp.status_code == 400
            assert old_rt_resp.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_oauth_authenticated_mcp_and_rls_isolation(oauth_setup, pg_store):
    """Verify that OAuth Bearer token scopes MCP requests to the token's account with RLS."""
    app, oa_server = oauth_setup
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Issue tokens for two separate users: alice and bob
            alice_token, _ = oa_server.create_tokens("client-1", "alice_account")
            bob_token, _ = oa_server.create_tokens("client-1", "bob_account")

            # 1. Unauthenticated request to /mcp returns 401 when enforce_auth is True
            unauth_resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert unauth_resp.status_code == 401

            # 2. Invalid token returns 401
            invalid_resp = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Authorization": "Bearer invalid_tampered_token_xyz"},
            )
            assert invalid_resp.status_code == 401

            # 3. Alice initializes MCP session with valid token
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-alice", "version": "1.0"},
                },
            }
            alice_init = await client.post(
                "/mcp",
                json=init_req,
                headers={
                    "Authorization": f"Bearer {alice_token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert alice_init.status_code == 200
            alice_session = alice_init.headers["mcp-session-id"]

            # Alice saves a turn
            alice_save = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "vault_save_turn",
                        "arguments": {
                            "user_query": "Alice's confidential message",
                            "title_hint": "Alice Thread",
                        },
                    },
                },
                headers={
                    "Authorization": f"Bearer {alice_token}",
                    "mcp-session-id": alice_session,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert alice_save.status_code == 200
            alice_lines = alice_save.text.strip().splitlines()
            alice_data = json.loads(next(l for l in alice_lines if l.startswith("data:"))[len("data:"):].strip())
            alice_res = json.loads(alice_data["result"]["content"][0]["text"])
            assert alice_res["ok"] is True
            alice_thread_id = alice_res["thread_id"]

            # 4. Bob initializes MCP session with Bob's token
            bob_init = await client.post(
                "/mcp",
                json=init_req,
                headers={
                    "Authorization": f"Bearer {bob_token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert bob_init.status_code == 200
            bob_session = bob_init.headers["mcp-session-id"]

            # Bob tries to find Alice's confidential message
            bob_find = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "vault_find",
                        "arguments": {
                            "query": "confidential",
                        },
                    },
                },
                headers={
                    "Authorization": f"Bearer {bob_token}",
                    "mcp-session-id": bob_session,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert bob_find.status_code == 200
            bob_lines = bob_find.text.strip().splitlines()
            bob_data = json.loads(next(l for l in bob_lines if l.startswith("data:"))[len("data:"):].strip())
            bob_res = json.loads(bob_data["result"]["content"][0]["text"])

            # Bob finds 0 hits because RLS isolates Alice's data from Bob's account!
            assert len(bob_res["threads"]) == 0

            # Alice finds her own message
            alice_find = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "vault_find",
                        "arguments": {
                            "query": "confidential",
                        },
                    },
                },
                headers={
                    "Authorization": f"Bearer {alice_token}",
                    "mcp-session-id": alice_session,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert alice_find.status_code == 200
            alice_lines2 = alice_find.text.strip().splitlines()
            alice_data2 = json.loads(next(l for l in alice_lines2 if l.startswith("data:"))[len("data:"):].strip())
            alice_res2 = json.loads(alice_data2["result"]["content"][0]["text"])
            assert len(alice_res2["threads"]) == 1
            assert alice_res2["threads"][0]["thread_id"] == alice_thread_id


@pytest.mark.asyncio
async def test_oauth_server_restart_refresh_token_persistence(pg_store):
    """Verify that after a complete server restart (blank memory state), existing refresh tokens in Postgres still work."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="oauth-default")
    svc = TurnService(pg_store, config=cfg)
    jwt_secret = "restart-test-secret-key-32bytes-minimum!!"

    # Server instance 1
    oa_server_1 = OAuthServer(jwt_secret=jwt_secret, pg_store=pg_store)
    app_1 = create_app(pg_store=pg_store, service=svc, config=cfg, oauth_server=oa_server_1, enforce_auth=True)

    client_id = ""
    refresh_token = ""
    verifier, challenge = generate_pkce_pair()

    async with app_1.router.lifespan_context(app_1):
        transport = httpx.ASGITransport(app=app_1)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Register client
            reg_resp = await client.post(
                "/oauth/register",
                json={
                    "client_name": "Restart Client",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                },
            )
            assert reg_resp.status_code == 201
            client_id = reg_resp.json()["client_id"]

            # Authorize
            auth_url = (
                f"/oauth/authorize?client_id={client_id}&redirect_uri=https://claude.ai/api/mcp/auth_callback"
                f"&response_type=code&code_challenge={challenge}&code_challenge_method=S256"
                f"&state=st1&account=restart_user&auto_approve=1"
            )
            auth_resp = await client.get(auth_url, follow_redirects=False)
            assert auth_resp.status_code == 302
            code = auth_resp.headers["location"].split("code=")[1].split("&")[0]

            # Token exchange
            token_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier,
                },
            )
            assert token_resp.status_code == 200
            refresh_token = token_resp.json()["refresh_token"]

    # --- SIMULATE SERVER RESTART ---
    # Instance 1 is shut down. Create completely NEW server instance 2 with blank memory state!
    oa_server_2 = OAuthServer(jwt_secret=jwt_secret, pg_store=pg_store)
    # Memory dictionaries are completely empty
    assert len(oa_server_2.clients) == 0
    assert len(oa_server_2.refresh_tokens) == 0

    app_2 = create_app(pg_store=pg_store, service=svc, config=cfg, oauth_server=oa_server_2, enforce_auth=True)
    async with app_2.router.lifespan_context(app_2):
        transport = httpx.ASGITransport(app=app_2)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Client registered on server 1 can still refresh token on server 2
            refresh_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            assert refresh_resp.status_code == 200
            data = refresh_resp.json()
            assert "access_token" in data
            assert data["refresh_token"] != refresh_token

            # Verify old refresh token is consumed/rotated and cannot be reused
            reuse_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            assert reuse_resp.status_code == 400
            assert reuse_resp.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_oauth_revoke_and_reconnect_stable_account_id(pg_store):
    """Verify that revoking and reconnecting with the same Google upstream IdP yields the identical account_id."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="oauth-default")
    svc = TurnService(pg_store, config=cfg)
    jwt_secret = "google-reconnect-secret-key-32bytes-min!!"

    oa_server = OAuthServer(jwt_secret=jwt_secret, pg_store=pg_store)
    app = create_app(pg_store=pg_store, service=svc, config=cfg, oauth_server=oa_server, enforce_auth=True)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Register client
            reg_resp = await client.post(
                "/oauth/register",
                json={
                    "client_name": "Claude Desktop",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                },
            )
            client_id = reg_resp.json()["client_id"]

            google_user_sub = "google_user_id_10928374928"

            # 1. First Connection: Authorize with Google upstream IdP
            verifier_1, challenge_1 = generate_pkce_pair()
            auth_url_1 = (
                f"/oauth/authorize?client_id={client_id}&redirect_uri=https://claude.ai/api/mcp/auth_callback"
                f"&response_type=code&code_challenge={challenge_1}&code_challenge_method=S256"
                f"&state=state_1&google_sub={google_user_sub}&auto_approve=1"
            )
            auth_resp_1 = await client.get(auth_url_1, follow_redirects=False)
            assert auth_resp_1.status_code == 302
            code_1 = auth_resp_1.headers["location"].split("code=")[1].split("&")[0]

            token_resp_1 = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code_1,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier_1,
                },
            )
            assert token_resp_1.status_code == 200
            token_data_1 = token_resp_1.json()
            access_token_1 = token_data_1["access_token"]
            refresh_token_1 = token_data_1["refresh_token"]

            payload_1 = oa_server.verify_access_token(access_token_1)
            account_id_1 = payload_1["account_id"]
            assert account_id_1 is not None

            # 2. Revoke the token
            revoke_resp = await client.post(
                "/oauth/revoke",
                data={"token": refresh_token_1},
            )
            assert revoke_resp.status_code == 200

            # Verify revoked refresh token is unusable
            revoked_refresh_resp = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token_1,
                    "client_id": client_id,
                },
            )
            assert revoked_refresh_resp.status_code == 400
            assert revoked_refresh_resp.json()["error"] == "invalid_grant"

            # 3. Reconnect: Same user signs in again with identical Google upstream IdP
            verifier_2, challenge_2 = generate_pkce_pair()
            auth_url_2 = (
                f"/oauth/authorize?client_id={client_id}&redirect_uri=https://claude.ai/api/mcp/auth_callback"
                f"&response_type=code&code_challenge={challenge_2}&code_challenge_method=S256"
                f"&state=state_2&google_sub={google_user_sub}&auto_approve=1"
            )
            auth_resp_2 = await client.get(auth_url_2, follow_redirects=False)
            assert auth_resp_2.status_code == 302
            code_2 = auth_resp_2.headers["location"].split("code=")[1].split("&")[0]

            token_resp_2 = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code_2,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier_2,
                },
            )
            assert token_resp_2.status_code == 200
            token_data_2 = token_resp_2.json()
            access_token_2 = token_data_2["access_token"]

            payload_2 = oa_server.verify_access_token(access_token_2)
            account_id_2 = payload_2["account_id"]

            # CRITICAL ASSERTION: Reconnecting yields the exact same account_id!
            assert account_id_1 == account_id_2
