"""Store-agnostic TurnService (§4.1).

Contains all protocol logic:
- Anchor-based thread binding
- Turn key generation and duplicate check
- n turn-number assignment and gap detection
- Chunk buffering and assembly
- Server-side truncation (limit: 100,000 characters, Fidelity.TRUNCATED)
- Redaction order: canonicalise -> redact -> hash -> store
- Transaction scoping: opens exactly one thread_txn per call
"""

from __future__ import annotations

import asyncio
import inspect
import re
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from thread_save.config import CaptureMode, VaultConfig, load_config
from thread_save.models import Fidelity, MAX_BODY_CHARS, SlotKey
from thread_save.security.idempotency import compute_content_hash
from thread_save.security.redactor import redact_text
from thread_save.storage.formatter import format_open_body
from thread_save.storage.identity import normalise_anchor
from thread_save.storage.protocol import Store, Stub, ThreadHit, ThreadStats
from thread_save.telemetry.events import EventLogger

logger = logging.getLogger("thread_save.service")

# Opt-out phrases (§6.5)
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


def check_opt_out(text: str) -> bool:
    """Check if text contains an opt-out phrase."""
    normalised = text.strip().lower()
    return any(phrase in normalised for phrase in _OPT_OUT_PHRASES)


# A whole line in brackets that describes content instead of being it, e.g.
# "[Extended breakdown covering failure scenarios ...]" (P1-12). Links end in ")".
_SUMMARY_LINE_RE = re.compile(r"^\s*\[[^\[\]]{30,}\]\s*$", re.M)

# log_turn never creates more not_logged stubs than this in one call
_MAX_NOT_LOGGED_PER_CALL = 50


async def _is_same_turn(
    txn, slots, thread_id: str, n: int,
    user_message: str, prev_user_anchor: str | None, turn: int | None,
) -> bool:
    """B7 E2b: is this log_turn call another call for turn n, the latest turn?

    The user message must match turn n's. The previous-message anchor then tells a
    repeat call (anchor = turn n-1) from a new turn that repeats the same words
    (anchor = turn n). Without an anchor, only cases that cannot be a new turn
    match: the first turn, a turn with no reply yet, or `turn` naming n itself.
    A doubtful case is appended, which can duplicate but never loses a reply.
    """
    if n < 1 or await txn.match_anchor(user_message) != n:
        return False
    if normalise_anchor(prev_user_anchor or ""):
        return n > 1 and await txn.match_anchor(prev_user_anchor) == n - 1
    if n == 1 or turn == n:
        return True
    reply = slots.get(thread_id, SlotKey(n, "assistant"))
    return reply is None or reply.fidelity in (Fidelity.OPEN, Fidelity.STUB)


def classify_reply(reply: str) -> Fidelity:
    """B7 E-truth: a reply sent back by the model is never stored as verbatim."""
    if "[REDACTED]" in reply or _SUMMARY_LINE_RE.search(reply):
        return Fidelity.ABRIDGED
    return Fidelity.REPORTED


class _ChunkBuffer:
    """In-memory reassembly buffer for chunked responses (§4.3)."""

    def __init__(self, timeout_seconds: int = 300):
        self._timeout = timeout_seconds
        self._buffers: dict[tuple[str, int], dict] = {}

    def add_chunk(
        self,
        thread_id: str,
        n: int,
        chunk_index: int,
        text: str,
        is_final: bool,
    ) -> Optional[str]:
        key = (thread_id, n)
        now = datetime.now(timezone.utc).timestamp()

        if key not in self._buffers:
            self._buffers[key] = {
                "chunks": {},
                "started_at": now,
                "has_final": False,
                "final_index": -1,
            }

        buf = self._buffers[key]
        buf["chunks"][chunk_index] = text
        if is_final:
            buf["has_final"] = True
            buf["final_index"] = chunk_index

        if buf["has_final"]:
            total = buf["final_index"] + 1
            if len(buf["chunks"]) == total:
                parts = [buf["chunks"][i] for i in range(total)]
                del self._buffers[key]
                return "".join(parts)

        return None


