"""OAuth 2.1 Server implementation for ThreadVault (§4, Milestone X4).

Implements:
- RFC 8414: OAuth 2.0 Authorization Server Metadata (/.well-known/oauth-authorization-server)
- OpenID Connect Discovery (/.well-known/openid-configuration)
- RFC 7591: Dynamic Client Registration (/oauth/register)
- RFC 7636: Proof Key for Code Exchange (PKCE with S256) (/oauth/authorize)
- RFC 6749: Token issuance and refresh (/oauth/token)
- Cryptographic JWT access tokens bound to account identity
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any, Optional
import urllib.parse

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import jwt

logger = logging.getLogger("thread_save.web.oauth")

JWT_SECRET_DEFAULT = os.environ.get(
    "THREADVAULT_JWT_SECRET",
    "threadvault-default-jwt-secret-key-change-in-prod-32bytes-min!",
)
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME = 3600  # 1 hour
AUTH_CODE_LIFETIME = 600  # 10 minutes
REFRESH_TOKEN_LIFETIME = 30 * 86400  # 30 days

# Pre-allowed redirect URI hostnames for Claude
ALLOWED_REDIRECT_HOSTS = {
    "claude.ai",
    "claude.com",
    "localhost",
    "127.0.0.1",
}


@dataclass
class ClientRegistration:
    client_id: str
    client_secret: Optional[str]
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    created_at: float = field(default_factory=time.time)


@dataclass
class AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    account_id: str
    scope: str
    expires_at: float


@dataclass
class RefreshTokenRecord:
    token: str
    client_id: str
    account_id: str
    scope: str
    expires_at: float


class OAuthServer:
    """In-memory OAuth 2.1 Server managing clients, authorization codes, and refresh tokens."""

    def __init__(self, jwt_secret: str = JWT_SECRET_DEFAULT):
        self.jwt_secret = jwt_secret
        self.clients: dict[str, ClientRegistration] = {}
        self.auth_codes: dict[str, AuthCode] = {}
        self.refresh_tokens: dict[str, RefreshTokenRecord] = {}

        # Pre-register default Claude client
        self.register_client(
            client_id="claude-connector-default",
            client_name="Claude Web Connector",
            redirect_uris=[
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
            ],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_secret=None,
        )

    def register_client(
        self,
        client_name: str,
        redirect_uris: list[str],
        grant_types: Optional[list[str]] = None,
        response_types: Optional[list[str]] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> ClientRegistration:
        cid = client_id or f"client_{secrets.token_urlsafe(16)}"
        csecret = client_secret or secrets.token_urlsafe(32)
        grants = grant_types or ["authorization_code", "refresh_token"]
        responses = response_types or ["code"]

        reg = ClientRegistration(
            client_id=cid,
            client_secret=csecret,
            client_name=client_name,
            redirect_uris=redirect_uris,
            grant_types=grants,
            response_types=responses,
        )
        self.clients[cid] = reg
        return reg

    def get_client(self, client_id: str) -> Optional[ClientRegistration]:
        return self.clients.get(client_id)

    def create_auth_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        account_id: str,
        scope: str = "vault",
    ) -> str:
        code = f"code_{secrets.token_urlsafe(32)}"
        self.auth_codes[code] = AuthCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            account_id=account_id,
            scope=scope,
            expires_at=time.time() + AUTH_CODE_LIFETIME,
        )
        return code

    def consume_auth_code(self, code: str) -> Optional[AuthCode]:
        auth_code = self.auth_codes.pop(code, None)
        if not auth_code:
            return None
        if time.time() > auth_code.expires_at:
            return None
        return auth_code

    def create_tokens(
        self,
        client_id: str,
        account_id: str,
        scope: str = "vault",
    ) -> tuple[str, str]:
        now = int(time.time())
        payload = {
            "iss": "threadvault",
            "sub": account_id,
            "account_id": account_id,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + ACCESS_TOKEN_LIFETIME,
            "jti": secrets.token_urlsafe(16),
        }
        access_token = jwt.encode(payload, self.jwt_secret, algorithm=JWT_ALGORITHM)

        refresh_token = f"rt_{secrets.token_urlsafe(32)}"
        self.refresh_tokens[refresh_token] = RefreshTokenRecord(
            token=refresh_token,
            client_id=client_id,
            account_id=account_id,
            scope=scope,
            expires_at=now + REFRESH_TOKEN_LIFETIME,
        )
        return access_token, refresh_token

    def rotate_refresh_token(
        self, refresh_token: str, client_id: Optional[str] = None
    ) -> Optional[tuple[str, str, str]]:
        """Validate, remove old refresh token, and issue a new pair.

        Returns (access_token, new_refresh_token, account_id) or None.
        """
        rec = self.refresh_tokens.pop(refresh_token, None)
        if not rec:
            return None
        if time.time() > rec.expires_at:
            return None
        if client_id and rec.client_id != client_id:
            return None

        access_token, new_refresh_token = self.create_tokens(
            client_id=rec.client_id,
            account_id=rec.account_id,
            scope=rec.scope,
        )
        return access_token, new_refresh_token, rec.account_id

    def verify_access_token(self, token: str) -> dict[str, Any]:
        """Decode and verify JWT access token. Raises jwt.PyJWTError on failure."""
        return jwt.decode(token, self.jwt_secret, algorithms=[JWT_ALGORITHM])


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify code_verifier against code_challenge using S256."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    clean_challenge = code_challenge.rstrip("=")
    return hmac.compare_digest(computed, clean_challenge)


def create_oauth_router(oauth_server: OAuthServer) -> APIRouter:
    """Create FastAPI APIRouter exposing OAuth 2.1 endpoints."""
    router = APIRouter()

    # 1. Discovery Metadata (RFC 8414 & OpenID Connect)
    @router.get("/.well-known/oauth-authorization-server")
    @router.get("/.well-known/openid-configuration")
    async def oauth_discovery(request: Request):
        base_url = str(request.base_url).rstrip("/")
        return {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            "scopes_supported": ["vault"],
        }

    # 2. Dynamic Client Registration (RFC 7591)
    @router.post("/oauth/register", status_code=201)
    async def register_client(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        client_name = body.get("client_name") or "External Client"
        redirect_uris = body.get("redirect_uris")
        if not redirect_uris or not isinstance(redirect_uris, list):
            raise HTTPException(
                status_code=400, detail="redirect_uris must be a non-empty list"
            )

        # Validate redirect URIs
        for uri in redirect_uris:
            parsed = urllib.parse.urlparse(uri)
            if not parsed.scheme or not parsed.netloc:
                raise HTTPException(status_code=400, detail=f"Invalid redirect URI: {uri}")
            hostname = parsed.hostname or ""
            if parsed.scheme != "https" and hostname not in ("localhost", "127.0.0.1"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Redirect URI must use HTTPS or localhost: {uri}",
                )

        reg = oauth_server.register_client(
            client_name=client_name,
            redirect_uris=redirect_uris,
            grant_types=body.get("grant_types"),
            response_types=body.get("response_types"),
        )

        return {
            "client_id": reg.client_id,
            "client_secret": reg.client_secret,
            "client_name": reg.client_name,
            "redirect_uris": reg.redirect_uris,
            "grant_types": reg.grant_types,
            "response_types": reg.response_types,
            "token_endpoint_auth_method": "none",
        }

    # 3. Authorization Endpoint (RFC 7636 + PKCE)
    @router.get("/oauth/authorize")
    async def authorize_get(
        request: Request,
        client_id: str,
        redirect_uri: str,
        response_type: str,
        code_challenge: str,
        code_challenge_method: str = "S256",
        state: Optional[str] = None,
        account: Optional[str] = None,
        auto_approve: Optional[str] = None,
    ):
        client = oauth_server.get_client(client_id)
        if not client:
            raise HTTPException(status_code=400, detail="Unknown client_id")

        if redirect_uri not in client.redirect_uris:
            raise HTTPException(
                status_code=400, detail="redirect_uri not registered for client"
            )

        if response_type != "code":
            raise HTTPException(
                status_code=400, detail="Unsupported response_type (only 'code' supported)"
            )

        if code_challenge_method != "S256":
            raise HTTPException(
                status_code=400,
                detail="code_challenge_method must be 'S256' per OAuth 2.1",
            )

        account_id = account or "default"

        # If interactive user requested approval page without auto_approve parameter
        accept_header = request.headers.get("accept", "")
        if "text/html" in accept_header and auto_approve != "1" and auto_approve != "true":
            # Return interactive HTML consent form
            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Authorize ThreadVault</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
        .card {{ background: #1e293b; padding: 2rem; border-radius: 0.75rem; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.5); width: 100%; max-width: 420px; }}
        h1 {{ font-size: 1.25rem; margin-top: 0; }}
        p {{ color: #94a3b8; font-size: 0.875rem; line-height: 1.4; }}
        .client {{ color: #38bdf8; font-weight: 600; }}
        .field {{ margin-bottom: 1rem; }}
        label {{ display: block; font-size: 0.75rem; text-transform: uppercase; color: #94a3b8; margin-bottom: 0.25rem; }}
        input {{ width: 100%; padding: 0.5rem; background: #0f172a; border: 1px solid #334155; border-radius: 0.375rem; color: #fff; font-size: 0.875rem; box-sizing: border-box; }}
        button {{ width: 100%; padding: 0.625rem; background: #2563eb; color: #fff; border: none; border-radius: 0.375rem; font-weight: 600; cursor: pointer; }}
        button:hover {{ background: #1d4ed8; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Authorize Connector</h1>
        <p><span class="client">{client.client_name}</span> is requesting access to save and search your conversations in ThreadVault.</p>
        <form method="POST" action="/oauth/authorize">
            <input type="hidden" name="client_id" value="{client_id}" />
            <input type="hidden" name="redirect_uri" value="{redirect_uri}" />
            <input type="hidden" name="response_type" value="{response_type}" />
            <input type="hidden" name="code_challenge" value="{code_challenge}" />
            <input type="hidden" name="code_challenge_method" value="{code_challenge_method}" />
            <input type="hidden" name="state" value="{state or ''}" />
            <div class="field">
                <label for="account">Account ID / Slug</label>
                <input type="text" id="account" name="account" value="{account_id}" required />
            </div>
            <button type="submit">Allow Access</button>
        </form>
    </div>
</body>
</html>"""
            return HTMLResponse(content=html_content)

        # Issue code and redirect
        code = oauth_server.create_auth_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            account_id=account_id,
        )

        params = {"code": code}
        if state:
            params["state"] = state
        separator = "&" if "?" in redirect_uri else "?"
        target_url = f"{redirect_uri}{separator}{urllib.parse.urlencode(params)}"
        return RedirectResponse(url=target_url, status_code=302)

    @router.post("/oauth/authorize")
    async def authorize_post(
        client_id: str = Form(...),
        redirect_uri: str = Form(...),
        response_type: str = Form(...),
        code_challenge: str = Form(...),
        code_challenge_method: str = Form("S256"),
        state: Optional[str] = Form(None),
        account: Optional[str] = Form("default"),
    ):
        client = oauth_server.get_client(client_id)
        if not client or redirect_uri not in client.redirect_uris:
            raise HTTPException(status_code=400, detail="Invalid client or redirect_uri")

        code = oauth_server.create_auth_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            account_id=account or "default",
        )

        params = {"code": code}
        if state:
            params["state"] = state
        separator = "&" if "?" in redirect_uri else "?"
        target_url = f"{redirect_uri}{separator}{urllib.parse.urlencode(params)}"
        return RedirectResponse(url=target_url, status_code=302)

    # 4. Token Endpoint (RFC 6749 + PKCE verification)
    @router.post("/oauth/token")
    async def token_endpoint(request: Request):
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                data = await request.json()
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_request", "error_description": "Malformed JSON"},
                )
        else:
            form = await request.form()
            data = dict(form)

        grant_type = data.get("grant_type")
        client_id = data.get("client_id")

        if grant_type == "authorization_code":
            code = data.get("code")
            redirect_uri = data.get("redirect_uri")
            code_verifier = data.get("code_verifier")

            if not code or not redirect_uri or not code_verifier:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "error_description": "Missing code, redirect_uri, or code_verifier",
                    },
                )

            auth_code = oauth_server.consume_auth_code(code)
            if not auth_code:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_grant",
                        "error_description": "Authorization code expired or invalid",
                    },
                )

            if client_id and auth_code.client_id != client_id:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_client", "error_description": "Client mismatch"},
                )

            if auth_code.redirect_uri != redirect_uri:
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_grant", "error_description": "Redirect URI mismatch"},
                )

            if not verify_pkce(code_verifier, auth_code.code_challenge):
                return JSONResponse(
                    status_code=400,
                    content={"error": "invalid_grant", "error_description": "PKCE verification failed"},
                )

            access_token, refresh_token = oauth_server.create_tokens(
                client_id=auth_code.client_id,
                account_id=auth_code.account_id,
                scope=auth_code.scope,
            )

            return JSONResponse(
                status_code=200,
                content={
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": ACCESS_TOKEN_LIFETIME,
                    "refresh_token": refresh_token,
                    "scope": auth_code.scope,
                },
            )

        elif grant_type == "refresh_token":
            refresh_token = data.get("refresh_token")
            if not refresh_token:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "error_description": "Missing refresh_token parameter",
                    },
                )

            res = oauth_server.rotate_refresh_token(
                refresh_token=refresh_token,
                client_id=client_id,
            )
            if not res:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_grant",
                        "error_description": "Refresh token expired or invalid",
                    },
                )

            new_access_token, new_refresh_token, _ = res
            return JSONResponse(
                status_code=200,
                content={
                    "access_token": new_access_token,
                    "token_type": "Bearer",
                    "expires_in": ACCESS_TOKEN_LIFETIME,
                    "refresh_token": new_refresh_token,
                    "scope": "vault",
                },
            )

        else:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "unsupported_grant_type",
                    "error_description": f"Grant type '{grant_type}' is not supported",
                },
            )

    return router
