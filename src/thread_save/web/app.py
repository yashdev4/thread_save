"""FastAPI Application mounting FastMCP over Streamable HTTP (§4, Milestone X3)."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from thread_save.config import VaultConfig, load_config
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.web.mcp_server import create_http_mcp_server
from thread_save.web.middleware import (
    AccountContextMiddleware,
    BodySizeLimitMiddleware,
    OriginValidatorMiddleware,
    RateLimitMiddleware,
    RateLimiter,
)

logger = logging.getLogger("thread_save.web.app")


def create_app(
    pg_store: Optional[PgStore] = None,
    service: Optional[TurnService] = None,
    config: Optional[VaultConfig] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> FastAPI:
    """Create and configure FastAPI application with Streamable HTTP MCP server."""
    cfg = config or load_config()
    store = pg_store or PgStore()
    svc = service or TurnService(store, config=cfg)

    mcp_server = create_http_mcp_server(svc)
    mcp_asgi = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 1. Connect database pool
        if isinstance(store, PgStore):
            await store.connect()
        # 2. Enter MCP Streamable HTTP session manager lifespan
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            logger.info("ThreadVault Web & MCP Streamable HTTP server started")
            yield
        # 3. Disconnect database pool
        if isinstance(store, PgStore):
            await store.close()
        logger.info("ThreadVault Web & MCP Streamable HTTP server stopped")

    app = FastAPI(
        title="ThreadVault Remote MCP Server",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Security and rate-limiting middlewares (order matters: outer to inner)
    app.add_middleware(AccountContextMiddleware)
    app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(OriginValidatorMiddleware)

    # Health check endpoint (§4.4, Milestone X8 prerequisite)
    @app.get("/health")
    async def health():
        db_connected = False
        if isinstance(store, PgStore):
            try:
                async with store.pool.acquire() as conn:
                    val = await conn.fetchval("SELECT 1")
                    db_connected = (val == 1)
            except Exception as e:
                logger.warning("Health check DB probe failed: %s", e)
                db_connected = False
        else:
            db_connected = True

        status = "healthy" if db_connected else "degraded"
        return {
            "status": status,
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
