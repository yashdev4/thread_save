"""Test suite for Local Vault Migration CLI (Milestone X10).

Verifies:
- Dry-run scanning and report accuracy without database writes
- Full migration from Markdown vault files into PostgreSQL (PgStore)
- Idempotent re-runs without duplication or errors
- Accurate turn and thread attributes (multi-page, stubs, gaps, recovered, truncated)
- Canonical rendering of migrated PostgreSQL rows matches Plan v2 specifications
- CLI invocation via subprocess
"""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

from thread_save.cli.migrate import import_local_vault
from thread_save.models import Fidelity
from thread_save.storage.pg_store import PgStore
from thread_save.storage.renderer import render_thread_markdown

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)
# Built fresh for this module: a checked-out folder could be stale or hold real data
FIXTURE_VAULT = Path(tempfile.gettempdir()) / "tv_migration_fixture"


@pytest.fixture(scope="module", autouse=True)
def rich_fixture_vault():
    sys.path.insert(0, str(Path(__file__).parent))
    from build_fsck_fixture import build_and_verify_fixture

    assert asyncio.run(build_and_verify_fixture(FIXTURE_VAULT)) == 0
    yield FIXTURE_VAULT
    shutil.rmtree(FIXTURE_VAULT, ignore_errors=True)


@pytest.fixture
async def clean_pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
        # Pre-seed OAuth accounts for tests (§Milestone H8: account must exist)
        await conn.execute(
            "INSERT INTO accounts (id, oauth_sub, slug, created_at) VALUES (gen_random_uuid(), 'sub_imported', 'imported-user', now())"
        )
        await conn.execute(
            "INSERT INTO accounts (id, oauth_sub, slug, created_at) VALUES (gen_random_uuid(), 'sub_test', 'test-account', now())"
        )
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_migration_dry_run(clean_pg_store):
    summary = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        dry_run=True,
        verbose=False,
    )

    assert summary.dry_run is True
    assert summary.success is True
    assert summary.threads_scanned == 2
    assert summary.files_scanned == 5
    assert summary.turns_scanned == 21
    assert summary.threads_imported == 2
    assert summary.turns_imported == 16

    # Verify no records written to database
    async with clean_pg_store.pool.acquire() as conn:
        t_count = await conn.fetchval("SELECT count(*) FROM threads")
        turn_count = await conn.fetchval("SELECT count(*) FROM turns")
        assert t_count == 0
        assert turn_count == 0


@pytest.mark.asyncio
async def test_migration_full_import(clean_pg_store):
    summary = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        dry_run=False,
        verbose=False,
    )

    assert summary.dry_run is False
    assert summary.success is True
    assert summary.threads_scanned == 2
    assert summary.files_scanned == 5
    assert summary.turns_scanned == 21
    assert summary.threads_imported == 2
    assert summary.turns_imported == 16

    # Verify database contents
    async with clean_pg_store.pool.acquire() as conn:
        t_count = await conn.fetchval("SELECT count(*) FROM threads")
        turn_count = await conn.fetchval("SELECT count(*) FROM turns")
        gap_count = await conn.fetchval("SELECT count(*) FROM gaps")

        assert t_count == 2
        assert turn_count == 16
        assert gap_count >= 1

        # Check multi-page thread
        m_row = await conn.fetchrow(
            "SELECT id, title, current_page, max_n FROM threads WHERE current_page >= 3"
        )
        assert m_row is not None
        assert m_row["current_page"] >= 3
        multi_page_tid = m_row["id"]

    # Check turns via get_turns
    turns = await clean_pg_store.get_turns("imported-user", multi_page_tid)
    assert len(turns) == 14  # 14 turns for multi-page thread

    # Check specific user turns exist
    user_turns = {t.turn_index: t for t in turns if t.role == "user"}
    assert 1 in user_turns
    assert 4 in user_turns
    assert user_turns[4].recovered is True


@pytest.mark.asyncio
async def test_migration_idempotency(clean_pg_store):
    # First import
    s1 = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        dry_run=False,
        verbose=False,
    )
    assert s1.success is True

    # Second import into same database
    s2 = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        dry_run=False,
        verbose=False,
    )
    assert s2.success is True
    assert s2.threads_imported == 2
    assert s2.turns_imported == 16

    # Counts must remain unchanged
    async with clean_pg_store.pool.acquire() as conn:
        t_count = await conn.fetchval("SELECT count(*) FROM threads")
        turn_count = await conn.fetchval("SELECT count(*) FROM turns")
        assert t_count == 2
        assert turn_count == 16


@pytest.mark.asyncio
async def test_migration_round_trip_renderer(clean_pg_store):
    await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        dry_run=False,
        verbose=False,
    )

    async with clean_pg_store.pool.acquire() as conn:
        # the multi-page thread (its page count depends on page_max_turns/bytes)
        tid = await conn.fetchval("SELECT id FROM threads WHERE current_page > 1")

    rendered_page1 = await render_thread_markdown(
        clean_pg_store,
        account_id="imported-user",
        thread_id=tid,
        page=1,
    )

    assert "---" in rendered_page1
    assert tid in rendered_page1
    assert "<!-- turn i=1" in rendered_page1
    assert "<!-- /turn i=1" in rendered_page1


def test_migration_cli_subprocess():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "thread_save.cli.migrate",
            "--vault-dir",
            str(FIXTURE_VAULT),
            "--dry-run",
            "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_h8_default_dry_run_requires_apply(clean_pg_store):
    """Milestone H8: default to dry-run without apply=True; require apply=True to write."""
    # When apply is not passed, it defaults to dry-run (no DB writes)
    summary_default = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        verbose=False,
    )
    assert summary_default.dry_run is True
    assert summary_default.success is True

    async with clean_pg_store.pool.acquire() as conn:
        t_count = await conn.fetchval("SELECT count(*) FROM threads")
        assert t_count == 0

    # With apply=True, it writes to the database
    summary_apply = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="imported-user",
        apply=True,
        verbose=False,
    )
    assert summary_apply.dry_run is False
    assert summary_apply.success is True

    async with clean_pg_store.pool.acquire() as conn:
        t_count_after = await conn.fetchval("SELECT count(*) FROM threads")
        assert t_count_after == 2


@pytest.mark.asyncio
async def test_h8_account_slug_must_match_existing_oauth_account(clean_pg_store):
    """Milestone H8: --account-slug must match existing OAuth account in database or exit with error."""
    # Attempt migration targeting a non-existent account
    summary = await import_local_vault(
        vault_dir=FIXTURE_VAULT,
        pg_store=clean_pg_store,
        account_slug="nonexistent-oauth-user",
        apply=True,
        verbose=False,
    )
    assert summary.success is False
    assert len(summary.errors) >= 1
    assert "does not match any existing oauth account" in summary.errors[0].lower()

    # CLI subprocess invocation targeting non-existent account must exit non-zero
    result_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "thread_save.cli.migrate",
            "--vault-dir",
            str(FIXTURE_VAULT),
            "--account-slug",
            "completely-nonexistent-user",
            "--quiet",
        ],
        capture_output=True,
        text=True,
    )
    assert result_cli.returncode != 0
    assert "error" in result_cli.stderr.lower() or "does not match" in result_cli.stderr.lower()
