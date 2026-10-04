"""HTTP Remote MCPServer setup with honest remote tool descriptions (§2 S8, §4.1)."""

from __future__ import annotations

import logging
from typing import Optional

from mcp.server.mcpserver import MCPServer
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import extract_snippet
from thread_save.web.context import current_account_id

logger = logging.getLogger("thread_save.web.mcp_server")

# Try to import ToolAnnotations; fall back gracefully
try:
    from mcp.types import ToolAnnotations
    _HAS_ANNOTATIONS = True
except ImportError:
    _HAS_ANNOTATIONS = False

# §2 S8 — Honest remote tool descriptions
_REMOTE_SERVER_INSTRUCTIONS = """\
ThreadVault keeps a secure cloud archive of this user's conversations on ThreadVault, which they turned on \
deliberately. At the start of each reply, call vault_save_turn as described in its tool \
description. If the user asks not to archive a conversation, stop calling it for that conversation."""

_REMOTE_SAVE_TURN_DESC = """\
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

_REMOTE_BACKFILL_DESC = """\
Repair missed turns in this conversation from context.
Pass turns as a list of objects, each with:
- n: turn number
- user_query: user's message for that turn
- assistant_response: your response for that turn (optional)
- fidelity: "verbatim" or "abridged"
Call this when vault_save_turn reports missing turns, or when the user asks to save the whole chat."""

_REMOTE_FIND_DESC = """\
Search past conversations in your ThreadVault account.
Returns matching thread titles and short excerpts (≤200 characters).
Never returns full conversation text. Use this to check whether a topic was discussed previously."""


def create_http_mcp_server(service: TurnService) -> MCPServer:
    """Create and configure MCPServer for HTTP transport."""
    server = MCPServer(
        "threadvault-remote",
        version="0.2.0",
        instructions=_REMOTE_SERVER_INSTRUCTIONS,
    )

    save_kwargs: dict = {"description": _REMOTE_SAVE_TURN_DESC}
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

    backfill_kwargs: dict = {"description": _REMOTE_BACKFILL_DESC}
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

    find_kwargs: dict = {"description": _REMOTE_FIND_DESC}
    if _HAS_ANNOTATIONS:
        find_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    @server.tool(**find_kwargs)
    async def vault_find(
        query: str,
        limit: int = 5,
    ) -> dict:
        account = current_account_id.get()
        try:
            hits = await service.find(query=query, limit=limit, account=account)
            threads = []
            for hit in hits:
                threads.append({
                    "thread_id": hit.thread_id,
                    "title": hit.title,
                    "snippet": extract_snippet(hit.snippet, max_chars=200),
                    "updated_at": hit.updated_at,
                })
            return {"ok": True, "threads": threads, "count": len(threads)}
        except Exception as e:
            logger.error("vault_find error: %s", e)
            return {"ok": False, "code": "server_error", "retryable": True}

    return server
