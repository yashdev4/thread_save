"""Which MCP clients get archived (P1-14).

Claude Code loads a user's claude.ai connectors into every coding session, and
ThreadVault's instructions then archived those sessions as if they were chats.
Chats (claude.ai web, desktop, mobile) are archived; coding-agent sessions are not.

The client is identified per connection from the MCP handshake (`clientInfo.name`)
and, over HTTP, the User-Agent header. For a skipped client the write tools are
left out of `tools/list`, and a call to one anyway is answered with a no-op
result, so nothing from that session is stored.

THREADVAULT_SKIP_CLIENTS: comma-separated, case-insensitive substrings matched
against "<clientInfo.name> <User-Agent>". Default "claude-code"; "none" archives
every client.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

import mcp.types as types
from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("thread_save.clients")

DEFAULT_SKIP_CLIENTS: tuple[str, ...] = ("claude-code",)

NOT_ARCHIVED_RESULT = {"ok": True, "skipped": "not_archived_client"}


def skipped_clients() -> tuple[str, ...]:
    raw = os.environ.get("THREADVAULT_SKIP_CLIENTS")
    if raw is None:
        return DEFAULT_SKIP_CLIENTS
    raw = raw.strip().lower()
    if raw in ("", "none", "off"):
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"


def client_identity(ctx: Any) -> tuple[str, str, str]:
    """(clientInfo.name, clientInfo.version, User-Agent) for a request context.

    Handshake protocol versions send clientInfo once, in `initialize`. From
    2026-07-28 there is no handshake and each request carries it in `_meta`
    (Claude Code 2.1 does this), so both places are read.
    """
    name = version = user_agent = ""
    params = getattr(getattr(ctx, "session", None), "client_params", None)
    info = getattr(params, "client_info", None)
    if info is not None:
        name = info.name or ""
        version = info.version or ""
    meta = getattr(ctx, "meta", None)
    meta_info = meta.get(_META_CLIENT_INFO) if isinstance(meta, dict) else None
    if not name and isinstance(meta_info, dict):
        name = str(meta_info.get("name") or "")
        version = str(meta_info.get("version") or "")
    headers = getattr(getattr(ctx, "request", None), "headers", None)
    if headers is not None:
        user_agent = headers.get("user-agent", "") or ""
    return name, version, user_agent


def is_skipped_client(name: str, user_agent: str = "", skip: Iterable[str] | None = None) -> bool:
    tokens = skipped_clients() if skip is None else tuple(skip)
    haystack = f"{name} {user_agent}".lower()
    return any(token in haystack for token in tokens)


class ArchiveServer(MCPServer):
    """MCPServer that withholds its write tools from clients that are not archived."""

    def __init__(self, *args: Any, write_tools: Iterable[str] = (), **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._write_tools = frozenset(write_tools)
        self._lowlevel_server.add_notification_handler(
            "notifications/initialized", types.NotificationParams, self._on_initialized
        )

    def _skipped(self, ctx: Any) -> tuple[bool, str]:
        name, version, user_agent = client_identity(ctx)
        label = f"{name}/{version}" if name else (user_agent or "unknown")
        return is_skipped_client(name, user_agent), label

    async def _on_initialized(self, ctx: Any, params: Any) -> None:
        # Who connects, so the skip list can be checked against real traffic
        name, version, user_agent = client_identity(ctx)
        logger.info(
            "client connected: name=%r version=%r user_agent=%r archived=%s",
            name, version, user_agent, not is_skipped_client(name, user_agent),
        )

    async def _handle_list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        result = await super()._handle_list_tools(ctx, params)
        skipped, label = self._skipped(ctx)
        if skipped:
            logger.info("tools/list for client %s: write tools withheld", label)
            result = types.ListToolsResult(
                tools=[t for t in result.tools if t.name not in self._write_tools]
            )
        else:
            logger.info("tools/list for client %s: archived", label)
        return result

    async def _handle_call_tool(self, ctx: Any, params: Any) -> Any:
        if params.name in self._write_tools:
            skipped, label = self._skipped(ctx)
            if skipped:
                logger.info("not archiving %s from client %s", params.name, label)
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps(NOT_ARCHIVED_RESULT))]
                )
        return await super()._handle_call_tool(ctx, params)
