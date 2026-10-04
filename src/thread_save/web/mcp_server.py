"""HTTP Remote MCPServer setup with tool parity and stdio alignment (§2 S8, §4.1, Milestone H4).

Exposes all ThreadVault MCP tools over Streamable HTTP transport:
- vault_save_turn
- vault_backfill
- vault_find
- vault_stats

Ensures tool list, descriptions, annotations, and server instructions match stdio server.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from mcp.server.mcpserver import MCPServer
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import extract_snippet
from thread_save.web.context import current_account_id
from thread_save.web.viewer import create_viewer_token

logger = logging.getLogger("thread_save.web.mcp_server")

# Try to import ToolAnnotations; fall back gracefully
try:
    from mcp.types import ToolAnnotations
    _HAS_ANNOTATIONS = True
except ImportError:
    _HAS_ANNOTATIONS = False

# Server instructions (matching stdio server.py §6.2)
_SERVER_INSTRUCTIONS = """\
ThreadVault keeps a local markdown archive of this user's conversations, which they turned on \
deliberately. At the start of each reply, call vault_save_turn as described in its tool \
description. If the user asks not to archive a conversation, stop calling it for that conversation."""

_SAVE_TURN_DESC = """\
Archive the conversation to the user's ThreadVault account. \
The user enabled this connector to keep a record of their chats.

Call this once at the start of each of your replies:
- user_query: the user's latest message, verbatim.
- prev_response: your previous reply in this conversation, verbatim. Omit on the first turn.
- prev_user_anchor: the first 80 characters of the user's previous message. Omit on the first turn.
- thread_id: the thread_id from the last result. Omit on the first turn.
- title_hint: a short descriptive title, first turn only.

If prev_response is longer than about 6000 characters, send it in parts with chunk_index and \
is_final instead of shortening it.
If a message contains passwords, API keys or similar secrets, replace them with [REDACTED] and \
set fidelity to "abridged".
If the result lists missing turns that are still in this conversation, send them with vault_backfill.
If the user asks you to save the whole chat, send all turns still in the conversation with vault_backfill.
If the user asks not to archive this conversation, stop calling this tool for the rest of it.
Repeated or duplicate calls are safe and never create duplicate entries."""

_BACKFILL_DESC = """\
Add earlier turns to a ThreadVault thread: turns listed as missing by vault_save_turn, or the \
whole conversation when the user asks to save it. Up to 10 turns per call; send more in further \
calls. Turns already archived are left unchanged, so resending is safe."""

_FIND_DESC = """\
Search saved ThreadVault threads by keyword or list recent threads. Returns metadata \
and short snippets only."""

_STATS_DESC = """\
Get coverage statistics for a ThreadVault thread: turn counts by fidelity, \
gap status, and coverage percentage."""


def create_http_mcp_server(service: TurnService) -> MCPServer:
    """Create and configure MCPServer for HTTP transport with full stdio tool parity."""
    server = MCPServer(
        "threadvault-remote",
        version="0.2.0",
        instructions=_SERVER_INSTRUCTIONS,
    )

    # 1. vault_save_turn
    save_kwargs: dict = {"description": _SAVE_TURN_DESC}
    if _HAS_ANNOTATIONS:
        save_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    @server.tool(**save_kwargs)
    async def vault_save_turn(
        user_query: str,
        thread_id: str | None = None,
        prev_response: str | None = None,
        prev_user_anchor: str | None = None,
        client_turn_number: int | None = None,
        title_hint: str | None = None,
        fidelity: str = "verbatim",
        chunk_index: int | None = None,
        is_final: bool = True,
        model_hint: str | None = None,
        current_response: str | None = None,
    ) -> dict:
        account = current_account_id.get()
        try:
            return await service.save_turn(
                user_query=user_query,
                prev_response=prev_response,
                prev_user_anchor=prev_user_anchor,
                thread_id=thread_id,
                client_turn_number=client_turn_number,
                title_hint=title_hint,
                fidelity_str=fidelity,
                chunk_index=chunk_index,
                is_final=is_final,
                model_hint=model_hint or "",
                current_response=current_response,
                account=account,
            )
        except Exception as e:
            logger.error("vault_save_turn error: %s", e)
            return {"ok": False, "code": "server_error", "retryable": True}

    # 2. vault_backfill
    backfill_kwargs: dict = {"description": _BACKFILL_DESC}
    if _HAS_ANNOTATIONS:
        backfill_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    @server.tool(**backfill_kwargs)
    async def vault_backfill(
        thread_id: str,
        turns: list[dict],
    ) -> dict:
        account = current_account_id.get()
        try:
            return await service.backfill(
                thread_id=thread_id,
                turns=turns,
                account=account,
            )
        except Exception as e:
            logger.error("vault_backfill error: %s", e)
            return {"ok": False, "code": "server_error", "retryable": True}

    # 3. vault_find
    find_kwargs: dict = {"description": _FIND_DESC}
    if _HAS_ANNOTATIONS:
        find_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=False,
        )

    @server.tool(**find_kwargs)
    async def vault_find(
        query: str | None = None,
        limit: int = 10,
    ) -> dict:
        account = current_account_id.get()
        try:
            hits = await service.find(query=query, limit=limit, account=account)
            threads = []
            for hit in hits:
                token = create_viewer_token(hit.thread_id, account)
                threads.append({
                    "thread_id": hit.thread_id,
                    "title": hit.title,
                    "snippet": extract_snippet(hit.snippet, max_chars=200),
                    "updated_at": hit.updated_at,
                    "viewer_url": f"/v/{token}",
                    "download_url": f"/download/{hit.thread_id}.md?token={token}",
                })
            return {"ok": True, "threads": threads, "count": len(threads)}
        except Exception as e:
            logger.error("vault_find error: %s", e)
            return {"ok": False, "code": "server_error", "retryable": True}

    # 4. vault_stats (Milestone H4 parity with stdio)
    stats_kwargs: dict = {"description": _STATS_DESC}
    if _HAS_ANNOTATIONS:
        stats_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=False,
        )

    @server.tool(**stats_kwargs)
    async def vault_stats(
        thread_id: str | None = None,
    ) -> dict:
        account = current_account_id.get()
        try:
            if thread_id is None:
                raw_hits = await service.find(limit=1, account=account)
                if not raw_hits:
                    return {"ok": False, "code": "no_threads", "retryable": False}
                thread_id = raw_hits[0].thread_id

            stats = await service.stats_async(thread_id, account=account)
            if stats is None:
                return {"ok": False, "code": "thread_not_found", "retryable": False}

            return {"ok": True, **stats}
        except Exception as e:
            logger.error("vault_stats error: %s", e)
            return {"ok": False, "code": "server_error", "retryable": True}

    return server
