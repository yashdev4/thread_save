"""FastAPI Application mounting FastMCP over Streamable HTTP (§4, Milestone X3)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from thread_save.config import VaultConfig, load_config
from thread_save.service import TurnService
from thread_save.storage.writer import FileStore
from thread_save.web.mcp_server import create_http_mcp_server
from thread_save.web.middleware import (
    AccountContextMiddleware,
    BodySizeLimitMiddleware,
    OriginValidatorMiddleware,
    RateLimitMiddleware,
    RateLimiter,
)
from thread_save.web.oauth import OAuthServer, create_oauth_router
from thread_save.web.startup import get_public_url, validate_startup_requirements

logger = logging.getLogger("thread_save.web.app")


def _configure_logging() -> None:
    """Uvicorn only sets up its own loggers; without this, thread_save INFO lines
    (which client connected, whether it is archived: P1-14) never reach the platform log."""
    tv = logging.getLogger("thread_save")
    if tv.level == logging.NOTSET:
        tv.setLevel(logging.INFO)
    if not tv.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        tv.addHandler(handler)


def create_app(
    service: Optional[TurnService] = None,
    config: Optional[VaultConfig] = None,
    rate_limiter: Optional[RateLimiter] = None,
    oauth_server: Optional[OAuthServer] = None,
    enforce_auth: Optional[bool] = None,
    host: Optional[str] = None,
) -> FastAPI:
    _configure_logging()
    cfg = config or load_config()

    # Resolve enforce_auth (fail-closed default: True in production/server startup)
    if enforce_auth is not None:
        enforce_auth_eff = bool(enforce_auth)
    elif "THREADVAULT_ENFORCE_AUTH" in os.environ:
        auth_raw = os.environ["THREADVAULT_ENFORCE_AUTH"].strip().lower()
        enforce_auth_eff = auth_raw in ("true", "1", "yes")
    elif config is not None:
        enforce_auth_eff = cfg.enforce_auth
    else:
        enforce_auth_eff = True

    # Pre-deploy safety startup validation
    validate_startup_requirements(host=host, enforce_auth=enforce_auth_eff)

    store = FileStore(config=cfg)

    svc = service or TurnService(store, config=cfg)
    if oauth_server:
        oa_server = oauth_server
        if oa_server.public_url is None:
            oa_server.public_url = cfg.public_url
    else:
        oa_server = OAuthServer(
            jwt_secret=cfg.jwt_secret,
            public_url=cfg.public_url,
        )

    from mcp.server.transport_security import TransportSecuritySettings

    mcp_server = create_http_mcp_server(svc)
    transport_sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp_asgi = mcp_server.streamable_http_app(transport_security=transport_sec)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Validate startup safety rules upon server boot
        validate_startup_requirements(host=host, enforce_auth=enforce_auth_eff)

        # 1. Start background GitHub sync loop if configured
        sync_task = None
        gh_repo = os.environ.get(
            "THREADVAULT_GH_REPO", "https://github.com/yashdev4/thread_vault.git"
        ).strip()
        if gh_repo and isinstance(store, FileStore):
            from thread_save.export.sync_loop import start_github_sync_loop
            # P1-2: pushes after saves only (no timer); the interval is no longer used
            sync_task = asyncio.create_task(
                start_github_sync_loop(
                    vault_root=cfg.vault_root,
                    repo=gh_repo,
                    token=os.environ.get("GITHUB_TOKEN") or os.environ.get("THREADVAULT_GH_TOKEN"),
                    store=store,
                )
            )

        # 2. Enter MCP Streamable HTTP session manager lifespan
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            logger.info(
                "ThreadVault Web & MCP Streamable HTTP server started (Storage: %s)",
                type(store).__name__,
            )
            yield

        # 3. Cleanup background sync task
        if sync_task:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass
        logger.info("ThreadVault Web & MCP Streamable HTTP server stopped")

    app = FastAPI(
        title="ThreadVault Remote MCP Server",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Security and rate-limiting middlewares (order matters: outer to inner)
    app.add_middleware(
        AccountContextMiddleware,
        oauth_server=oa_server,
        enforce_auth=enforce_auth_eff,
    )
    app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        OriginValidatorMiddleware,
        allowed_hosts=cfg.allowed_hosts,
    )

    # OAuth 2.1 endpoints (discovery, DCR, PKCE authorization, token issuance & refresh)
    app.include_router(create_oauth_router(oa_server))

    # Health check endpoint (§4.4, Milestone X8 prerequisite)
    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "storage": "filestore",
            "database": "connected",
            "version": "0.2.0",
        }

    @app.get("/")
    async def root():
        return {
            "name": "ThreadVault",
            "version": "0.2.0",
            "endpoint": "/mcp",
            "transport": "streamable_http",
        }

    # Mount Streamable HTTP app at root so it handles /mcp directly
    app.mount("", mcp_asgi)

    return app
