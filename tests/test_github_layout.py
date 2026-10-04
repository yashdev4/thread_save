"""Tests for GitHub Archive Layout and Deterministic Indexes (Milestone GH2).

Verifies:
- Golden files for README.md, index/threads.json, and monthly indexes
- Relative markdown links in monthly indexes correctly target ../YYYY/MM/...
- Differential assertion: same rows from FileStore and PgStore produce identical export trees
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import pytest

from thread_save.config import VaultConfig
from thread_save.export.layout import (
    ExportThreadInfo,
    extract_filestore_export_tree,
    extract_pgstore_export_tree,
    generate_archive_tree,
    generate_monthly_index,
    generate_readme,
    generate_threads_json,
)
from thread_save.models import Fidelity, ThreadMeta, TurnData
from thread_save.security.idempotency import compute_content_hash_short
from thread_save.storage.formatter import format_front_matter, format_turn
from thread_save.storage.path_resolver import build_filename, ensure_vault_structure
from thread_save.storage.pg_store import PgStore
import os

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def clean_pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
        await conn.execute(
            "INSERT INTO accounts (id, oauth_sub, slug, created_at) VALUES (gen_random_uuid(), 'sub_testuser', 'testuser', now())"
        )
    yield store
    await store.close()


def test_readme_golden():
    """Verify README.md contains required guidelines, conflict notices, and squash CLI commands."""
    readme = generate_readme()

    assert "# ThreadVault Archive" in readme
    assert "Do Not Edit Directly:" in readme
    assert "Privacy Notice:" in readme
    assert "Deletion Semantics:" in readme
    assert "python -m thread_save.cli.github squash" in readme
    assert "threads.json" in readme
    assert "YYYY-MM.md" in readme



def test_threads_json_golden():
    """Verify index/threads.json format is deterministically structured and sorted."""
    dt1 = datetime(2026, 10, 2, 14, 23, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 4, 9, 15, 0, tzinfo=timezone.utc)

    threads = [
        ExportThreadInfo(
            thread_id="01M444NQWT0R5QZB9KE26DH74B",
            title="Thread B",
            slug="thread-b",
            account="testuser",
            created_at=dt2,
            updated_at=dt2,
            turns=2,
            pages=["2026/10/2026-10-04T0915_testuser_6dh74b_thread-b_p01.md"],
        ),
        ExportThreadInfo(
            thread_id="01M444NQWT0R5QZB9KE26DH74A",
            title="Thread A",
            slug="thread-a",
            account="testuser",
            created_at=dt1,
            updated_at=dt1,
            turns=4,
            pages=["2026/10/2026-10-02T1423_testuser_6dh74a_thread-a_p01.md"],
        ),
    ]

    out = generate_threads_json(threads)
    parsed = json.loads(out)

    # Deterministic keys sorted by thread_id
    keys = list(parsed.keys())
    assert keys == ["01M444NQWT0R5QZB9KE26DH74A", "01M444NQWT0R5QZB9KE26DH74B"]

    t_a = parsed["01M444NQWT0R5QZB9KE26DH74A"]
    assert t_a["title"] == "Thread A"
    assert t_a["slug"] == "thread-a"
    assert t_a["account"] == "testuser"
    assert t_a["turns"] == 4
    assert t_a["pages"] == ["2026/10/2026-10-02T1423_testuser_6dh74a_thread-a_p01.md"]


def test_monthly_index_golden():
    """Verify generated index/YYYY-MM.md produces relative markdown links."""
    dt = datetime(2026, 10, 2, 14, 23, 0, tzinfo=timezone.utc)

    threads = [
        ExportThreadInfo(
            thread_id="01M444NQWT0R5QZB9KE26DH74A",
            title="Postgres Bulk Inserts",
            slug="postgres-bulk-inserts",
            account="testuser",
            created_at=dt,
            updated_at=dt,
            turns=6,
            pages=[
                "2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p01.md",
                "2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p02.md",
            ],
        )
    ]

    index_md = generate_monthly_index(2026, 10, threads)

    assert "# Threads — 2026-10" in index_md
    assert "## [Postgres Bulk Inserts](../2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p01.md)" in index_md
    assert "- **Thread ID:** `01M444NQWT0R5QZB9KE26DH74A`" in index_md
    assert "- **Turns:** 6" in index_md
    assert "[Page 1](../2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p01.md)" in index_md
    assert "[Page 2](../2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p02.md)" in index_md


@pytest.mark.asyncio
async def test_filestore_and_pgstore_produce_identical_trees(clean_pg_store: PgStore):
    """Differential test: same conversation in FileStore and PgStore produces 100% identical trees."""
    account = "testuser"
    tid = "01M444NQWT0R5QZB9KE26DH74A"
    dt = datetime(2026, 10, 2, 14, 23, 0, tzinfo=timezone.utc)
    slug = "postgres-bulk-insert"
    delim = "7a8b"

    # 1. Populate FileStore vault
    with tempfile.TemporaryDirectory() as td:
        vault_root = Path(td)
        cfg = VaultConfig(vault_root=vault_root, default_account=account)
        ensure_vault_structure(cfg)

        year = f"{dt.year:04d}"
        month = f"{dt.month:02d}"
        thread_dir = vault_root / account / year / month
        thread_dir.mkdir(parents=True, exist_ok=True)

        filename = build_filename(dt, account, tid[-6:], slug, 1)
        page_file = thread_dir / filename

        turns = [
            TurnData(
                turn_index=1,
                role="user",
                body="How do I bulk insert into postgres?",
                timestamp=dt,
                model="",
                fidelity=Fidelity.VERBATIM,
                char_count=35,
                content_hash=compute_content_hash_short("How do I bulk insert into postgres?"),
                anchor="how do i bulk insert into postgres?",
                turn_key="key1",
            ),
            TurnData(
                turn_index=1,
                role="assistant",
                body="Use copy_records_to_table for high performance.",
                timestamp=dt,
                model="",
                fidelity=Fidelity.VERBATIM,
                char_count=48,
                content_hash=compute_content_hash_short("Use copy_records_to_table for high performance."),
            ),
        ]

        meta = ThreadMeta(
            schema_version=2,
            thread_id=tid,
            title="Postgres Bulk Inserts",
            slug=slug,
            account=account,
            client="claude-desktop",
            created=dt,
            updated=dt,
            page=1,
            turn_count=2,
            turn_range=[1, 1],
            bytes=0,
            tags=[],
            nonce=delim,
            open_turn=None,
            paused=False,
        )

        turns_text = "\n\n".join([format_turn(t, nonce=delim) for t in turns])
        body_text = f"\n\n{turns_text}\n"
        meta.bytes = len((format_front_matter(meta) + body_text).encode("utf-8"))
        full_content = format_front_matter(meta) + body_text
        page_file.write_text(full_content, encoding="utf-8")

        # 2. Populate PgStore
        async with clean_pg_store.pool.acquire() as conn:
            acc_uuid = await clean_pg_store.resolve_account_uuid(conn, account)
            await conn.execute("SELECT set_config('app.account_id', $1, true)", str(acc_uuid))
            await conn.execute(
                """INSERT INTO threads (
                    id, account_id, title, slug, created_at, updated_at,
                    open_turn, paused, max_n, current_page, current_page_turns,
                    current_page_bytes, delim
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
                tid,
                acc_uuid,
                "Postgres Bulk Inserts",
                slug,
                dt,
                dt,
                None,
                False,
                1,
                1,
                2,
                meta.bytes,
                delim,
            )

            for t in turns:
                await conn.execute(
                    """INSERT INTO turns (
                        thread_id, n, role, body, fidelity, chars, hash,
                        recovered, created_at, updated_at, page, turn_key, anchor
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
                    tid,
                    t.turn_index,
                    t.role,
                    t.body,
                    t.fidelity.rank,
                    t.char_count,
                    t.content_hash,
                    t.recovered,
                    t.timestamp,
                    t.timestamp,
                    1,
                    t.turn_key or "",
                    t.anchor or "",
                )

        # 3. Extract trees from both stores
        tree_file = extract_filestore_export_tree(vault_root, account)
        tree_pg = await extract_pgstore_export_tree(clean_pg_store, account)

        # 4. Compare trees
        assert set(tree_file.keys()) == set(tree_pg.keys()), (
            f"Paths differ: file={set(tree_file.keys())} vs pg={set(tree_pg.keys())}"
        )

        for path in sorted(tree_file.keys()):
            assert tree_file[path] == tree_pg[path], f"File content mismatch at {path}"
