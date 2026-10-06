"""ThreadVault MCP Server — reliability-hardened entry point.

§7.3 stdio hygiene: stdout → stderr before any import.
§I-1: Never-raise wrapper on every tool.
§I-2: Hot-path < 50ms; FTS updates off the hot path.
§I-4: Unique vault_* tool names.
§I-5: Results are tiny, data-only, no imperative sentences.
§I-7: No network, no reads outside vault, no stdout.
§I-8: Global kill switch (vault/.paused) and per-conversation opt-out.
§I-9: vault_find returns ≤200-char snippets only.
§6.2: Tool descriptions carry invariants.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback
from typing import Any

# ── §7.3 stdio hygiene — BEFORE any other import ──────────────────────────
_real_stdout = sys.stdout
sys.stdout = sys.stderr

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("thread_save")

# ── Now safe to import ────────────────────────────────────────────────────


from thread_save.config import (
    VaultConfig,
    legacy_save_turn_enabled,
    load_capture_mode,
    load_config,
)
from thread_save.index.sqlite_index import ThreadIndex
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import extract_snippet
from thread_save.storage.path_resolver import ensure_vault_structure
from thread_save.storage.writer import FileStore
from thread_save.telemetry.events import EventLogger, LatencyTimer
from thread_save.tools.clients import ArchiveServer
from thread_save.tools.descriptions import (
    BACKFILL_DESC,
    DEST_LOCAL,
    LOCAL_TOOL_NAMES,
    log_turn_description,
    public_result,
    server_instructions,
    user_messages_only,
)

# Try to import ToolAnnotations; fall back gracefully (§2 note)
try:
    from mcp.types import ToolAnnotations
    _HAS_ANNOTATIONS = True
except ImportError:
    _HAS_ANNOTATIONS = False

# ── Global state (lazy-init) ──────────────────────────────────────────────

_config: VaultConfig | None = None
_service: TurnService | None = None
_index: ThreadIndex | None = None
_events: EventLogger | None = None


def _init() -> tuple[VaultConfig, TurnService, ThreadIndex, EventLogger]:
    global _config, _service, _index, _events
    if _config is None:
        _config = load_config()
        ensure_vault_structure(_config)
        store = FileStore(_config)
        _index = ThreadIndex(_config)
        _events = EventLogger(_config.events_path, _config.events_max_bytes)
        _service = TurnService(store, config=_config, events=_events)
        _attach_file_log(_config)
        logger.info("ThreadVault initialised: %s", _config.vault_root)
    assert _service is not None and _index is not None and _events is not None
    return _config, _service, _index, _events


_file_log_attached = False


def _attach_file_log(config: VaultConfig) -> None:
    """B6 L0: Claude Desktop does not keep a stdio server's stderr, so also log to the vault."""
    global _file_log_attached
    if _file_log_attached:
        return
    try:
        handler = logging.handlers.RotatingFileHandler(
            config.index_dir / "server.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logging.getLogger("thread_save").addHandler(handler)
        _file_log_attached = True
    except OSError as e:
        logger.warning("Cannot open server.log: %s", e)


def _log_failure(tool: str, code: str, **fields: Any) -> None:
    """B6 L0: failed calls are recorded in events.jsonl too."""
    if _events is not None:
        _events.log(tool=tool, ok=False, code=code, **fields)


# ── Opt-out phrase detection (§6.5) ───────────────────────────────────────

_OPT_OUT_PHRASES = frozenset({
    "don't save this chat",
    "dont save this chat",
    "stop archiving",
    "don't log this",
    "dont log this",
    "stop logging",
    "don't archive this",
    "dont archive this",
})


def _check_opt_out(text: str) -> bool:
    """Check if user_query contains an opt-out phrase."""
    normalised = text.strip().lower()
    return any(phrase in normalised for phrase in _OPT_OUT_PHRASES)


# ── Never-raise wrapper (§I-1) ────────────────────────────────────────────

def _error_result(code: str, retryable: bool = False) -> dict:
    """Standard error result — tiny, no imperative sentences (§I-5)."""
    return {"ok": False, "code": code, "retryable": retryable}


# ── MCP Server ─────────────────────────────────────────────────────────────

# B7 — Server instructions (echo-free, real destination)
_SERVER_INSTRUCTIONS = server_instructions(DEST_LOCAL, LOCAL_TOOL_NAMES["log_turn"])

mcp = ArchiveServer(
    "threadvault",
    write_tools={LOCAL_TOOL_NAMES[k] for k in ("log_turn", "save_turn", "backfill")},
    version="0.2.0",
    instructions=_SERVER_INSTRUCTIONS,
)


# ── Tool 1: vault_log_turn (B7 path E) ────────────────────────────────────

_CAPTURE = load_capture_mode()
_LOG_TURN_DESC = log_turn_description(DEST_LOCAL, _CAPTURE)

_log_turn_kwargs: dict = {"description": _LOG_TURN_DESC}
if _HAS_ANNOTATIONS:
    _log_turn_kwargs["annotations"] = ToolAnnotations(
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
    try:
        config, service, index, events = _init()
        timer = LatencyTimer()
        with timer:
            result = await service.log_turn(
                user_message=user_message,
                reply=reply,
                thread_id=thread_id,
                turn=turn,
                prev_user_anchor=prev_user_anchor,
                title_hint=title_hint,
                account=config.default_account,
            )

        if result.get("ok") and result.get("action") in ("write", "merge"):
            # A merge only changed the reply of an already indexed turn (E2b)
            _index_queue.put((
                index, result["thread_id"], result["n"],
                user_message if result["action"] == "write" else "",
                reply if result.get("reply_fidelity") else None,
                title_hint, service.thread_index_info(result["thread_id"]),
            ))

        events.log(
            tool=LOCAL_TOOL_NAMES["log_turn"],
            thread_id=result.get("thread_id"),
            n=result.get("n"),
            mode=config.capture.value,
            binding=result.get("binding", "paused" if result.get("paused") else "unknown"),
            gap_size=len(result.get("not_logged") or []),
            fidelity=result.get("reply_fidelity") or "none",
            content_bytes=result.get("bytes", 0),
            latency_ms=timer.elapsed_ms,
            ok=result.get("ok", True),
            action=result.get("action", ""),
        )
        return public_result(result)

    except Exception:
        logger.error("vault_log_turn failed: %s", traceback.format_exc())
        _log_failure(LOCAL_TOOL_NAMES["log_turn"], "write_failed", thread_id=thread_id)
        return _error_result("write_failed", retryable=True)


async def vault_log_turn(
    user_message: str,
    reply: str | None = None,
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
if _CAPTURE.value == "user_only":
    mcp.tool(name=LOCAL_TOOL_NAMES["log_turn"], **_log_turn_kwargs)(vault_log_turn_user_only)
else:
    mcp.tool(name=LOCAL_TOOL_NAMES["log_turn"], **_log_turn_kwargs)(vault_log_turn)


# ── Legacy: vault_save_turn (B1 §6.2 text; listed only with THREADVAULT_LEGACY_SAVE_TURN=on) ──

# §6.2 — exact tool description
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

_save_turn_kwargs: dict = {"description": _SAVE_TURN_DESC}
if _HAS_ANNOTATIONS:
    _save_turn_kwargs["annotations"] = ToolAnnotations(
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
    try:
        config, service, index, events = _init()
        timer = LatencyTimer()

        with timer:
            result = await service.save_turn(
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
                account=config.default_account,
            )

        # Index turn in background (§I-2: off hot path)
        if result.get("ok") and result.get("n"):
            tid = result.get("thread_id", "")
            _index_queue.put((
                index, tid, result["n"], user_query, None, title_hint,
                service.thread_index_info(tid),
            ))

        # Telemetry (binding from the result, not the argument: P2-13)
        events.log(
            tool=LOCAL_TOOL_NAMES["save_turn"],
            thread_id=result.get("thread_id"),
            n=result.get("n"),
            mode=config.mode.value,
            binding=result.get("binding", "paused" if result.get("paused") else "unknown"),
            chunk=chunk_index is not None,
            fidelity=fidelity,
            content_bytes=len(user_query.encode("utf-8"))
            + len((prev_response or "").encode("utf-8")),
            latency_ms=timer.elapsed_ms,
            ok=result.get("ok", True),
            model_hint=model_hint or "",
            nudge=config.nudge.value,
            missing_reported=len(result.get("missing", [])),
        )

        return result

    except Exception as e:
        logger.error("vault_save_turn failed: %s", traceback.format_exc())
        _log_failure(LOCAL_TOOL_NAMES["save_turn"], "write_failed", thread_id=thread_id)
        return _error_result("write_failed", retryable=True)


if legacy_save_turn_enabled():
    mcp.tool(name=LOCAL_TOOL_NAMES["save_turn"], **_save_turn_kwargs)(vault_save_turn)


# ── Tool 2: vault_backfill (§5) ────────────────────────────────────────────

_BACKFILL_DESC = BACKFILL_DESC

_backfill_kwargs: dict = {"description": _BACKFILL_DESC}
if _HAS_ANNOTATIONS:
    _backfill_kwargs["annotations"] = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


@mcp.tool(name=LOCAL_TOOL_NAMES["backfill"], **_backfill_kwargs)
async def vault_backfill(
    thread_id: str,
    turns: list[dict],
) -> dict:
    try:
        config, service, index, events = _init()

        timer = LatencyTimer()
        with timer:
            result = await service.backfill(
                thread_id=thread_id,
                turns=user_messages_only(turns),
                account=config.default_account,
            )

        events.log(
            tool=LOCAL_TOOL_NAMES["backfill"],
            thread_id=thread_id,
            recovered=len(result.get("stored", [])),
            latency_ms=timer.elapsed_ms,
            ok=result.get("ok", True),
            code=result.get("code", ""),
        )

        return result

    except Exception as e:
        logger.error("vault_backfill failed: %s", traceback.format_exc())
        _log_failure(LOCAL_TOOL_NAMES["backfill"], "write_failed", thread_id=thread_id)
        return _error_result("write_failed", retryable=True)


# ── Tool 3: vault_find (§I-9: snippets only, never full turns) ────────────

_FIND_DESC = """\
Search saved ThreadVault threads by keyword or list recent threads. Returns metadata \
and short snippets only."""

_find_kwargs: dict = {"description": _FIND_DESC}
if _HAS_ANNOTATIONS:
    _find_kwargs["annotations"] = ToolAnnotations(
        readOnlyHint=True,
        openWorldHint=False,
    )


@mcp.tool(name=LOCAL_TOOL_NAMES["find"], **_find_kwargs)
async def vault_find(
    query: str | None = None,
    limit: int = 10,
) -> dict:
    try:
        config, service, index, events = _init()

        timer = LatencyTimer()
        with timer:
            is_titles_only = service.envelope_encryption_enabled
            if is_titles_only:
                # Envelope encryption mode: bypass SQLite FTS body index and search titles only
                raw_hits = await service.find(query=query, limit=limit, titles_only=True)
                results = [
                    {
                        "thread_id": h.thread_id,
                        "title": h.title,
                        "snippet": h.snippet,
                        "updated_at": h.updated_at,
                    }
                    for h in raw_hits
                ]
            else:
                # SQLite FTS first
                results = index.search_threads(query=query, limit=limit)

                # Fallback to store
                if not results:
                    raw_hits = await service.find(query=query, limit=limit)
                    results = [
                        {
                            "thread_id": h.thread_id,
                            "title": h.title,
                            "snippet": h.snippet,
                            "updated_at": h.updated_at,
                        }
                        for h in raw_hits
                    ]

            # §I-9: Enforce snippet length
            for r in results:
                if "snippet" in r and len(r["snippet"]) > 200:
                    r["snippet"] = r["snippet"][:197] + "..."

        search_mode = "titles_only" if is_titles_only else "full_text"
        res: dict[str, Any] = {
            "ok": True,
            "threads": results,
            "count": len(results),
            "search_mode": search_mode,
        }
        if len(results) == 0:
            if is_titles_only and query:
                res["notice"] = (
                    f"Envelope encryption enabled: searched thread titles only (turn bodies are encrypted). "
                    f"No matching thread titles found for '{query}'."
                )
            elif query:
                res["notice"] = f"No threads matching '{query}' found."
            else:
                res["notice"] = "No threads found in vault."
        elif is_titles_only:
            res["notice"] = "Envelope encryption enabled: searched thread titles only."

        events.log(
            tool=LOCAL_TOOL_NAMES["find"],
            latency_ms=timer.elapsed_ms,
            ok=True,
        )

        return res

    except Exception as e:
        logger.error("vault_find failed: %s", traceback.format_exc())
        return _error_result("search_failed", retryable=True)


# ── Tool 4: vault_stats (§7) ──────────────────────────────────────────────

_STATS_DESC = """\
Get coverage statistics for a ThreadVault thread. \
Reports turn counts by fidelity, gap status, and coverage percentage."""

_stats_kwargs: dict = {"description": _STATS_DESC}
if _HAS_ANNOTATIONS:
    _stats_kwargs["annotations"] = ToolAnnotations(
        readOnlyHint=True,
        openWorldHint=False,
    )


@mcp.tool(name=LOCAL_TOOL_NAMES["stats"], **_stats_kwargs)
async def vault_stats(
    thread_id: str | None = None,
) -> dict:
    try:
        config, service, index, events = _init()

        if thread_id is None:
            raw_hits = await service.find(limit=1)
            if not raw_hits:
                return _error_result("no_threads")
            thread_id = raw_hits[0].thread_id

        stats = service.stats(thread_id)
        if stats is None:
            return _error_result("thread_not_found")

        return {"ok": True, **stats}

    except Exception as e:
        logger.error("vault_stats failed: %s", traceback.format_exc())
        return _error_result("stats_failed", retryable=True)


# ── FTS Indexing Helper (off hot path, §I-2) ──────────────────────────────


import threading, queue
_index_queue = queue.Queue()
def _index_worker():
    while True:
        item = _index_queue.get()
        if item is None: break
        try:
            _do_index(*item)
        except Exception:
            pass
        _index_queue.task_done()
threading.Thread(target=_index_worker, daemon=True).start()

def _do_index(
    index: ThreadIndex,
    thread_id: str,
    n: int,
    user_text: str,
    reply_text: str | None,
    title_hint: str | None,
    info: dict,
) -> None:
    """Best-effort FTS indexing. Never blocks the hot path.

    Uses the thread's real title/account/path; an empty title_hint must not
    overwrite the title of an existing thread (P2-12).
    """
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        title = info.get("title") or title_hint or ""
        if title:
            index.upsert_thread(
                thread_id=thread_id,
                title=title,
                slug=info.get("slug", ""),
                account=info.get("account", ""),
                path=info.get("path", ""),
                created=info.get("created") or now,
                updated=info.get("updated") or now,
            )
        if user_text:
            index.index_turn(thread_id, n, "user", user_text, title=title)
        if reply_text:
            index.index_turn(thread_id, n, "assistant", reply_text, title=title)
    except Exception:
        pass


# ── Entry Point ────────────────────────────────────────────────────────────

def main():
    """Run the MCP server over stdio."""
    sys.stdout = _real_stdout
    # P1-14: log from startup, so which client connected is in server.log before any save
    try:
        config = load_config()
        ensure_vault_structure(config)
        _attach_file_log(config)
    except Exception as e:
        logger.warning("server.log not attached at startup: %s", e)
    logger.info("ThreadVault MCP server starting (v0.2.0)...")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
