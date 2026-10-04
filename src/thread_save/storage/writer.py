"""Write engine — FileStore implementation with ThreadTxn (§3, §4, §4.1).

Rebuilt for the invocation reliability layer:
- Store protocol implementation (cross_device_sync_plan_2.md §4.1)
- FileStore.thread_txn: per-file lock, in-memory edits, atomic write on exit,
  nothing written on exception.
- All protocol logic moved to TurnService.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

from thread_save.config import VaultConfig
from thread_save.models import (
    Fidelity,
    MAX_BODY_CHARS,
    PageState,
    SlotKey,
    ThreadMeta,
    TurnData,
)
from thread_save.security.idempotency import (
    SlotIndex,
    WriteAction,
    compute_content_hash,
    compute_content_hash_short,
)
from thread_save.security.sanitizer import sanitize_slug
from thread_save.storage.formatter import (
    format_front_matter,
    format_new_page,
    format_stub_body,
    format_turn,
    format_turn_separator,
)
from thread_save.storage.gaps import GapTracker
from thread_save.storage.identity import (
    generate_thread_id,
    generate_thread_id_short,
    normalise_anchor,
    search_active_by_anchor,
    update_active_thread,
)
from thread_save.storage.path_resolver import (
    ensure_directory,
    resolve_thread_path,
)
from thread_save.storage.protocol import (
    BindResult,
    Stub,
    ThreadHit,
    ThreadStats,
    ThreadTxn,
    UpsertResult,
)


class _CountList(list):
    def __eq__(self, other):
        if other == 0 and len(self) == 0:
            return True
        return super().__eq__(other)


class _ThreadEntry:
    __slots__ = ("meta", "created_dt", "thread_id_short", "anchor_map")

    def __init__(
        self, meta: ThreadMeta, created_dt: datetime, thread_id_short: str
    ):
        self.meta = meta
        self.created_dt = created_dt
        self.thread_id_short = thread_id_short
        # {n: normalised_anchor} for user turns in this thread
        self.anchor_map: dict[int, str] = {}


class _ThreadRegistry:
    def __init__(self):
        self._threads: dict[str, _ThreadEntry] = {}

    def register(self, thread_id: str, entry: _ThreadEntry) -> None:
        self._threads[thread_id] = entry

    def get(self, thread_id: str) -> Optional[_ThreadEntry]:
        return self._threads.get(thread_id)

    def exists(self, thread_id: str) -> bool:
        return thread_id in self._threads

    def all_ids(self) -> list[str]:
        return list(self._threads.keys())

    def all_entries(self) -> dict[str, _ThreadEntry]:
        return dict(self._threads)


class FileThreadTxn:
    """ThreadTxn implementation for FileStore (§4.1).

    Per-file lock, in-memory edits, atomic write on exit,
    nothing written on exception.
    """

    def __init__(self, store: FileStore, account_id: str, thread_id: str):
        self._store = store
        self._account_id = account_id
        self._thread_id = thread_id
        self._lock: Optional[asyncio.Lock] = None
        self._entry: Optional[_ThreadEntry] = None
        self._ps: Optional[PageState] = None
        self._current_page_file: Optional[Path] = None
        self._current_content: str = ""
        self._staged_page_edits: dict[Path, str] = {}
        self._staged_meta_updates: dict[str, Any] = {}
        self._staged_slots: list[tuple[SlotKey, str, Fidelity, int, Optional[str]]] = []
        self._staged_anchor_updates: dict[int, str] = {}
        self._staged_gaps: list[int] = []

    async def __aenter__(self) -> ThreadTxn:
        self._lock = self._store._get_thread_lock(self._thread_id)
        await self._lock.acquire()

        self._entry = self._store._registry.get(self._thread_id)
        if not self._entry:
            raise ValueError(f"Thread {self._thread_id} not found in registry")

        self._ps = self._store._page_states.get(self._thread_id)
        if not self._ps:
            raise ValueError(f"Thread {self._thread_id} page state not found")

        self._current_page_file = Path(self._ps.file_path)
        if self._current_page_file.exists():
            self._current_content = self._current_page_file.read_text(encoding="utf-8")
        else:
            self._current_content = format_new_page(self._entry.meta)

        self._staged_page_edits[self._current_page_file] = self._current_content
        return self

    async def load_slots(self) -> SlotIndex:
        return self._store._slots

    async def match_anchor(self, prev_user_anchor: str) -> Optional[int]:
        norm = normalise_anchor(prev_user_anchor)
        if not norm or not self._entry:
            return None
        matches = [m for m, a in self._entry.anchor_map.items() if a == norm]
        return max(matches) if matches else None

    async def upsert_turn(
        self,
        n: int,
        role: str,
        body: str,
        fidelity: Fidelity,
        recovered: bool = False,
        turn_key: str | None = None,
        page: int | None = None,
        model: str = "",
        anchor: str = "",
    ) -> UpsertResult:
        assert self._entry is not None and self._ps is not None

        # Size limit truncation (never reject; store prefix with Fidelity.TRUNCATED)
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS]
            fidelity = Fidelity.TRUNCATED

        key = SlotKey(n, role)
        body_hash = compute_content_hash(body)

        action = self._store._slots.evaluate(
            self._thread_id, key, body_hash, fidelity, len(body)
        )

        if action == WriteAction.NO_OP:
            return UpsertResult(action="no_op", n=n)

        now = datetime.now(timezone.utc).astimezone()
        turn = TurnData(
            turn_index=n,
            role=role,
            body=body,
            timestamp=now,
            model=model,
            fidelity=fidelity,
            char_count=len(body),
            content_hash=compute_content_hash_short(body),
            recovered=recovered,
            anchor=anchor if role == "user" else "",
            turn_key=turn_key,
        )

        nonce = self._entry.meta.nonce
        turn_text = format_turn_separator() + "\n" + format_turn(turn, nonce=nonce) + "\n"
        turn_bytes_len = len(turn_text.encode("utf-8"))

        if action == WriteAction.WRITE_NEW:
            # Check pagination rollover
            projected = self._ps.current_bytes + turn_bytes_len
            projected_turns = self._ps.current_turn_count + 1

            if (
                (projected > self._store._config.page_max_bytes
                 or projected_turns > self._store._config.page_max_turns)
                and self._ps.current_turn_count > 0
            ):
                new_page_num = self._ps.current_page + 1
                new_file = resolve_thread_path(
                    self._store._config,
                    self._entry.meta.account,
                    self._entry.created_dt,
                    self._entry.thread_id_short,
                    self._entry.meta.slug,
                    new_page_num,
                )
                prev_name = self._current_page_file.name
                new_meta = self._entry.meta.model_copy(update={
                    "page": new_page_num,
                    "prev": prev_name,
                    "turn_count": 0,
                    "turn_range": [n, n],
                })
                new_content = format_new_page(new_meta, prev_filename=prev_name)
                self._current_page_file = new_file
                self._current_content = new_content
                self._staged_page_edits[self._current_page_file] = self._current_content
                self._ps.current_page = new_page_num
                self._ps.current_bytes = len(new_content.encode("utf-8"))
                self._ps.current_turn_count = 0
                self._ps.file_path = str(new_file)

            self._current_content += turn_text
            self._staged_page_edits[self._current_page_file] = self._current_content
            self._ps.current_bytes += turn_bytes_len
            self._ps.current_turn_count += 1
            if n > self._ps.global_turn_index:
                self._ps.global_turn_index = n

        elif action == WriteAction.REPLACE:
            replaced = False
            for pfile, pcontent in self._staged_page_edits.items():
                open_pattern = rf"<!-- turn [^>]*i={n}\b[^>]*role={role}\b[^>]* -->"
                close_pattern = rf"<!-- /turn [^>]*i={n}\b[^>]* -->"
                open_match = re.search(open_pattern, pcontent)
                if open_match:
                    close_match = re.search(close_pattern, pcontent[open_match.end():])
                    if close_match:
                        close_end = open_match.end() + close_match.end()
                        new_block = format_turn(turn, nonce=nonce)
                        new_pcontent = (
                            pcontent[:open_match.start()]
                            + new_block
                            + pcontent[close_end:]
                        )
                        self._staged_page_edits[pfile] = new_pcontent
                        if pfile == self._current_page_file:
                            self._current_content = new_pcontent
                        replaced = True
                        break
            if not replaced:
                self._current_content += turn_text
                self._staged_page_edits[self._current_page_file] = self._current_content
                self._ps.current_bytes += turn_bytes_len

        self._staged_slots.append((key, body_hash, fidelity, len(body), turn.turn_key))
        if role == "user" and anchor:
            self._staged_anchor_updates[n] = normalise_anchor(anchor)

        return UpsertResult(action=action.value, n=n)

    async def write_stubs(self, stubs: list[Stub]) -> None:
        assert self._entry is not None and self._ps is not None
        now = datetime.now(timezone.utc).astimezone()
        nonce = self._entry.meta.nonce

        for stub in stubs:
            gn = stub.n
            u_body = format_stub_body(stub.anchor)
            a_body = format_stub_body(stub.anchor)
            u_turn = TurnData(
                turn_index=gn,
                role="user",
                body=u_body,
                timestamp=now,
                fidelity=Fidelity.STUB,
                char_count=len(u_body),
                content_hash=compute_content_hash_short(u_body),
                recovered=False,
                anchor=stub.anchor,
            )
            a_turn = TurnData(
                turn_index=gn,
                role="assistant",
                body=a_body,
                timestamp=now,
                fidelity=Fidelity.STUB,
                char_count=len(a_body),
                content_hash=compute_content_hash_short(a_body),
                recovered=False,
                anchor="",
            )
            u_text = format_turn_separator() + "\n" + format_turn(u_turn, nonce=nonce) + "\n"
            a_text = format_turn_separator() + "\n" + format_turn(a_turn, nonce=nonce) + "\n"

            combined = u_text + a_text
            self._current_content += combined
            self._staged_page_edits[self._current_page_file] = self._current_content
            self._ps.current_bytes += len(combined.encode("utf-8"))

            self._staged_slots.append((SlotKey(gn, "user"), u_turn.content_hash, Fidelity.STUB, u_turn.char_count, None))
            self._staged_slots.append((SlotKey(gn, "assistant"), a_turn.content_hash, Fidelity.STUB, a_turn.char_count, None))

    async def record_gaps(self, gaps: list[int]) -> None:
        self._staged_gaps.extend(gaps)

    async def update_thread_meta(self, **fields) -> None:
        self._staged_meta_updates.update(fields)

    async def enqueue_export(self) -> None:
        pass

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                # Invariant W-11: on exception, nothing is written to disk!
                return False

            assert self._entry is not None and self._ps is not None
            now = datetime.now(timezone.utc).astimezone()

            for k, v in self._staged_meta_updates.items():
                if hasattr(self._entry.meta, k):
                    setattr(self._entry.meta, k, v)
            self._entry.meta.updated = now
            self._entry.meta.turn_count = self._ps.current_turn_count
            self._entry.meta.bytes = self._ps.current_bytes

            # Update front matter in current page content
            new_fm = format_front_matter(self._entry.meta)
            cur_text = self._staged_page_edits[self._current_page_file]
            parts = cur_text.split("---\n", 2)
            if len(parts) >= 3:
                self._staged_page_edits[self._current_page_file] = new_fm + parts[2]

            # Write all staged page edits to disk atomically
            for pfile, ptext in self._staged_page_edits.items():
                ensure_directory(pfile.parent)
                await self._store._atomic_write(pfile, ptext.encode("utf-8"))

            # Commit slots and gaps in-memory
            for key, body_hash, fidelity, length, turn_key in self._staged_slots:
                self._store._slots.record(
                    self._thread_id, key, body_hash, fidelity, length, turn_key=turn_key
                )

            for gn, anc in self._staged_anchor_updates.items():
                self._entry.anchor_map[gn] = anc

            if self._staged_gaps:
                self._store._gaps.register_gaps(self._thread_id, self._staged_gaps)

            # Update active.json
            client_key = f"{self._entry.meta.client}:{self._entry.meta.account}"
            last_anchor = self._entry.anchor_map.get(
                max(self._entry.anchor_map.keys(), default=0), ""
            )
            update_active_thread(
                self._store._config, client_key, self._thread_id, last_anchor
            )
        finally:
            if self._lock:
                self._lock.release()


class FileStore:
    """Core persistence backend for local Markdown vault files (§4.1)."""

    def __init__(self, config: VaultConfig):
        self._config = config
        self._page_states: dict[str, PageState] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._slots = SlotIndex()
        self._gaps = GapTracker(lost_hours=config.gap_lost_hours)
        self._registry = _ThreadRegistry()

    def _get_thread_lock(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._file_locks:
            self._file_locks[thread_id] = asyncio.Lock()
        return self._file_locks[thread_id]

    @property
    def gap_tracker(self) -> GapTracker:
        return self._gaps

    async def _create_thread(
        self,
        title_hint: str,
        tags: list[str] | None = None,
        account: str | None = None,
        client: str = "claude-desktop",
    ) -> tuple[str, PageState]:
        now = datetime.now(timezone.utc).astimezone()
        account = account or self._config.default_account
        thread_id = generate_thread_id()
        tid_short = generate_thread_id_short(thread_id)
        slug = sanitize_slug(title_hint or "untitled", self._config.slug_max_chars)
        title = (title_hint or "").strip() or "Untitled Thread"

        meta = ThreadMeta(
            schema_version=2,
            thread_id=thread_id,
            title=title,
            slug=slug,
            account=account,
            client=client,
            created=now,
            updated=now,
            page=1,
            turn_count=0,
            turn_range=[1, 0],
            bytes=0,
            tags=tags or [],
        )

        file_path = resolve_thread_path(
            self._config, account, now, tid_short, slug, 1,
        )
        ensure_directory(file_path.parent)

        content = format_new_page(meta)
        content_bytes = content.encode("utf-8")
        await self._atomic_write(file_path, content_bytes)

        ps = PageState(
            thread_id=thread_id,
            current_page=1,
            current_bytes=len(content_bytes),
            current_turn_count=0,
            global_turn_index=0,
            file_path=str(file_path),
        )
        self._page_states[thread_id] = ps

        entry = _ThreadEntry(meta, now, tid_short)
        self._registry.register(thread_id, entry)

        client_key = f"{client}:{account}"
        update_active_thread(self._config, client_key, thread_id)

        return thread_id, ps

    def _bind_thread(
        self,
        thread_id: str | None,
        prev_user_anchor: str | None,
        account: str,
    ) -> tuple[str | None, str]:
        if thread_id and self._registry.exists(thread_id):
            return thread_id, "id"

        if prev_user_anchor:
            norm_anchor = normalise_anchor(prev_user_anchor)
            if norm_anchor:
                candidates = []
                for tid, entry in self._registry.all_entries().items():
                    matches = [
                        n for n, a in entry.anchor_map.items()
                        if a == norm_anchor
                    ]
                    if len(matches) == 1:
                        candidates.append(tid)

                if len(candidates) == 1:
                    return candidates[0], "anchor"
                elif len(candidates) > 1:
                    return None, "new"

                found = search_active_by_anchor(self._config, norm_anchor)
                if found and self._registry.exists(found):
                    return found, "anchor"

        return None, "new"

    # ── Store Protocol Implementation (§4.1) ───────────────────────────

    async def bind_thread(
        self,
        account_id: str,
        thread_id: str | None,
        anchor: str | None,
    ) -> BindResult:
        tid, method = self._bind_thread(thread_id, anchor, account_id)
        return BindResult(thread_id=tid, method=method)

    async def create_thread(
        self,
        account_id: str,
        title_hint: str | None,
        tags: list[str] | None = None,
        client: str = "claude-desktop",
    ) -> ThreadMeta:
        tid, _ = await self._create_thread(
            title_hint or "Untitled Thread", tags=tags, account=account_id, client=client
        )
        entry = self._registry.get(tid)
        assert entry is not None
        return entry.meta

    def thread_txn(self, account_id: str, thread_id: str) -> FileThreadTxn:
        return FileThreadTxn(self, account_id, thread_id)

    async def is_tombstoned(self, account_id: str, thread_id: str) -> bool:
        return False

    async def find(
        self,
        account_id: str,
        query: str | None,
        limit: int = 10,
        titles_only: bool = False,
    ) -> list[ThreadHit]:
        results = []
        q_lower = (query or "").lower()
        for tid, entry in self._registry.all_entries().items():
            if not q_lower:
                match = True
            elif titles_only:
                match = q_lower in entry.meta.title.lower()
            else:
                match = (
                    q_lower in entry.meta.title.lower()
                    or q_lower in entry.meta.slug.lower()
                )
            if match:
                results.append(
                    ThreadHit(
                        thread_id=tid,
                        title=entry.meta.title,
                        snippet=entry.meta.title,
                        updated_at=entry.meta.updated.isoformat(),
                    )
                )
                if len(results) >= limit:
                    break
        return results

    async def stats(
        self,
        account_id: str,
        thread_id: str | None,
    ) -> Optional[ThreadStats]:
        if not thread_id:
            return None
        s = self.get_thread_stats(thread_id)
        if not s:
            return None
        fc = s.get("fidelity_counts", {})
        return ThreadStats(
            total_turns=s.get("total_turns", 0),
            verbatim=fc.get("verbatim", 0),
            abridged=fc.get("abridged", 0),
            truncated=fc.get("truncated", 0),
            stubs=fc.get("stub", 0),
            gaps_open=s.get("gaps_open", []),
            gaps_recovered=s.get("gaps_recovered", []),
            open_turn=s.get("open_turn"),
            gaps_open_count=s.get("gaps_open_count", 0),
            gaps_lost_count=s.get("gaps_lost_count", 0),
        )

    # ── Utility Methods ────────────────────────────────────────────────

    def get_thread_stats(self, thread_id: str) -> Optional[dict]:
        entry = self._registry.get(thread_id)
        if entry is None:
            return None

        meta = entry.meta
        slots = self._slots.all_slot_keys(thread_id)
        ps = self._page_states.get(thread_id)

        fidelity_counts: dict[str, int] = {
            "verbatim": 0,
            "abridged": 0,
            "truncated": 0,
            "stub": 0,
            "open": 0,
        }
        for sk in slots:
            slot_entry = self._slots.get(thread_id, sk)
            if slot_entry:
                fidelity_counts[slot_entry.fidelity.value] = (
                    fidelity_counts.get(slot_entry.fidelity.value, 0) + 1
                )

        user_slots = [sk for sk in slots if sk.role == "user"]
        non_stub_users = sum(
            1
            for sk in user_slots
            if (se := self._slots.get(thread_id, sk))
            and se.fidelity != Fidelity.STUB
        )
        coverage_pct = (
            round((non_stub_users / len(user_slots)) * 100, 1)
            if user_slots
            else 0.0
        )

        summary = self._gaps.get_summary(thread_id)
        open_gaps = _CountList(summary["gaps_open"])
        recovered_gaps = _CountList(summary["gaps_recovered"])
        lost_gaps = _CountList(summary["gaps_lost"])

        return {
            "thread_id": thread_id,
            "title": meta.title,
            "pages": ps.current_page if ps else meta.page,
            "total_bytes": ps.current_bytes if ps else meta.bytes,
            "total_turns": meta.turn_count,
            "open_turn": meta.open_turn,
            "fidelity_counts": fidelity_counts,
            "stub": fidelity_counts.get("stub", 0),
            "coverage_pct": coverage_pct,
            "gaps_open": open_gaps,
            "gaps_open_count": len(open_gaps),
            "gaps_recovered": recovered_gaps,
            "gaps_recovered_count": len(recovered_gaps),
            "gaps_lost": lost_gaps,
            "gaps_lost_count": summary["gaps_lost_count"],
            "paused": meta.paused,
        }

    def get_all_threads(self, account_id: str = "") -> list[dict]:
        results = []
        for tid, entry in self._registry.all_entries().items():
            meta = entry.meta
            first_anchor = entry.anchor_map.get(1, "")
            last_anchor = entry.anchor_map.get(
                max(entry.anchor_map.keys(), default=0), ""
            )
            results.append({
                "thread_id": tid,
                "title": meta.title,
                "slug": meta.slug,
                "account": meta.account,
                "created_at": meta.created.isoformat(),
                "updated": meta.updated.isoformat(),
                "turn_count": meta.turn_count,
                "tags": meta.tags,
                "first_user_anchor": first_anchor,
                "last_user_anchor": last_anchor,
            })
        return results

    def pause_thread(self, thread_id: str) -> None:
        entry = self._registry.get(thread_id)
        if entry:
            entry.meta.paused = True

    def is_thread_paused(self, thread_id: str) -> bool:
        entry = self._registry.get(thread_id)
        return entry.meta.paused if entry else False

    def delete_thread(self, account_id: str, thread_id: str) -> None:
        if thread_id in self._registry._threads:
            del self._registry._threads[thread_id]
        if thread_id in self._page_states:
            del self._page_states[thread_id]

    async def _atomic_write(self, target: Path, data: bytes) -> None:
        tmp = target.with_suffix(f".tmp.{os.getpid()}.{asyncio.get_event_loop().time()}")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError:
            await asyncio.sleep(0.01)
            try:
                os.replace(tmp, target)
            except OSError:
                await asyncio.sleep(0.05)
                os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    async def _atomic_append(self, target: Path, data: bytes) -> None:
        existing = target.read_bytes() if target.exists() else b""
        await self._atomic_write(target, existing + data)


def WriteEngine(config: VaultConfig):
    """Factory creating TurnService backed by FileStore for protocol tests."""
    from thread_save.service import TurnService
    store = FileStore(config)
    return TurnService(store, config=config)
