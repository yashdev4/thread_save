"""PostgreSQL persistence backend (PgStore) implementing Store & ThreadTxn protocols (§4.1, §4.2).

Features:
- Single-transaction save (§3.3)
- Advisory lock per thread: pg_advisory_xact_lock(hashtext(thread_id))
- Single-statement upsert with fidelity ranking (§4.3)
- Row-Level Security (RLS) scoped by app.account_id with SET LOCAL
- Outbox table for transactional exporter queue (W-11)
- Sticky pagination (§3.5)
- Tombstone checking via deleted_threads (W-10)
- Dense user turn numbers and gaps tracking
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import secrets
from typing import Any, AsyncContextManager, Optional
import uuid

import asyncpg

from thread_save.models import Fidelity, MAX_BODY_CHARS, ThreadMeta, SlotKey, TurnData
from thread_save.security.idempotency import (
    SlotIndex,
    compute_content_hash,
    compute_content_hash_short,
    canonical_v1,
)
from thread_save.security.sanitizer import sanitize_slug
from thread_save.storage.identity import (
    generate_thread_id,
    generate_thread_id_short,
    normalise_anchor,
)
from thread_save.storage.protocol import (
    BindResult,
    Stub,
    ThreadHit,
    ThreadStats,
    ThreadTxn,
    UpsertResult,
)
from thread_save.storage.writer import _CountList

logger = logging.getLogger("thread_save.storage.pg_store")

_RANK_TO_FIDELITY: dict[int, Fidelity] = {
    0: Fidelity.OPEN,
    1: Fidelity.STUB,
    2: Fidelity.TRUNCATED,
    3: Fidelity.ABRIDGED,
    4: Fidelity.VERBATIM,
}


class PgThreadTxn:
    """ThreadTxn implementation for PgStore (§4.1, §3.3)."""

    def __init__(
        self,
        store: PgStore,
        account_id: str,
        account_uuid: uuid.UUID,
        thread_id: str,
    ):
        self._store = store
        self._account_id = account_id
        self._account_uuid = account_uuid
        self._thread_id = thread_id
        self._conn: Optional[asyncpg.Connection] = None
        self._tx: Optional[asyncpg.transaction.Transaction] = None
        self._thread_row: Optional[asyncpg.Record] = None
        self._staged_meta_updates: dict[str, Any] = {}
        self._is_tombstoned: bool = False

    async def __aenter__(self) -> PgThreadTxn:
        self._conn = await self._store.pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()

        # Strict timeouts and pool hygiene (§3.3)
        await self._conn.execute("SET LOCAL statement_timeout = '4s'")
        await self._conn.execute("SET LOCAL idle_in_transaction_session_timeout = '5s'")
        await self._conn.execute(
            "SELECT set_config('app.account_id', $1, true)", str(self._account_uuid)
        )

        # Check tombstone
        del_row = await self._conn.fetchval(
            "SELECT 1 FROM deleted_threads WHERE thread_id = $1 AND account_id = $2",
            self._thread_id,
            self._account_uuid,
        )
        if del_row:
            self._is_tombstoned = True
            return self

        # Per-thread advisory lock (§3.2, §3.3)
        await self._conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", self._thread_id
        )

        # Load thread row
        self._thread_row = await self._conn.fetchrow(
            """SELECT id, account_id, title, slug, created_at, updated_at,
                      open_turn, paused, max_n, current_page, current_page_turns,
                      current_page_bytes, delim
               FROM threads WHERE id = $1 AND account_id = $2""",
            self._thread_id,
            self._account_uuid,
        )
        return self

    async def load_slots(self) -> SlotIndex:
        if not self._conn or self._is_tombstoned:
            return SlotIndex()

        rows = await self._conn.fetch(
            """SELECT n, role, hash, fidelity, chars, turn_key
               FROM turns WHERE thread_id = $1""",
            self._thread_id,
        )
        slots = SlotIndex()
        for r in rows:
            fidelity = _RANK_TO_FIDELITY.get(r["fidelity"], Fidelity.VERBATIM)
            slots.record(
                self._thread_id,
                SlotKey(r["n"], r["role"]),
                r["hash"],
                fidelity,
                r["chars"],
                turn_key=r["turn_key"],
            )
        return slots

    async def match_anchor(self, prev_user_anchor: str) -> Optional[int]:
        """Match latest user turn by normalised anchor (§3.2)."""
        if not self._conn or self._is_tombstoned:
            return None
        norm = normalise_anchor(prev_user_anchor)
        if not norm:
            return None
        row = await self._conn.fetchval(
            """SELECT n FROM turns
               WHERE thread_id = $1 AND role = 'user' AND anchor = $2
               ORDER BY n DESC LIMIT 1""",
            self._thread_id,
            norm,
        )
        return row

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
        if not self._conn or self._is_tombstoned:
            return UpsertResult(action="no_op", n=n)

        anchor = anchor.replace("\x00", "") if anchor else ""
        # Truncate over size limit (§3.4)
        canonical_body = canonical_v1(body)
        if len(canonical_body) > MAX_BODY_CHARS:
            canonical_body = canonical_body[:MAX_BODY_CHARS - 1] + "\n"
            fidelity = Fidelity.TRUNCATED

        body_hash = compute_content_hash(canonical_body)
        chars = len(canonical_body)
        body_bytes_len = len(canonical_body.encode("utf-8"))

        # Determine page (sticky pagination §3.5)
        cur_page = (
            self._staged_meta_updates.get("current_page")
            or (self._thread_row["current_page"] if self._thread_row else 1)
        )
        cur_turns = (
            self._staged_meta_updates.get("current_page_turns")
            or (self._thread_row["current_page_turns"] if self._thread_row else 0)
        )
        cur_bytes = (
            self._staged_meta_updates.get("current_page_bytes")
            or (self._thread_row["current_page_bytes"] if self._thread_row else 0)
        )

        if page is not None:
            turn_page = page
        elif role == "user":
            if (cur_bytes + body_bytes_len > 400_000 or cur_turns + 1 > 60) and cur_turns > 0:
                cur_page += 1
                cur_turns = 0
                cur_bytes = 0

            turn_page = cur_page
            cur_turns += 1
            cur_bytes += body_bytes_len
            self._staged_meta_updates["current_page"] = cur_page
            self._staged_meta_updates["current_page_turns"] = cur_turns
            self._staged_meta_updates["current_page_bytes"] = cur_bytes
        else:
            # Assistant shares page with user of same n (§3.5)
            u_page = await self._conn.fetchval(
                "SELECT page FROM turns WHERE thread_id = $1 AND n = $2 AND role = 'user'",
                self._thread_id,
                n,
            )
            turn_page = u_page if u_page is not None else cur_page

        # Upsert statement in one query (§4.3)
        row = await self._conn.fetchrow(
            """
            INSERT INTO turns (
                thread_id, n, role, body, fidelity, chars, hash,
                recovered, created_at, updated_at, page, turn_key,
                hash_version, anchor
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now(), $9, $10, 1, $11)
            ON CONFLICT (thread_id, n, role) DO UPDATE
            SET body = EXCLUDED.body,
                fidelity = EXCLUDED.fidelity,
                chars = EXCLUDED.chars,
                hash = EXCLUDED.hash,
                recovered = turns.recovered OR EXCLUDED.recovered,
                updated_at = now()
            WHERE turns.hash <> EXCLUDED.hash
              AND (EXCLUDED.fidelity > turns.fidelity
                   OR (EXCLUDED.fidelity = turns.fidelity AND EXCLUDED.chars > turns.chars))
            RETURNING (xmax = 0) AS inserted;
            """,
            self._thread_id,
            n,
            role,
            canonical_body,
            fidelity.rank,
            chars,
            body_hash,
            recovered,
            turn_page,
            turn_key,
            anchor or None,
        )

        cur_max = (
            self._staged_meta_updates.get("max_n")
            or (self._thread_row["max_n"] if self._thread_row else 0)
        )
        if n > cur_max:
            self._staged_meta_updates["max_n"] = n

        if row is not None:
            action = "write_new" if row["inserted"] else "replace"
        else:
            action = "no_op"

        return UpsertResult(action=action, n=n)

    async def write_stubs(self, stubs: list[Stub]) -> None:
        if not self._conn or self._is_tombstoned:
            return

        for stub in stubs:
            body = (
                f'[not archived \u2014 began: "{stub.anchor}"]'
                if stub.anchor
                else "[not archived]"
            )
            body = canonical_v1(body)
            body_hash = compute_content_hash(body)
            chars = len(body)

            # Insert user stub
            await self._conn.execute(
                """
                INSERT INTO turns (
                    thread_id, n, role, body, fidelity, chars, hash,
                    recovered, created_at, updated_at, page, hash_version
                )
                VALUES ($1, $2, 'user', $3, $4, $5, $6, false, now(), now(), 1, 1)
                ON CONFLICT (thread_id, n, role) DO NOTHING;
                """,
                self._thread_id,
                stub.n,
                body,
                Fidelity.STUB.rank,
                chars,
                body_hash,
            )

            # Insert assistant stub
            await self._conn.execute(
                """
                INSERT INTO turns (
                    thread_id, n, role, body, fidelity, chars, hash,
                    recovered, created_at, updated_at, page, hash_version
                )
                VALUES ($1, $2, 'assistant', $3, $4, $5, $6, false, now(), now(), 1, 1)
                ON CONFLICT (thread_id, n, role) DO NOTHING;
                """,
                self._thread_id,
                stub.n,
                body,
                Fidelity.STUB.rank,
                chars,
                body_hash,
            )

            # Register gap
            await self._conn.execute(
                """
                INSERT INTO gaps (thread_id, n, state, requested, first_seen)
                VALUES ($1, $2, 'open', false, now())
                ON CONFLICT (thread_id, n) DO NOTHING;
                """,
                self._thread_id,
                stub.n,
            )

    async def record_gaps(self, gaps: list[int]) -> None:
        if not self._conn or self._is_tombstoned:
            return
        for g in gaps:
            await self._conn.execute(
                """
                INSERT INTO gaps (thread_id, n, state, requested, first_seen)
                VALUES ($1, $2, 'open', false, now())
                ON CONFLICT (thread_id, n) DO NOTHING;
                """,
                self._thread_id,
                g,
            )

    async def recover_gap(self, n: int) -> None:
        if not self._conn or self._is_tombstoned:
            return
        await self._conn.execute(
            "UPDATE gaps SET state = 'recovered' WHERE thread_id = $1 AND n = $2",
            self._thread_id,
            n,
        )

    async def update_thread_meta(self, **fields) -> None:
        self._staged_meta_updates.update(fields)

    async def enqueue_export(self) -> None:
        """Enqueue thread in outbox table with 2-minute debounce (§3.3, §4.2)."""
        if not self._conn or self._is_tombstoned:
            return
        await self._conn.execute(
            """
            INSERT INTO outbox (thread_id, target, due_at, attempts)
            VALUES ($1, 'default', now() + interval '2 minutes', 0)
            ON CONFLICT (thread_id) DO UPDATE SET due_at = now() + interval '2 minutes';
            """,
            self._thread_id,
        )

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                if self._tx:
                    await self._tx.rollback()
                return False

            if self._is_tombstoned or not self._conn:
                if self._tx:
                    await self._tx.rollback()
                return False

            # Update threads table with staged meta updates
            if self._staged_meta_updates:
                clauses = []
                vals = [self._thread_id, self._account_uuid]
                idx = 3
                for k, v in self._staged_meta_updates.items():
                    clauses.append(f"{k} = ${idx}")
                    vals.append(v)
                    idx += 1
                clauses.append("updated_at = now()")

                sql = f"UPDATE threads SET {', '.join(clauses)} WHERE id = $1 AND account_id = $2"
                await self._conn.execute(sql, *vals)

            if self._tx:
                await self._tx.commit()
        finally:
            if self._conn:
                await self._store.pool.release(self._conn)
                self._conn = None


class PgStore:
    """PostgreSQL storage backend for ThreadVault (§4.1, §4.2)."""

    def __init__(
        self,
        dsn: str = "postgresql://postgres:@127.0.0.1:5432/thread_save_test",
        pool: Optional[asyncpg.Pool] = None,
        master_key: Optional[str] = None,
        envelope_encryption_enabled: bool = False,
    ):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = pool
        self._master_key = master_key
        self._envelope_encryption_enabled = envelope_encryption_enabled or (master_key is not None)
        self._account_cache: dict[str, uuid.UUID] = {}
        self._paused_threads: set[str] = set()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PgStore pool not initialized. Call connect() first.")
        return self._pool

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=2, max_size=20, timeout=10
            )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def resolve_account_uuid(
        self, conn: asyncpg.Connection, account_id: str
    ) -> uuid.UUID:
        """Resolve account string to a valid UUID and ensure account row exists."""
        if account_id in self._account_cache:
            return self._account_cache[account_id]

        try:
            acc_uuid = uuid.UUID(account_id)
        except (ValueError, TypeError):
            acc_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"threadvault.account.{account_id}")

        slug = sanitize_slug(account_id, 20)
        existing = await conn.fetchval("SELECT id FROM accounts WHERE slug = $1", slug)
        if existing is not None:
            self._account_cache[account_id] = existing
            return existing

        await conn.execute(
            """
            INSERT INTO accounts (id, oauth_sub, slug, created_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (id) DO UPDATE SET slug = EXCLUDED.slug;
            """,
            acc_uuid,
            f"sub_{account_id}",
            slug,
        )
        self._account_cache[account_id] = acc_uuid
        return acc_uuid

    async def bind_thread(
        self,
        account_id: str,
        thread_id: str | None,
        anchor: str | None,
    ) -> BindResult:
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )

                # 1. Bind by thread_id
                if thread_id:
                    is_del = await conn.fetchval(
                        "SELECT 1 FROM deleted_threads WHERE thread_id = $1 AND account_id = $2",
                        thread_id,
                        acc_uuid,
                    )
                    if is_del:
                        return BindResult(thread_id=thread_id, method="id")

                    exists = await conn.fetchval(
                        "SELECT 1 FROM threads WHERE id = $1 AND account_id = $2",
                        thread_id,
                        acc_uuid,
                    )
                    if exists:
                        return BindResult(thread_id=thread_id, method="id")

                # 2. Bind by anchor (split-never-merge: I-3)
                if anchor:
                    norm = normalise_anchor(anchor)
                    if norm:
                        rows = await conn.fetch(
                            """
                            SELECT t.id
                            FROM threads t
                            JOIN turns u ON u.thread_id = t.id AND u.role = 'user'
                            WHERE t.account_id = $1 AND u.anchor = $2
                            GROUP BY t.id
                            """,
                            acc_uuid,
                            norm,
                        )
                        if len(rows) == 1:
                            return BindResult(thread_id=rows[0]["id"], method="anchor")
                        elif len(rows) > 1:
                            # Multi-match across threads -> new thread
                            return BindResult(thread_id=None, method="new")

                return BindResult(thread_id=None, method="new")

    async def create_thread(
        self,
        account_id: str,
        title_hint: str | None,
        tags: list[str] | None = None,
        client: str = "claude-desktop",
        continues: str | None = None,
    ) -> ThreadMeta:
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )

                now = datetime.now(timezone.utc).astimezone()
                tid = generate_thread_id()
                slug = sanitize_slug(title_hint or "untitled", 20)
                title = (title_hint or "").replace("\x00", "").strip() or "Untitled Thread"
                delim = secrets.token_hex(2)

                await conn.execute(
                    """
                    INSERT INTO threads (
                        id, account_id, title, slug, created_at, updated_at,
                        open_turn, paused, max_n, current_page, current_page_turns,
                        current_page_bytes, delim
                    )
                    VALUES ($1, $2, $3, $4, $5, $5, 0, false, 0, 1, 0, 0, $6);
                    """,
                    tid,
                    acc_uuid,
                    title,
                    slug,
                    now,
                    delim,
                )

                return ThreadMeta(
                    schema_version=2,
                    thread_id=tid,
                    title=title,
                    slug=slug,
                    account=account_id,
                    client=client,
                    created=now,
                    updated=now,
                    page=1,
                    turn_count=0,
                    turn_range=[1, 0],
                    bytes=0,
                    tags=tags or [],
                    nonce=delim,
                    continues=continues,
                )

    def thread_txn(
        self, account_id: str, thread_id: str
    ) -> AsyncContextManager[ThreadTxn]:
        # Pre-resolve account UUID synchronously if cached, else lazily
        try:
            acc_uuid = uuid.UUID(account_id)
        except (ValueError, TypeError):
            acc_uuid = uuid.uuid5(
                uuid.NAMESPACE_DNS, f"threadvault.account.{account_id}"
            )
        return PgThreadTxn(self, account_id, acc_uuid, thread_id)

    async def is_tombstoned(self, account_id: str, thread_id: str) -> bool:
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )
                row = await conn.fetchval(
                    "SELECT 1 FROM deleted_threads WHERE thread_id = $1 AND account_id = $2",
                    thread_id,
                    acc_uuid,
                )
                return bool(row)

    async def delete_thread(self, account_id: str, thread_id: str) -> bool:
        """Tombstone thread and cascade delete (W-10, §3.7)."""
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )
                await conn.execute(
                    """
                    INSERT INTO deleted_threads (thread_id, account_id, deleted_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (thread_id) DO NOTHING;
                    """,
                    thread_id,
                    acc_uuid,
                )
                await conn.execute(
                    "DELETE FROM threads WHERE id = $1 AND account_id = $2",
                    thread_id,
                    acc_uuid,
                )
        return True

    async def delete_account(self, account_id: str) -> bool:
        """Cascade delete full account and all associated data (§2 S8, Milestone X6)."""
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )
                res = await conn.execute(
                    "DELETE FROM accounts WHERE id = $1", acc_uuid
                )
                await conn.execute(
                    "DELETE FROM deleted_threads WHERE account_id = $1", acc_uuid
                )
                self._account_cache.pop(account_id, None)
                return "DELETE 1" in res

    async def set_account_retention(
        self, account_id: str, retention_days: Optional[int]
    ) -> None:
        """Set or update retention period in days for an account (§2 S8)."""
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "UPDATE accounts SET retention_days = $1 WHERE id = $2",
                    retention_days,
                    acc_uuid,
                )

    async def purge_expired_threads(
        self, now: Optional[datetime] = None
    ) -> list[str]:
        """Purge threads exceeding their account's configured retention policy (§2 S8)."""
        purged_ids: list[str] = []
        async with self.pool.acquire() as conn:
            accounts_with_retention = await conn.fetch(
                "SELECT id, retention_days FROM accounts WHERE retention_days IS NOT NULL AND retention_days > 0"
            )
            for acc in accounts_with_retention:
                acc_uuid = acc["id"]
                days = acc["retention_days"]
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                    )
                    expired_threads = await conn.fetch(
                        """
                        SELECT id FROM threads
                        WHERE account_id = $1
                          AND updated_at < (COALESCE($2, now()) - make_interval(days := $3))
                        """,
                        acc_uuid,
                        now,
                        days,
                    )
                    for eth in expired_threads:
                        tid = eth["id"]
                        await conn.execute(
                            """
                            INSERT INTO deleted_threads (thread_id, account_id, deleted_at)
                            VALUES ($1, $2, now())
                            ON CONFLICT (thread_id) DO NOTHING;
                            """,
                            tid,
                            acc_uuid,
                        )
                        await conn.execute(
                            "DELETE FROM threads WHERE id = $1 AND account_id = $2",
                            tid,
                            acc_uuid,
                        )
                        purged_ids.append(tid)

        return purged_ids

    def pause_thread(self, thread_id: str, account: str | None = None) -> None:
        self._paused_threads.add(thread_id)

    def is_thread_paused(self, thread_id: str, account: str | None = None) -> bool:
        return thread_id in self._paused_threads

    async def find(
        self,
        account_id: str,
        query: str | None,
        limit: int = 10,
        titles_only: Optional[bool] = None,
    ) -> list[ThreadHit]:
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )

                q = (query or "").strip()
                if not q:
                    rows = await conn.fetch(
                        """
                        SELECT id, title, updated_at
                        FROM threads
                        WHERE account_id = $1
                        ORDER BY updated_at DESC
                        LIMIT $2
                        """,
                        acc_uuid,
                        limit,
                    )
                    return [
                        ThreadHit(
                            thread_id=r["id"],
                            title=r["title"] or "Untitled Thread",
                            snippet=r["title"] or "Untitled Thread",
                            updated_at=r["updated_at"].isoformat(),
                        )
                        for r in rows
                    ]

                is_titles_only = (
                    titles_only
                    if titles_only is not None
                    else self._envelope_encryption_enabled
                )

                if is_titles_only:
                    # Envelope encryption search (§H6): turn bodies are encrypted, search thread titles only
                    rows = await conn.fetch(
                        """
                        SELECT id, title, updated_at,
                               title as snippet
                        FROM threads
                        WHERE account_id = $1
                          AND title ILIKE '%' || $2 || '%'
                        ORDER BY updated_at DESC
                        LIMIT $3
                        """,
                        acc_uuid,
                        q,
                        limit,
                    )
                    return [
                        ThreadHit(
                            thread_id=r["id"],
                            title=r["title"] or "Untitled Thread",
                            snippet=(r["snippet"] or r["title"] or "")[:200],
                            updated_at=r["updated_at"].isoformat(),
                        )
                        for r in rows
                    ]

                # FTS using turns tsv or title match
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT t.id, t.title, t.updated_at,
                           coalesce(ts_headline('simple', coalesce(u.body, t.title), plainto_tsquery('simple', $2)), t.title) as snippet
                    FROM threads t
                    LEFT JOIN turns u ON u.thread_id = t.id AND u.tsv @@ plainto_tsquery('simple', $2)
                    WHERE t.account_id = $1
                      AND (t.title ILIKE '%' || $2 || '%' OR u.tsv @@ plainto_tsquery('simple', $2))
                    ORDER BY t.updated_at DESC
                    LIMIT $3
                    """,
                    acc_uuid,
                    q,
                    limit,
                )
                return [
                    ThreadHit(
                        thread_id=r["id"],
                        title=r["title"] or "Untitled Thread",
                        snippet=(r["snippet"] or r["title"] or "")[:200],
                        updated_at=r["updated_at"].isoformat(),
                    )
                    for r in rows
                ]

    async def stats(
        self,
        account_id: str,
        thread_id: str | None,
    ) -> Optional[ThreadStats]:
        if not thread_id:
            return None

        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )

                t_row = await conn.fetchrow(
                    "SELECT max_n, open_turn, paused FROM threads WHERE id = $1 AND account_id = $2",
                    thread_id,
                    acc_uuid,
                )
                if not t_row:
                    return None

                turn_rows = await conn.fetch(
                    "SELECT fidelity, count(*) as cnt FROM turns WHERE thread_id = $1 GROUP BY fidelity",
                    thread_id,
                )
                fc = {r["fidelity"]: r["cnt"] for r in turn_rows}

                gaps_open_rows = await conn.fetch(
                    "SELECT n FROM gaps WHERE thread_id = $1 AND state = 'open' ORDER BY n",
                    thread_id,
                )
                gaps_open = _CountList([r["n"] for r in gaps_open_rows])

                gaps_rec_rows = await conn.fetch(
                    "SELECT n FROM gaps WHERE thread_id = $1 AND state = 'recovered' ORDER BY n",
                    thread_id,
                )
                gaps_rec = _CountList([r["n"] for r in gaps_rec_rows])

                total_turns = sum(
                    v for k, v in fc.items() if k != Fidelity.OPEN.rank
                )
                return ThreadStats(
                    total_turns=total_turns,
                    verbatim=fc.get(Fidelity.VERBATIM.rank, 0),
                    abridged=fc.get(Fidelity.ABRIDGED.rank, 0),
                    truncated=fc.get(Fidelity.TRUNCATED.rank, 0),
                    stubs=fc.get(Fidelity.STUB.rank, 0),
                    gaps_open=gaps_open,
                    gaps_recovered=gaps_rec,
                    open_turn=t_row["open_turn"],
                    gaps_open_count=len(gaps_open),
                    gaps_lost_count=0,
                )

    async def get_thread_stats(self, thread_id: str, account_id: str = "default") -> Optional[dict]:
        """Dictionary representation of stats for test compatibility."""
        st = await self.stats(account_id, thread_id)
        if not st:
            return None

        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )
                t_row = await conn.fetchrow(
                    "SELECT title, current_page, current_page_bytes, paused FROM threads WHERE id = $1",
                    thread_id,
                )
                user_cnt = await conn.fetchval(
                    "SELECT count(*) FROM turns WHERE thread_id = $1 AND role = 'user'",
                    thread_id,
                )
                non_stub_user = await conn.fetchval(
                    "SELECT count(*) FROM turns WHERE thread_id = $1 AND role = 'user' AND fidelity <> $2",
                    thread_id,
                    Fidelity.STUB.rank,
                )


        cov = round((non_stub_user / user_cnt * 100), 1) if user_cnt else 0.0

        return {
            "thread_id": thread_id,
            "title": t_row["title"] if t_row else "Untitled",
            "pages": t_row["current_page"] if t_row else 1,
            "total_bytes": t_row["current_page_bytes"] if t_row else 0,
            "total_turns": st.total_turns,
            "open_turn": st.open_turn,
            "fidelity_counts": {
                "verbatim": st.verbatim,
                "abridged": st.abridged,
                "truncated": st.truncated,
                "stub": st.stubs,
                "open": 0,
            },
            "stub": st.stubs,
            "coverage_pct": cov,
            "gaps_open": st.gaps_open,
            "gaps_open_count": st.gaps_open_count,
            "gaps_recovered": st.gaps_recovered,
            "gaps_recovered_count": len(st.gaps_recovered),
            "gaps_lost": _CountList([]),
            "gaps_lost_count": 0,
            "paused": t_row["paused"] if t_row else False,
        }

    async def get_turns(self, account_id: str, thread_id: str) -> list[TurnData]:
        """Fetch all stored turns for a thread as TurnData objects."""
        async with self.pool.acquire() as conn:
            acc_uuid = await self.resolve_account_uuid(conn, account_id)
            rows = await conn.fetch(
                """SELECT n, role, body, fidelity, chars, hash, recovered, created_at, page, turn_key, anchor
                   FROM turns
                   WHERE thread_id = $1
                   ORDER BY n ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END ASC""",
                thread_id,
            )
            turns: list[TurnData] = []
            for r in rows:
                fed = _RANK_TO_FIDELITY.get(r["fidelity"], Fidelity.VERBATIM)
                td = TurnData(
                    turn_index=r["n"],
                    role=r["role"],
                    body=r["body"],
                    timestamp=r["created_at"],
                    model="",
                    fidelity=fed,
                    char_count=r["chars"],
                    content_hash=r["hash"] or "",
                    recovered=r["recovered"],
                    anchor=r["anchor"] or "",
                    turn_key=r["turn_key"] or "",
                )
                turns.append(td)
            return turns

    async def report_missing(self, account_id: str, thread_id: str, limit: int = 10) -> list[int]:
        """Return open unreported gaps up to limit, marking older excess gaps as lost (§reliability plan B2/B3 D2)."""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT n FROM gaps
                WHERE thread_id = $1 AND state = 'open' AND requested = false
                ORDER BY n ASC
                """,
                thread_id,
            )
            unreported = [r["n"] for r in rows]
            if len(unreported) > limit:
                older = unreported[:-limit]
                to_report = unreported[-limit:]
                await conn.execute(
                    "UPDATE gaps SET state = 'lost' WHERE thread_id = $1 AND n = ANY($2::int[])",
                    thread_id,
                    older,
                )
            else:
                to_report = unreported

            if to_report:
                await conn.execute(
                    "UPDATE gaps SET requested = true WHERE thread_id = $1 AND n = ANY($2::int[])",
                    thread_id,
                    to_report,
                )
            return to_report

