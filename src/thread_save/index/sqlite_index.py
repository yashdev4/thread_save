"""SQLite FTS5 index for thread search and retrieval (§6).

Markdown files are the source of truth. This DB is 100% derived
and can be deleted and rebuilt from disk at any time.

The index is written inside the same logical operation as the markdown
append. If the index write fails, log and continue — never block
the primary write path.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from thread_save.config import VaultConfig

logger = logging.getLogger("thread_save.index")


class ThreadIndex:
    """SQLite FTS5-backed index for thread search."""

    def __init__(self, config: VaultConfig):
        self._config = config
        self._db_path = config.index_db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_db(self) -> sqlite3.Connection:
        """Lazy-init the database connection and schema."""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # Main threads table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id   TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                slug        TEXT NOT NULL,
                account     TEXT NOT NULL,
                path        TEXT NOT NULL,
                created     TEXT NOT NULL,
                updated     TEXT NOT NULL,
                turn_count  INTEGER DEFAULT 0,
                pages       INTEGER DEFAULT 1,
                gaps_json   TEXT DEFAULT '[]',
                tags_json   TEXT DEFAULT '[]'
            )
        """)

        # FTS5 virtual table over turn text
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                thread_id UNINDEXED,
                turn_index UNINDEXED,
                role UNINDEXED,
                body,
                title,
                tokenize='porter unicode61'
            )
        """)

        self._conn.commit()
        return self._conn

    def upsert_thread(
        self,
        thread_id: str,
        title: str,
        slug: str,
        account: str,
        path: str,
        created: datetime,
        updated: datetime,
        turn_count: int = 0,
        pages: int = 1,
        gaps: list[int] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Insert or update a thread record."""
        try:
            conn = self._ensure_db()
            import json
            conn.execute(
                """
                INSERT INTO threads
                    (thread_id, title, slug, account, path, created, updated,
                     turn_count, pages, gaps_json, tags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title=excluded.title,
                    path=excluded.path,
                    updated=excluded.updated,
                    turn_count=excluded.turn_count,
                    pages=excluded.pages,
                    gaps_json=excluded.gaps_json,
                    tags_json=excluded.tags_json
                """,
                (
                    thread_id, title, slug, account, path,
                    created.isoformat(), updated.isoformat(),
                    turn_count, pages,
                    json.dumps(gaps or []),
                    json.dumps(tags or []),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.warning("Index upsert_thread failed: %s", e)

    def index_turn(
        self,
        thread_id: str,
        turn_index: int,
        role: str,
        body: str,
        title: str = "",
    ) -> None:
        """Add a turn to the FTS5 index."""
        try:
            conn = self._ensure_db()
            conn.execute(
                "INSERT INTO turns_fts (thread_id, turn_index, role, body, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (thread_id, turn_index, role, body, title),
            )
            conn.commit()
        except Exception as e:
            logger.warning("Index index_turn failed: %s", e)

    def search_threads(
        self,
        query: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search threads by keyword or list recent threads.

        Args:
            query: FTS5 query string, or None for recent threads.
            limit: Max results.

        Returns:
            List of thread dicts with thread_id, title, path, updated, snippet.
        """
        try:
            conn = self._ensure_db()
            if query:
                # Search in FTS5 then join with threads table. Every turn row also
                # carries the title, so keep only the best-ranked hit per thread.
                hits = conn.execute(
                    """
                    SELECT t.thread_id, t.title, t.path, t.updated,
                           snippet(turns_fts, 3, '<mark>', '</mark>', '...', 32)
                    FROM turns_fts f
                    JOIN threads t ON t.thread_id = f.thread_id
                    WHERE turns_fts MATCH ?
                    ORDER BY t.updated DESC, f.rank
                    LIMIT ?
                    """,
                    (query, limit * 50),
                ).fetchall()
                seen: set[str] = set()
                rows = []
                for r in hits:
                    if r[0] not in seen:
                        seen.add(r[0])
                        rows.append(r)
                rows = rows[:limit]
            else:
                rows = conn.execute(
                    """
                    SELECT thread_id, title, path, updated, ''
                    FROM threads
                    ORDER BY updated DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

            return [
                {
                    "thread_id": r[0],
                    "title": r[1],
                    "path": r[2],
                    "updated": r[3],
                    "snippet": r[4],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning("Index search failed: %s", e)
            return []

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
