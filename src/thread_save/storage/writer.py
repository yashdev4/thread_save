"""Write engine — FileStore implementation with ThreadTxn (§3, §4, §4.1).

Rebuilt for the invocation reliability layer:
- Store protocol implementation (cross_device_sync_plan_2.md §4.1)
- FileStore.thread_txn: per-file lock, in-memory edits, atomic write on exit,
  nothing written on exception.
- All protocol logic moved to TurnService.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
from datetime import datetime, timedelta, timezone
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
    parse_page,
)
from thread_save.storage.gaps import GapTracker
from thread_save.storage.identity import (
    anchors_agree,
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

logger = logging.getLogger("thread_save.storage.writer")

_PAGE_FILE_RE = re.compile(r"_p(\d+)\.md$")
_TURN_OPEN_RE = re.compile(r"<!-- turn i=(\d+) ")
_THREAD_ID_RE = re.compile(r'^"?thread_id"?:\s*"?([0-9A-Z]{26})"?', re.M)


class _CountList(list):
    def __eq__(self, other):
        if other == 0 and len(self) == 0:
            return True
        return super().__eq__(other)


# P1-18: how long a chat counts as active when a call lost its thread_id
_RECENT_BIND_WINDOW = timedelta(hours=6)


class _ThreadEntry:
    __slots__ = ("meta", "created_dt", "thread_id_short", "anchor_map", "persisted")

    def __init__(
        self,
        meta: ThreadMeta,
        created_dt: datetime,
        thread_id_short: str,
        persisted: bool = True,
    ):
        self.meta = meta
        self.created_dt = created_dt
        self.thread_id_short = thread_id_short
        # {n: normalised_anchor} for user turns in this thread
        self.anchor_map: dict[int, str] = {}
        # False until the first transaction commits its page to disk (W-11)
        self.persisted = persisted


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
        self._ps_snapshot: Optional[PageState] = None
        self._meta_snapshot: Optional[ThreadMeta] = None

    async def __aenter__(self) -> ThreadTxn:
        self._lock = self._store._get_thread_lock(self._thread_id)
        await self._lock.acquire()

        try:
            self._entry = self._store._registry.get(self._thread_id)
            if not self._entry:
                raise ValueError(f"Thread {self._thread_id} not found in registry")

            self._ps = self._store._page_states.get(self._thread_id)
            if not self._ps:
                raise ValueError(f"Thread {self._thread_id} page state not found")
        except BaseException:
            self._lock.release()
            raise

        # In-memory edits are rolled back if the transaction fails (W-11)
        self._ps_snapshot = self._ps.model_copy()
        self._meta_snapshot = self._entry.meta.model_copy(deep=True)

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

    async def user_anchor(self, n: int) -> Optional[str]:
        if not self._entry:
            return None
        return self._entry.anchor_map.get(n)

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
        newest_wins: bool = False,
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
        if newest_wins and action == WriteAction.NO_OP:
            existing = self._store._slots.get(self._thread_id, key)
            if existing is not None and existing.content_hash != body_hash:
                action = WriteAction.REPLACE

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
        self._store._pending_export_threads.add(self._thread_id)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                # Invariant W-11: on exception, nothing is written to disk!
                self._rollback()
                return False
            try:
                await self._commit()
            except BaseException:
                self._rollback()
                raise
        finally:
            if self._lock:
                self._lock.release()

    def _rollback(self) -> None:
        """Undo in-memory edits; forget a thread whose first page never reached disk."""
        if self._entry is not None and not self._entry.persisted:
            self._store._discard_thread(self._thread_id)
            return
        if self._ps_snapshot is not None:
            self._store._page_states[self._thread_id] = self._ps_snapshot
        if self._entry is not None and self._meta_snapshot is not None:
            self._entry.meta = self._meta_snapshot

    async def _commit(self) -> None:
        assert self._entry is not None and self._ps is not None
        if (
            self._entry.persisted
            and not self._staged_slots
            and not self._staged_meta_updates
            and not self._staged_gaps
        ):
            return  # nothing changed: a retry must not touch the file (W-4)
        now = datetime.now(timezone.utc).astimezone()

        for k, v in self._staged_meta_updates.items():
            if hasattr(self._entry.meta, k):
                setattr(self._entry.meta, k, v)
        self._entry.meta.updated = now
        self._entry.meta.turn_count = self._ps.current_turn_count
        self._entry.meta.bytes = self._ps.current_bytes

        # Update front matter in current page content: this page's number,
        # its predecessor and the turn range it holds (P2-12)
        cur_text = self._staged_page_edits[self._current_page_file]
        page_ns = [int(m) for m in _TURN_OPEN_RE.findall(cur_text)]
        page_meta = self._entry.meta.model_copy(update={
            "page": self._ps.current_page,
            "prev": self._store._page_filename(self._entry, self._ps.current_page - 1),
            "turn_range": [min(page_ns), max(page_ns)] if page_ns else [1, 0],
        })
        new_fm = format_front_matter(page_meta)
        parts = cur_text.split("---\n", 2)
        if len(parts) >= 3:
            self._staged_page_edits[self._current_page_file] = new_fm + parts[2]

        # Write all staged page edits to disk atomically
        for pfile, ptext in self._staged_page_edits.items():
            ensure_directory(pfile.parent)
            await self._store._atomic_write(pfile, ptext.encode("utf-8"))
        self._entry.persisted = True

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


class FileStore:
    """Core persistence backend for local Markdown vault files (§4.1)."""

    def __init__(self, config: VaultConfig):
        self._config = config
        self._page_states: dict[str, PageState] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._slots = SlotIndex()
        self._gaps = GapTracker(lost_hours=config.gap_lost_hours)
        self._registry = _ThreadRegistry()
        from thread_save.export.offload import OffloadManager
        self._offload_mgr = OffloadManager(config)
        self._offloaded_threads: dict[str, dict] = {
            tid: ptr.to_dict() for tid, ptr in self._offload_mgr.offload_index.load().items()
        }
        self._pending_export_threads: set[str] = set()
        self._loaded_accounts: set[str] = set()

    def _get_thread_lock(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._file_locks:
            self._file_locks[thread_id] = asyncio.Lock()
        return self._file_locks[thread_id]

    @property
    def gap_tracker(self) -> GapTracker:
        return self._gaps

    @property
    def offload_manager(self) -> Any:
        return self._offload_mgr

    @property
    def offloaded_threads(self) -> dict[str, dict]:
        return dict(self._offloaded_threads)

    async def _create_thread(
        self,
        title_hint: str,
        tags: list[str] | None = None,
        account: str | None = None,
        client: str = "claude-desktop",
        continues: str | None = None,
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
            continues=continues,
        )

        file_path = resolve_thread_path(
            self._config, account, now, tid_short, slug, 1,
        )

        # The page reaches disk only when the first transaction commits (W-11):
        # a failed first save must not leave a header-only file behind.
        content = format_new_page(meta)
        content_bytes = content.encode("utf-8")

        ps = PageState(
            thread_id=thread_id,
            current_page=1,
            current_bytes=len(content_bytes),
            current_turn_count=0,
            global_turn_index=0,
            file_path=str(file_path),
        )
        self._page_states[thread_id] = ps

        entry = _ThreadEntry(meta, now, tid_short, persisted=False)
        self._registry.register(thread_id, entry)

        return thread_id, ps

    def _discard_thread(self, thread_id: str) -> None:
        """Forget a thread that was created in memory but never written."""
        self._registry._threads.pop(thread_id, None)
        self._page_states.pop(thread_id, None)

    def _page_filename(self, entry: _ThreadEntry, page: int) -> Optional[str]:
        if page < 1:
            return None
        return resolve_thread_path(
            self._config, entry.meta.account, entry.created_dt,
            entry.thread_id_short, entry.meta.slug, page,
        ).name

    # ── Restart recovery (B6 L1) ───────────────────────────────────────

    def _account_dir(self, account: str) -> Optional[Path]:
        if not account or account.startswith((".", "_")):
            return None
        root = self._config.vault_root.resolve()
        acc_dir = (root / account).resolve()
        if acc_dir.parent != root or not acc_dir.is_dir():
            return None
        return acc_dir

    def restore_thread_state(
        self, thread_id: str, pages: list[tuple[int, Path, str]]
    ) -> bool:
        """Rebuild registry, page state, slots, anchors and gaps from page files.

        `pages` holds (page number, path, markdown). The page number comes from the
        filename, because older front matter always said `page: 1`.
        """
        parsed = []
        for page_no, path, content in sorted(pages, key=lambda x: x[0]):
            try:
                meta, turns = parse_page(content)
            except Exception as e:
                logger.warning("Cannot parse %s during restore: %s", path, e)
                return False
            if meta.thread_id != thread_id:
                continue
            parsed.append((page_no, path, content, meta, turns))
        if not parsed:
            return False

        last_page_no, last_path, last_content, last_meta, last_turns = parsed[-1]
        entry = _ThreadEntry(
            last_meta.model_copy(update={"page": 1, "prev": None}),
            last_meta.created,
            generate_thread_id_short(thread_id),
        )
        max_n = 0
        for _, _, _, _, turns in parsed:
            for t in turns:
                max_n = max(max_n, t.turn_index)
                self._slots.record(
                    thread_id,
                    SlotKey(t.turn_index, t.role),
                    compute_content_hash(t.body),
                    t.fidelity,
                    len(t.body),
                    turn_key=t.turn_key,
                )
                if t.role != "user":
                    continue
                if t.fidelity == Fidelity.STUB:
                    self._gaps.register_gaps(thread_id, [t.turn_index])
                    self._gaps.mark_requested(thread_id, t.turn_index)
                    continue
                if t.recovered:
                    self._gaps.register_gaps(thread_id, [t.turn_index])
                    self._gaps.recover(thread_id, t.turn_index)
                # The header keeps only 40 chars of the anchor; rebuild it from the body
                entry.anchor_map[t.turn_index] = normalise_anchor(t.body)

        self._registry.register(thread_id, entry)
        self._page_states[thread_id] = PageState(
            thread_id=thread_id,
            current_page=last_page_no,
            current_bytes=len(last_content.encode("utf-8")),
            current_turn_count=len(last_turns),
            global_turn_index=max_n,
            file_path=str(last_path),
        )
        return True

    def _rehydrate(self, thread_id: str, account: str) -> bool:
        """Load a thread written by an earlier process from its Markdown pages."""
        if self._registry.exists(thread_id):
            return True
        acc_dir = self._account_dir(account)
        if acc_dir is None or not thread_id:
            return False
        short = generate_thread_id_short(thread_id)
        pattern = f"*/*/*_{glob.escape(short)}_*_p*.md"
        pages: list[tuple[int, Path, str]] = []
        for path in acc_dir.glob(pattern):
            m = _PAGE_FILE_RE.search(path.name)
            if not m:
                continue
            try:
                pages.append((int(m.group(1)), path, path.read_text(encoding="utf-8")))
            except OSError as e:
                logger.warning("Cannot read %s during restore: %s", path, e)
                return False
        if not pages:
            return False
        restored = self.restore_thread_state(thread_id, pages)
        if restored:
            logger.info("Restored thread %s from %d page(s)", thread_id, len(pages))
        return restored

    def _load_account(self, account: str) -> None:
        """Restore every thread of an account once per process (for find)."""
        if account in self._loaded_accounts:
            return
        self._loaded_accounts.add(account)
        acc_dir = self._account_dir(account)
        if acc_dir is None:
            return
        for path in acc_dir.glob("*/*/*_p01.md"):
            try:
                with open(path, encoding="utf-8") as f:
                    m = _THREAD_ID_RE.search(f.read(4096))
            except OSError:
                continue
            if m:
                self._rehydrate(m.group(1), account)

    def _bind_thread(
        self,
        thread_id: str | None,
        prev_user_anchor: str | None,
        account: str,
        user_message: str | None = None,
    ) -> tuple[str | None, str]:
        if thread_id:
            if self._registry.exists(thread_id) or self._rehydrate(thread_id, account):
                return thread_id, "id"
            if thread_id in self._offloaded_threads:
                return thread_id, "offloaded_id"
            # P1-18: the model copied the 26-character id with a mistake
            fixed = self._match_mangled_id(thread_id, account)
            if fixed:
                return fixed, "id_fuzzy"

        tid, method = self._bind_by_anchor(prev_user_anchor, account)
        if tid or method == "ambiguous":
            return (tid, method) if tid else (None, "new")
        # P1-18: no usable id; the chat's latest message identifies its thread
        recent = self._bind_recent(account, prev_user_anchor, user_message)
        if recent:
            return recent, "recent"
        return None, "new"

    def _match_mangled_id(self, thread_id: str, account: str) -> Optional[str]:
        """The one known thread whose id differs from `thread_id` by a copying slip."""
        self._load_account(account)
        sent = thread_id.strip().upper()
        candidates = []
        for tid, entry in self._registry.all_entries().items():
            if entry.meta.account != account:
                continue
            same_tail = tid[-6:].upper() == sent[-6:]
            near = len(tid) == len(sent) and sum(a != b for a, b in zip(tid.upper(), sent)) <= 2
            if same_tail or near:
                candidates.append(tid)
        return candidates[0] if len(candidates) == 1 else None

    def _bind_recent(
        self, account: str, prev_user_anchor: str | None, user_message: str | None
    ) -> Optional[str]:
        """A thread of this account, active recently, whose latest user message is
        the one this call names: the previous message (a new turn), or this very
        message with no reply stored yet (an end-of-reply call). Only a single match
        binds, so two chats are never merged on a guess (I-3)."""
        if not (normalise_anchor(prev_user_anchor or "") or normalise_anchor(user_message or "")):
            return None
        self._load_account(account)
        now = datetime.now(timezone.utc)
        candidates = []
        for tid, entry in self._registry.all_entries().items():
            if entry.meta.account != account or not entry.anchor_map:
                continue
            updated = entry.meta.updated
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if now - updated > _RECENT_BIND_WINDOW:
                continue
            last_n = max(n for n, a in entry.anchor_map.items() if a) if any(entry.anchor_map.values()) else 0
            if not last_n:
                continue
            latest = entry.anchor_map[last_n]
            if prev_user_anchor and anchors_agree(prev_user_anchor, latest):
                candidates.append(tid)
            elif user_message and normalise_anchor(user_message) == latest:
                reply = self._slots.get(tid, SlotKey(last_n, "assistant"))
                if reply is None or reply.fidelity in (Fidelity.OPEN, Fidelity.STUB):
                    candidates.append(tid)
        return candidates[0] if len(candidates) == 1 else None

    def _bind_by_anchor(self, prev_user_anchor: str | None, account: str) -> tuple[Optional[str], str]:
        """Exact anchor binding (§4.1). Returns (thread_id, "anchor") on a match,
        (None, "ambiguous") when several threads match, else (None, "new")."""
        thread_id = None

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
                    return None, "ambiguous"

                found = search_active_by_anchor(self._config, norm_anchor)
                if found and (
                    self._registry.exists(found) or self._rehydrate(found, account)
                ):
                    return found, "anchor"

                # Check offloaded candidates (§1 G7)
                offloaded_matches = [
                    tid for tid, ptr in self._offloaded_threads.items()
                    if ptr.get("last_user_anchor") == norm_anchor
                ]
                if len(offloaded_matches) == 1:
                    return offloaded_matches[0], "offloaded_anchor"
                elif len(offloaded_matches) > 1:
                    return None, "ambiguous"

        return thread_id, "new"

    # ── Store Protocol Implementation (§4.1) ───────────────────────────

    async def bind_thread(
        self,
        account_id: str,
        thread_id: str | None,
        anchor: str | None,
        user_message: str | None = None,
    ) -> BindResult:
        tid, method = self._bind_thread(thread_id, anchor, account_id, user_message)
        return BindResult(thread_id=tid, method=method)

    async def create_thread(
        self,
        account_id: str,
        title_hint: str | None,
        tags: list[str] | None = None,
        client: str = "claude-desktop",
        continues: str | None = None,
    ) -> ThreadMeta:
        tid, _ = await self._create_thread(
            title_hint or "Untitled Thread", tags=tags, account=account_id, client=client, continues=continues
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
        self._load_account(account_id)
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
            reported=fc.get("reported", 0),
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
            "reported": 0,
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
