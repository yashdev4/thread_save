"""Test suite for PostgreSQL Coverage Diagnostics CLI (Milestone D3).

Verifies:
- python -m thread_save.report --dsn ... --thread <id> shows:
  - Per turn: recorded or stub, routine save vs. backfill, fidelity
  - Totals: coverage %, recovered %, first_save_turn
- Same summary for latest N threads (--latest N)
- Subprocess execution exits 0 and prints output format.
"""

import os
from pathlib import Path
import subprocess
import sys
import pytest

from thread_save.config import VaultConfig
from thread_save.report import (
    generate_postgres_thread_report,
    run_postgres_diagnostics,
)
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def seeded_pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
        await conn.execute(
            "INSERT INTO accounts (id, oauth_sub, slug, created_at) VALUES (gen_random_uuid(), 'sub_diag', 'diag-user', now())"
        )

    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="diag-user")
    svc = TurnService(store, config=cfg)

    # Thread 1: Starts at turn 5 (stubs 1..4, routine save at turn 5)
    res1 = await svc.save_turn(
        user_query="Late prompt for thread 1",
        client_turn_number=5,
        title_hint="Diagnostics Thread 1",
        account="diag-user",
    )
    tid1 = res1["thread_id"]

    # Backfill turn 2
    await svc.backfill(
        thread_id=tid1,
        turns=[{
            "n": 2,
            "user_query": "Turn 2 recovered",
            "assistant_response": "Turn 2 reply recovered",
            "fidelity": "verbatim",
        }],
        account="diag-user",
    )

    # Thread 2: Starts normally at turn 1
    res2 = await svc.save_turn(
        user_query="Prompt 1 for thread 2",
        title_hint="Diagnostics Thread 2",
        account="diag-user",
    )
    tid2 = res2["thread_id"]

    yield store, tid1, tid2
    await store.close()


@pytest.mark.asyncio
async def test_d3_postgres_thread_report_details_and_totals(seeded_pg_store):
    store, tid1, _ = seeded_pg_store

    async with store.pool.acquire() as conn:
        rep = await generate_postgres_thread_report(conn, tid1)

    assert "error" not in rep
    assert rep["thread_id"] == tid1
    assert rep["title"] == "Diagnostics Thread 1"

    # Per turn assertions
    turns = rep["turns"]
    assert len(turns) >= 5

    turn_map = {(t["n"], t["role"]): t for t in turns}

    # Turn 1: Stub
    assert turn_map[(1, "user")]["status"] == "stub"
    assert turn_map[(1, "user")]["save_type"] == "stub"
    assert turn_map[(1, "user")]["fidelity"] == "stub"

    # Turn 2: Backfilled
    assert turn_map[(2, "user")]["status"] == "recorded"
    assert turn_map[(2, "user")]["save_type"] == "backfill"
    assert turn_map[(2, "user")]["fidelity"] == "verbatim"

    # Turn 5: Routine save
    assert turn_map[(5, "user")]["status"] == "recorded"
    assert turn_map[(5, "user")]["save_type"] == "routine save"
    assert turn_map[(5, "user")]["fidelity"] == "verbatim"

    # Totals assertions
    tot = rep["totals"]
    assert tot["first_save_turn"] == 5
    assert tot["coverage_pct"] > 0
    assert tot["recovered_pct"] > 0


@pytest.mark.asyncio
async def test_d3_postgres_latest_threads_summary(seeded_pg_store):
    _, tid1, tid2 = seeded_pg_store

    reports = await run_postgres_diagnostics(TEST_DSN, latest=5)
    assert len(reports) == 2

    tids = [r["thread_id"] for r in reports]
    assert tid1 in tids
    assert tid2 in tids

    for r in reports:
        assert "totals" in r
        assert "coverage_pct" in r["totals"]
        assert "recovered_pct" in r["totals"]
        assert "first_save_turn" in r["totals"]


def test_d3_postgres_report_cli_subprocess(seeded_pg_store):
    _, tid1, _ = seeded_pg_store

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "thread_save.report",
            "--dsn",
            TEST_DSN,
            "--thread",
            tid1,
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    out = result.stdout
    assert "Thread Diagnostic Report" in out
    assert tid1 in out
    assert "Coverage" in out
    assert "Recovered" in out
    assert "First Save Turn: Turn 5" in out
    assert "routine save" in out
    assert "backfill" in out
