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
from thread_save.storage.pg_store import PgStore
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
from thread_save.web.viewer import create_viewer_router

logger = logging.getLogger("thread_save.web.app")


def create_app(
    pg_store: Optional[PgStore] = None,
    service: Optional[TurnService] = None,
    config: Optional[VaultConfig] = None,
    rate_limiter: Optional[RateLimiter] = None,
    oauth_server: Optional[OAuthServer] = None,
    enforce_auth: Optional[bool] = None,
    host: Optional[str] = None,
) -> FastAPI:
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

    storage_backend = os.environ.get("THREADVAULT_STORAGE_BACKEND", "").strip().lower()
    has_db = bool(os.environ.get("DATABASE_URL"))

    if pg_store is not None:
        store = pg_store
    elif storage_backend == "file" or not has_db:
        store = FileStore(config=cfg)
    else:
        store = PgStore()

    svc = service or TurnService(store, config=cfg)
    if oauth_server:
        oa_server = oauth_server
        if oa_server._pg_store is None and isinstance(store, PgStore):
            oa_server._pg_store = store
        if oa_server.public_url is None:
            oa_server.public_url = cfg.public_url
    else:
        oa_server = OAuthServer(
            jwt_secret=cfg.jwt_secret,
            pg_store=store if isinstance(store, PgStore) else None,
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

        # 1. Connect database pool if using PgStore
        if isinstance(store, PgStore):
            await store.connect()

        # 2. Start background GitHub sync loop if configured
        sync_task = None
        gh_repo = os.environ.get(
            "THREADVAULT_GH_REPO", "https://github.com/yashdev4/thread_vault.git"
        ).strip()
        if gh_repo and isinstance(store, FileStore):
            from thread_save.export.sync_loop import start_github_sync_loop
            sync_interval = int(os.environ.get("THREADVAULT_SYNC_INTERVAL_SECONDS", "60"))
            sync_task = asyncio.create_task(
                start_github_sync_loop(
                    vault_root=cfg.vault_root,
                    repo=gh_repo,
                    token=os.environ.get("GITHUB_TOKEN") or os.environ.get("THREADVAULT_GH_TOKEN"),
                    interval_seconds=sync_interval,
                )
            )

        # 3. Enter MCP Streamable HTTP session manager lifespan
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            logger.info(
                "ThreadVault Web & MCP Streamable HTTP server started (Storage: %s)",
                type(store).__name__,
            )
            yield

        # 4. Cleanup background sync task
        if sync_task:
            sync_task.cancel()
            try:
                await sync_task
            except asyncio.CancelledError:
                pass

        # 5. Disconnect database pool
        if isinstance(store, PgStore):
            await store.close()
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

    # Thread viewer and .md download endpoints (§4, Milestone X5)
    if isinstance(store, PgStore):
        app.include_router(create_viewer_router(store))

    # Health check endpoint (§4.4, Milestone X8 prerequisite)
    @app.get("/health")
    async def health():
        if isinstance(store, PgStore):
            db_connected = False
            try:
                async with store.pool.acquire() as conn:
                    val = await conn.fetchval("SELECT 1")
                    db_connected = (val == 1)
            except Exception as e:
                logger.warning("Health check DB probe failed: %s", e)
                db_connected = False
            status = "healthy" if db_connected else "degraded"
            storage_type = "postgres"
        else:
            db_connected = True
            status = "healthy"
            storage_type = "filestore"

        return {
            "status": status,
            "storage": storage_type,
            "database": "connected" if db_connected else "disconnected",
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


def run_server(host: str = "0.0.0.0", port: int = 8000, **kwargs):
    """Run Uvicorn with platform proxy headers enabled (§pre-deploy safety)."""
    import uvicorn
    uvicorn.run(
        "thread_save.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        **kwargs,
    )