class TurnService:
    """Store-agnostic coordinator for ThreadVault protocol actions."""

    def __init__(
        self,
        store: Store,
        config: VaultConfig | None = None,
        events: EventLogger | None = None,
        github_target: Optional[Any] = None,
    ):
        self._store = store
        self._config = config or load_config()
        self._events = events
        self._chunks = _ChunkBuffer(self._config.chunk_timeout_seconds)
        self._github_target = github_target

    @property
    def store(self) -> Store:
        return self._store

    @property
    def _registry(self):
        return getattr(self._store, "_registry", None)

    @property
    def _slots(self):
        return getattr(self._store, "_slots", None)

    @property
    def gap_tracker(self):
        return getattr(self._store, "gap_tracker", None)

    def get_thread_stats(self, thread_id: str) -> Optional[dict]:
        if hasattr(self._store, "get_thread_stats"):
            return self._store.get_thread_stats(thread_id)
        return None

    def get_all_threads(self, account_id: str = "") -> list[dict]:
        if hasattr(self._store, "get_all_threads"):
            return self._store.get_all_threads(account_id)
        return []

    async def save_turn(
        self,
        user_query: str,
        prev_response: str | None = None,
        prev_user_anchor: str | None = None,
        thread_id: str | None = None,
        client_turn_number: int | None = None,
        title_hint: str | None = None,
        fidelity_str: str = "verbatim",
        chunk_index: int | None = None,
        is_final: bool = True,
        model_hint: str = "",
        current_response: str | None = None,
        account: str | None = None,
        client: str = "claude-desktop",
    ) -> dict:
        """Main protocol method for saving a conversation turn."""
        account = account or self._config.default_account

        # 1. Global pause check
        if self._config.is_paused:
            return {"ok": True, "paused": True}

        # 2. Per-thread pause / opt-out
        if check_opt_out(user_query):
            if thread_id:
                self.pause_thread(thread_id, account)
            return {"ok": True, "paused": True}

        if thread_id and self.is_thread_paused(thread_id, account):
            return {"ok": True, "paused": True}

        # 3. Thread binding
        bind_res = await self._store.bind_thread(account, thread_id, prev_user_anchor)
        bound_id = bind_res.thread_id
        binding = bind_res.method

        if not bound_id:
            meta = await self._store.create_thread(
                account, title_hint or "Untitled Thread", client=client
            )
            bound_id = meta.thread_id
            binding = "new"

        if await self._store.is_tombstoned(account, bound_id):
            return {"ok": True, "thread_id": bound_id, "tombstoned": True}

        # 4. Chunk assembly
        prev_fidelity = (
            Fidelity.ABRIDGED if fidelity_str == "abridged" else Fidelity.VERBATIM
        )
        if chunk_index is not None and prev_response is not None:
            assembled = self._chunks.add_chunk(
                bound_id, 0, chunk_index, prev_response, is_final
            )
            if assembled is None:
                return {
                    "ok": True,
                    "thread_id": bound_id,
                    "n": 0,
                    "chunk_buffered": True,
                }
            prev_response = assembled
            if not is_final:
                prev_fidelity = Fidelity.TRUNCATED

        # 5. Server-side size limits (never reject; store prefix with Fidelity.TRUNCATED)
        # Limit: 100,000 characters (MAX_BODY_CHARS)
        user_fidelity = Fidelity.VERBATIM
        if len(user_query) > MAX_BODY_CHARS:
            user_query = user_query[:MAX_BODY_CHARS]
            user_fidelity = Fidelity.TRUNCATED

        if prev_response is not None and len(prev_response) > MAX_BODY_CHARS:
            prev_response = prev_response[:MAX_BODY_CHARS]
            prev_fidelity = Fidelity.TRUNCATED

        curr_fidelity = Fidelity.VERBATIM
        if current_response is not None and len(current_response) > MAX_BODY_CHARS:
            current_response = current_response[:MAX_BODY_CHARS]
            curr_fidelity = Fidelity.TRUNCATED

        # 6. Redaction (Order: canonicalise -> redact -> hash -> store)
        if self._config.redaction_enabled:
            uq_stored = redact_text(user_query).text
            prev_stored = redact_text(prev_response).text if prev_response is not None else None
            curr_stored = redact_text(current_response).text if current_response is not None else None
        else:
            uq_stored = user_query
            prev_stored = prev_response
            curr_stored = current_response

        # 7. Turn key computation (plain SHA-256 hash_v1)
        user_anchor = normalise_anchor(user_query)
        norm_prev = normalise_anchor(prev_user_anchor or "", max_chars=1_000_000)
        norm_query = normalise_anchor(uq_stored, max_chars=1_000_000)
        prev_resp_hash = compute_content_hash(prev_stored or "")
        client_turn_str = str(client_turn_number) if client_turn_number is not None else ""
        turn_key_input = f"{norm_prev}|{norm_query}|{prev_resp_hash}|{client_turn_str}"
        turn_key = compute_content_hash(turn_key_input)

        # Check offloaded thread handling (§1 G7)
        bound_id, binding, orig_bound_id, dedup_n = await self._resolve_offloaded(
            account, bound_id, binding, turn_key, title_hint, client
        )
        if dedup_n is not None:
            return {
                "ok": True,
                "thread_id": bound_id,
                "action": "no_op",
                "n": dedup_n,
                "binding": binding,
            }

        # 8. Single transaction for all writes (§4.1)
        async with self._store.thread_txn(account, bound_id) as txn:
            slots = await txn.load_slots()
            highest = slots.highest_n(bound_id)
            gap_ns: list[int] = []

            # Idempotency check via turn_key
            existing_n = slots.find_turn_key(bound_id, turn_key)
            if existing_n is not None:
                n = existing_n
            else:
                # Anchor matching
                anchor_n = None
                if prev_user_anchor:
                    if hasattr(txn, "match_anchor"):
                        anchor_n = await txn.match_anchor(prev_user_anchor)
                    elif hasattr(self._store, "_registry"):
                        entry = self._store._registry.get(bound_id)
                        if entry:
                            norm = normalise_anchor(prev_user_anchor)
                            matches = [m for m, a in entry.anchor_map.items() if a == norm]
                            if len(matches) == 1:
                                anchor_n = matches[0]

                if anchor_n is not None:
                    n = anchor_n + 1
                    if client_turn_number is not None and client_turn_number > n:
                        gap_ns = list(range(n, client_turn_number))
                        n = client_turn_number
                    elif n > highest + 1:
                        gap_ns = list(range(highest + 1, n))
                else:
                    n = highest + 1
                    if client_turn_number is not None and client_turn_number > n:
                        gap_ns = list(range(n, client_turn_number))
                        n = client_turn_number

            # Register and write stubs for any gaps
            if gap_ns:
                await txn.record_gaps(gap_ns)
                stubs = [Stub(n=g, anchor=user_anchor) for g in gap_ns]
                await txn.write_stubs(stubs)

            # Write prev_response into (n-1, assistant) if present
            if prev_stored is not None and n > 1:
                await txn.upsert_turn(
                    n=n - 1,
                    role="assistant",
                    body=prev_stored,
                    fidelity=prev_fidelity,
                    model=model_hint,
                )

            # Write user turn into (n, user)
            await txn.upsert_turn(
                n=n,
                role="user",
                body=uq_stored,
                fidelity=user_fidelity,
                anchor=user_anchor,
                turn_key=turn_key,
            )

            # Handle assistant slot
            if curr_stored is not None:
                await txn.upsert_turn(
                    n=n,
                    role="assistant",
                    body=curr_stored,
                    fidelity=curr_fidelity,
                    model=model_hint,
                )
                await txn.update_thread_meta(open_turn=None)
            else:
                await txn.upsert_turn(
                    n=n,
                    role="assistant",
                    body=format_open_body(),
                    fidelity=Fidelity.OPEN,
                    model=model_hint,
                )
                await txn.update_thread_meta(open_turn=n)

            await txn.enqueue_export()

        # 9. Build response
        result: dict = {
            "ok": True,
            "thread_id": bound_id,
            "n": n,
            "binding": binding,
        }
        if binding == "continuation" and orig_bound_id:
            result["continues"] = orig_bound_id

        # Check for open gaps to report back
        if hasattr(self._store, "report_missing"):
            missing = await self._store.report_missing(
                account, bound_id, limit=self._config.max_missing_reported
            )
            if missing:
                result["missing"] = missing
        elif hasattr(self._store, "_gaps"):
            missing = self._store._gaps.report_missing(
                bound_id, limit=self._config.max_missing_reported
            )
            if missing:
                result["missing"] = missing

        if self._config.nudge == self._config.nudge.ON:
            result["log_next_turn"] = True

        return result

    async def _resolve_offloaded(
        self,
        account: str,
        bound_id: str,
        binding: str,
        turn_key: str,
        title_hint: str | None,
        client: str,
    ) -> tuple[str, str, str | None, int | None]:
        """Bring an offloaded thread back, or open a continuation thread (§1 G7).

        Returns (thread_id, binding, original_thread_id, dedup_n). A non-None
        dedup_n means the turn is already archived and the call is a no-op.
        """
        offloaded_threads = getattr(self._store, "offloaded_threads", {})
        if bound_id not in offloaded_threads:
            return bound_id, binding, None, None

        offload_info = offloaded_threads[bound_id]
        # Dedup check via pointer's recent_turn_keys
        if turn_key in offload_info.get("recent_turn_keys", []):
            return bound_id, binding, None, offload_info.get("max_n", 1)

        # Resumed conversation -> attempt rehydration
        rehydrated = False
        target = getattr(self, "_github_target", None) or getattr(self._store, "_github_target", None)
        if target and hasattr(self._store, "offload_manager"):
            rehydrated = await self._store.offload_manager.rehydrate_thread(
                self._store, bound_id, target
            )
        if rehydrated:
            return bound_id, "rehydrated", None, None

        # Continuation thread fallback (§1 G7):
        # If rehydrate fails (GitHub unreachable, token expired, hash mismatch),
        # create a continuation thread with continues: <thread_id> in front matter,
        # return ok: true. Never block save or corrupt partial restore.
        logger.warning("Rehydration failed for %s; creating continuation thread", bound_id)
        cont_meta = await self._store.create_thread(
            account,
            title_hint=title_hint or offload_info.get("title") or "Continuation Thread",
            client=client,
            continues=bound_id,
        )
        return cont_meta.thread_id, "continuation", bound_id, None

    async def log_turn(
        self,
        user_message: str,
        reply: str | None = None,
        thread_id: str | None = None,
        turn: int | None = None,
        prev_user_anchor: str | None = None,
        title_hint: str | None = None,
        model_hint: str = "",
        account: str | None = None,
        client: str = "claude-desktop",
    ) -> dict:
        """Archive one complete turn after the reply is written (B7 path E).

        One call writes (n, user) and (n, assistant) in one transaction. The model
        is never asked for earlier output: the server issues thread_id and
        next_turn, and turns that were never logged become `not_logged` stubs.
        A second call for the latest turn fills or replaces its reply instead of
        adding a turn (E2b), so each reply is stored once.
        Returns an internal dict; tool wrappers expose only ok/thread_id/next_turn.
        """
        account = account or self._config.default_account

        if self._config.is_paused:
            return {"ok": True, "paused": True}
        if check_opt_out(user_message):
            if thread_id:
                self.pause_thread(thread_id, account)
            return {"ok": True, "paused": True}
        if thread_id and self.is_thread_paused(thread_id, account):
            return {"ok": True, "paused": True}

        # E-floor: in user_only mode nothing the model wrote is stored
        if self._config.capture == CaptureMode.USER_ONLY:
            reply = None

        # Content limits. User text keeps the global cap; the reply has the
        # smaller echo budget (E-budget) and is never claimed verbatim (E-truth).
        user_fidelity = Fidelity.VERBATIM
        if len(user_message) > MAX_BODY_CHARS:
            user_message = user_message[:MAX_BODY_CHARS]
            user_fidelity = Fidelity.TRUNCATED

        reply_fidelity: Fidelity | None = None
        if reply is not None:
            reply_fidelity = classify_reply(reply)
            if len(reply) > self._config.reply_max_chars:
                reply = reply[: self._config.reply_max_chars]
                reply_fidelity = Fidelity.TRUNCATED

        # Turn key from the raw text: redaction masks can change between
        # processes, and a retry after a restart must still be a no-op.
        norm_prev = normalise_anchor(prev_user_anchor or "", max_chars=1_000_000)
        norm_user = normalise_anchor(user_message, max_chars=1_000_000)
        turn_str = str(turn) if turn is not None else ""
        turn_key = compute_content_hash(
            f"log1|{turn_str}|{norm_prev}|{norm_user}|{compute_content_hash(reply or '')}"
        )
        user_anchor = normalise_anchor(user_message)

        if self._config.redaction_enabled:
            user_stored = redact_text(user_message).text
            reply_stored = redact_text(reply).text if reply is not None else None
        else:
            user_stored = user_message
            reply_stored = reply

        bind_res = await self._store.bind_thread(account, thread_id, prev_user_anchor)
        bound_id = bind_res.thread_id
        binding = bind_res.method
        if not bound_id:
            meta = await self._store.create_thread(
                account, title_hint or "Untitled Thread", client=client
            )
            bound_id = meta.thread_id
            binding = "new"

        if await self._store.is_tombstoned(account, bound_id):
            return {"ok": True, "thread_id": bound_id, "tombstoned": True}

        bound_id, binding, orig_bound_id, dedup_n = await self._resolve_offloaded(
            account, bound_id, binding, turn_key, title_hint, client
        )
        if dedup_n is not None:
            return {
                "ok": True, "thread_id": bound_id, "n": dedup_n,
                "next_turn": dedup_n + 1, "binding": binding, "action": "no_op",
            }

        gap_ns: list[int] = []
        action = "no_op"
        async with self._store.thread_txn(account, bound_id) as txn:
            slots = await txn.load_slots()
            existing_n = slots.find_turn_key(bound_id, turn_key)
            highest = slots.highest_n(bound_id)
            if existing_n is not None:
                # Retry of a call that already committed: change nothing (W-4)
                n = existing_n
            elif await _is_same_turn(txn, slots, bound_id, highest, user_message, prev_user_anchor, turn):
                # E2b: a second call for the latest turn (called early and again at
                # the end, or the user pressed Retry). One turn per reply: the newest
                # reply fills or replaces that turn's reply; nothing is appended.
                n = highest
                if reply_stored is not None and reply_fidelity is not None:
                    res = await txn.upsert_turn(
                        n=n,
                        role="assistant",
                        body=reply_stored,
                        fidelity=reply_fidelity,
                        model=model_hint,
                        newest_wins=True,
                    )
                    if res.action != "no_op":
                        action = "merge"
                        await txn.enqueue_export()
            else:
                action = "write"
                n = highest + 1
                # A stale or absurd `turn` never overwrites or floods the thread
                if turn is not None and highest + 1 < turn <= highest + 1 + _MAX_NOT_LOGGED_PER_CALL:
                    gap_ns = list(range(highest + 1, turn))
                    n = turn

                if gap_ns:
                    await txn.record_gaps(gap_ns)
                    await txn.write_stubs([Stub(n=g, anchor="") for g in gap_ns])

                await txn.upsert_turn(
                    n=n,
                    role="user",
                    body=user_stored,
                    fidelity=user_fidelity,
                    anchor=user_anchor,
                    turn_key=turn_key,
                )
                if reply_stored is not None and reply_fidelity is not None:
                    await txn.upsert_turn(
                        n=n,
                        role="assistant",
                        body=reply_stored,
                        fidelity=reply_fidelity,
                        model=model_hint,
                    )
                await txn.enqueue_export()

        # not_logged gaps are recorded for stats but never reported back:
        # nothing asks the model to resend earlier turns (E-gaps)
        tracker = self.gap_tracker
        if gap_ns and tracker is not None and hasattr(tracker, "mark_requested"):
            for g in gap_ns:
                tracker.mark_requested(bound_id, g)

        result: dict = {
            "ok": True,
            "thread_id": bound_id,
            "n": n,
            "next_turn": n + 1,
            "binding": binding,
            "action": action,
            "not_logged": gap_ns,
            "reply_fidelity": reply_fidelity.value if reply_fidelity else None,
            "bytes": len((user_stored or "").encode("utf-8"))
            + len((reply_stored or "").encode("utf-8")),
        }
        if orig_bound_id:
            result["continues"] = orig_bound_id
        return result

    def thread_index_info(self, thread_id: str) -> dict:
        """Title/slug/account/path of a thread, for the local search index (P2-12)."""
        registry = self._registry
        entry = registry.get(thread_id) if registry is not None else None
        if entry is None:
            return {}
        ps = getattr(self._store, "_page_states", {}).get(thread_id)
        return {
            "title": entry.meta.title,
            "slug": entry.meta.slug,
            "account": entry.meta.account,
            "path": ps.file_path if ps else "",
            "created": entry.meta.created,
            "updated": entry.meta.updated,
        }

    async def backfill(
        self,
        thread_id: str,
        turns: list[dict],
        account: str | None = None,
    ) -> dict:
        """Add earlier turns to repair gaps (§2.3)."""
        account = account or self._config.default_account
        if len(turns) > self._config.max_backfill_per_call:
            return {"ok": False, "code": "too_many", "retryable": False}

        stored: list[int] = []
        skipped: list[int] = []

        async with self._store.thread_txn(account, thread_id) as txn:
            for turn_data in turns:
                n = turn_data["n"]
                fid_str = turn_data.get("fidelity", "verbatim")
                fidelity = (
                    Fidelity.ABRIDGED if fid_str == "abridged" else Fidelity.VERBATIM
                )

                if uq := turn_data.get("user_query"):
                    if len(uq) > MAX_BODY_CHARS:
                        uq = uq[:MAX_BODY_CHARS]
                        fidelity = Fidelity.TRUNCATED
                    if self._config.redaction_enabled:
                        uq = redact_text(uq).text
                    anchor = normalise_anchor(uq)
                    action = await txn.upsert_turn(
                        n=n,
                        role="user",
                        body=uq,
                        fidelity=fidelity,
                        anchor=anchor,
                        recovered=True,
                    )
                    if action.action != "no_op":
                        stored.append(n)
                        if hasattr(self._store, "_gaps"):
                            self._store._gaps.recover(thread_id, n)
                        if hasattr(txn, "recover_gap"):
                            await txn.recover_gap(n)
                    else:
                        skipped.append(n)

                if ar := turn_data.get("assistant_response"):
                    if len(ar) > MAX_BODY_CHARS:
                        ar = ar[:MAX_BODY_CHARS]
                        fidelity = Fidelity.TRUNCATED
                    if self._config.redaction_enabled:
                        ar = redact_text(ar).text
                    action = await txn.upsert_turn(
                        n=n,
                        role="assistant",
                        body=ar,
                        fidelity=fidelity,
                        recovered=True,
                    )
                    if action.action != "no_op":
                        if n not in stored:
                            stored.append(n)
                        if hasattr(self._store, "_gaps"):
                            self._store._gaps.recover(thread_id, n)
                        if hasattr(txn, "recover_gap"):
                            await txn.recover_gap(n)

            await txn.enqueue_export()

        return {"ok": True, "stored": stored, "skipped": skipped}

    @property
    def envelope_encryption_enabled(self) -> bool:
        if self._config.envelope_encryption_enabled or self._config.master_key:
            return True
        if getattr(self._store, "_envelope_encryption_enabled", False):
            return True
        return False

    async def find(
        self,
        query: str | None = None,
        limit: int = 10,
        account: str | None = None,
        titles_only: Optional[bool] = None,
    ) -> list[ThreadHit]:
        account = account or self._config.default_account
        if titles_only is None:
            titles_only = self.envelope_encryption_enabled
        if hasattr(self._store, "find"):
            try:
                return await self._store.find(account, query, limit, titles_only=titles_only)
            except TypeError:
                return await self._store.find(account, query, limit)
        return []

    def stats(
        self,
        thread_id: str | None = None,
        account: str | None = None,
    ) -> Optional[dict]:
        account = account or self._config.default_account
        if hasattr(self._store, "get_thread_stats"):
            return self._store.get_thread_stats(thread_id) if thread_id else None
        return None

    async def stats_async(
        self,
        thread_id: str | None = None,
        account: str | None = None,
    ) -> Optional[dict]:
        account = account or self._config.default_account
        if not thread_id:
            return None
        if hasattr(self._store, "get_thread_stats"):
            try:
                res = self._store.get_thread_stats(thread_id, account)
                if asyncio.iscoroutine(res):
                    res = await res
                if res:
                    return res
            except TypeError:
                res = self._store.get_thread_stats(thread_id)
                if asyncio.iscoroutine(res):
                    res = await res
                if res:
                    return res
        if hasattr(self._store, "stats"):
            st = await self._store.stats(account, thread_id)
            if st:
                return st.model_dump()
        return None

    def pause_thread(self, thread_id: str, account: str | None = None) -> None:
        if hasattr(self._store, "pause_thread"):
            self._store.pause_thread(thread_id)

    def is_thread_paused(self, thread_id: str, account: str | None = None) -> bool:
        if hasattr(self._store, "is_thread_paused"):
            return self._store.is_thread_paused(thread_id)
        return False
