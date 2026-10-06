"""HTTP Remote MCPServer setup with tool parity and stdio alignment (§2 S8, §4.1, Milestone H4).

Exposes all ThreadVault MCP tools over Streamable HTTP transport:
- vault_log_turn (B7; vault_save_turn only with THREADVAULT_LEGACY_SAVE_TURN=on)
- vault_backfill
- vault_find
- vault_stats

Ensures tool list, descriptions, annotations, and server instructions match stdio server.py.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional

from pydantic import Field

from thread_save.config import CaptureMode, legacy_save_turn_enabled
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import extract_snippet
from thread_save.tools.clients import ArchiveServer
from thread_save.tools.descriptions import (
    BACKFILL_DESC,
    DEST_REMOTE,
    REMOTE_TOOL_NAMES,
    REPLY_FIELD_DESC,
    log_turn_description,
    public_result,
    server_instructions,
    user_messages_only,
)
from thread_save.web.context import current_account_id
from thread_save.web.viewer import create_viewer_token

logger = logging.getLogger("thread_save.web.mcp_server")

# Try to import ToolAnnotations; fall back gracefully
try:
    from mcp.types import ToolAnnotations
    _HAS_ANNOTATIONS = True
except ImportError:
    _HAS_ANNOTATIONS = False

# B7 — Server instructions (echo-free, real destination: the server, not this computer)
_SERVER_INSTRUCTIONS = server_instructions(DEST_REMOTE, REMOTE_TOOL_NAMES["log_turn"])


def _server_instructions(capture: CaptureMode) -> str:
    return server_instructions(DEST_REMOTE, REMOTE_TOOL_NAMES["log_turn"], capture)


def _log_turn_desc(capture: CaptureMode, reply_max_chars: int = 8_000) -> str:
    return log_turn_description(DEST_REMOTE, capture, reply_max_chars)


_LOG_TURN_DESC = _log_turn_desc(CaptureMode.FULL)

# Legacy (B1 §6.2); listed only with THREADVAULT_LEGACY_SAVE_TURN=on
_SAVE_TURN_DESC = """\
Call at the start of every reply to archive this chat turn for the user. \
The user enabled this connector to keep a record of their chats in their ThreadVault account.

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
If earlier turns of this conversation are listed as missing, send them with vault_backfill.
If the result lists missing turns that are still in this conversation, send them with vault_backfill.
If the user asks you to save the whole chat, send all turns still in the conversation with vault_backfill.
If the user asks not to archive this conversation, stop calling this tool for the rest of it.
Repeated or duplicate calls are safe and never create duplicate entries."""

_BACKFILL_DESC = BACKFILL_DESC

_FIND_DESC = """\
Search saved ThreadVault threads by keyword or list recent threads. Returns metadata \
and short snippets only."""

_STATS_DESC = """\
Get coverage statistics for a ThreadVault thread. \
Reports turn counts by fidelity, gap status, and coverage percentage."""


def create_http_mcp_server(service: TurnService) -> ArchiveServer:
    """Create and configure MCPServer for HTTP transport with full stdio tool parity."""
    capture = getattr(getattr(service, "_config", None), "capture", CaptureMode.FULL)
    server = ArchiveServer(
        "threadvault-remote",
        write_tools={REMOTE_TOOL_NAMES[k] for k in ("log_turn", "save_turn", "backfill")},
        version="0.2.0",
        instructions=_server_instructions(capture),
    )

    # 1. vault_log_turn (B7 path E, E8 two calls per reply)
    reply_max = getattr(getattr(service, "_config", None), "reply_max_chars", 8_000)
    log_kwargs: dict = {"name": "vault_log_turn", "description": _log_turn_desc(capture, reply_max)}
    if _HAS_ANNOTATIONS:
        log_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    async def _log_turn(
        user_message: str,
        reply: str | None,
        thread_id: str | None,
        turn: int | None,
        prev_user_anchor: str | None,
        title_hint: str | None,
    ) -> dict:
        account = current_account_id.get()
        try:
            result = await service.log_turn(
                user_message=user_message,
                reply=reply,
                thread_id=thread_id,
                turn=turn,
                prev_user_anchor=prev_user_anchor,
                title_hint=title_hint,
                account=account,
                client="remote",
            )
            # P1-15 measurement: is the logged reply formatted or a flattened retelling?
            logger.info(
                "vault_log_turn n=%s action=%s reply_fidelity=%s reply_shape=%s",
                result.get("n"), result.get("action"),
                result.get("reply_fidelity"), result.get("reply_shape"),
            )
            return public_result(result, REMOTE_TOOL_NAMES["log_turn"])
        except Exception as e:
            logger.error("vault_log_turn error: %s", e)
            return {"ok": False, "code": "server_error", "retryable": True}

    async def vault_log_turn(
        user_message: str,
        reply: Annotated[str | None, Field(description=REPLY_FIELD_DESC)] = None,
        thread_id: str | None = None,
        turn: int | None = None,
        prev_user_anchor: str | None = None,
        title_hint: str | None = None,
    ) -> dict:
        return await _log_turn(user_message, reply, thread_id, turn, prev_user_anchor, title_hint)

    async def vault_log_turn_user_only(
        user_message: str,
        thread_id: str | None = None,
        turn: int | None = None,
        prev_user_anchor: str | None = None,
        title_hint: str | None = None,
    ) -> dict:
        return await _log_turn(user_message, None, thread_id, turn, prev_user_anchor, title_hint)

    # E-floor: in user_only mode the reply parameter is not part of the schema at all
    if capture == CaptureMode.USER_ONLY:
        server.tool(**log_kwargs)(vault_log_turn_user_only)
    else:
        server.tool(**log_kwargs)(vault_log_turn)

    # 1b. Legacy vault_save_turn
    save_kwargs: dict = {"description": _SAVE_TURN_DESC}
    if _HAS_ANNOTATIONS:
        save_kwargs["annotations"] = ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

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

    if legacy_save_turn_enabled():
        server.tool(**save_kwargs)(vault_save_turn)

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
                turns=user_messages_only(turns),
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
            is_titles_only = service.envelope_encryption_enabled
            hits = await service.find(
                query=query, limit=limit, account=account, titles_only=is_titles_only
            )
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

            search_mode = "titles_only" if is_titles_only else "full_text"
            result: dict[str, Any] = {
                "ok": True,
                "threads": threads,
                "count": len(threads),
                "search_mode": search_mode,
            }
            if len(threads) == 0:
                if is_titles_only and query:
                    result["notice"] = (
                        f"Envelope encryption enabled: searched thread titles only (turn bodies are encrypted). "
                        f"No matching thread titles found for '{query}'."
                    )
                elif query:
                    result["notice"] = f"No threads matching '{query}' found."
                else:
                    result["notice"] = "No threads found in vault."
            elif is_titles_only:
                result["notice"] = "Envelope encryption enabled: searched thread titles only."
            return result
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
