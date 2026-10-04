"""Verification suite for Milestone X7 (Outbox Exporter Worker).

Covers:
- Transactional outbox enqueue with 2-minute debounce flush
- Successful asynchronous export to target backend (MockExportTarget)
- Hash-based idempotency (skip redundant export if hash unchanged)
- Fault resilience & exponential backoff on export failures
- Save path isolation: worker failures or worker termination never affect saves ("Done when" condition)
"""

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import pytest

from thread_save.config import VaultConfig
from thread_save.export.worker import MockExportTarget, OutboxWorker
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_x7_outbox_enqueue_and_debounce(pg_store):
    """Verify that saving turns enqueues outbox jobs debounced by 2 minutes."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="export-account")
    svc = TurnService(pg_store, config=cfg)

    # 1. Save turn 1
    res1 = await svc.save_turn(
        user_query="First message to export",
        title_hint="Export Test",
        account="export-account",
    )
    assert res1["ok"]
    tid = res1["thread_id"]

    # Verify outbox entry created with due_at ~2 minutes in future
    async with pg_store.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT due_at, attempts FROM outbox WHERE thread_id = $1", tid)
        assert row is not None
        assert row["attempts"] == 0
        diff = (row["due_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 100 <= diff <= 130  # ~120s (2 minutes)

    # 2. Save turn 2 within 2 minutes: extends debounce
    res2 = await svc.save_turn(
        user_query="Second message within debounce window",
        thread_id=tid,
        account="export-account",
    )
    assert res2["ok"]

    async with pg_store.pool.acquire() as conn:
        row2 = await conn.fetchrow("SELECT due_at FROM outbox WHERE thread_id = $1", tid)
        assert row2 is not None
        diff2 = (row2["due_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 100 <= diff2 <= 130


@pytest.mark.asyncio
async def test_x7_worker_successful_export_and_idempotency(pg_store):
    """Verify worker drains due entries and enforces hash-based idempotency."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="export-account")
    svc = TurnService(pg_store, config=cfg)

    res = await svc.save_turn(
        user_query="Message ready to be exported",
        title_hint="Export Flow",
        account="export-account",
    )
    tid = res["thread_id"]

    mock_target = MockExportTarget()
    worker = OutboxWorker(store=pg_store, targets={"default": mock_target})

    # At current time: not due yet
    processed_0 = await worker.process_batch()
    assert processed_0 == 0
    assert len(mock_target.exports) == 0

    # Simulate 3 minutes later: now due
    future_time = datetime.now(timezone.utc) + timedelta(minutes=3)
    processed_1 = await worker.process_batch(now=future_time)
    assert processed_1 == 1
    assert len(mock_target.exports) == 1
    assert mock_target.exports[0]["thread_id"] == tid
    assert "Message ready to be exported" in mock_target.exports[0]["content"]

    # Outbox is drained
    async with pg_store.pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM outbox WHERE thread_id = $1", tid)
        assert count == 0

    # Idempotency test: Re-insert into outbox with last_hash set to the exported content's hash
    async with pg_store.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO outbox (thread_id, target, due_at, last_hash, attempts)
            VALUES ($1, 'default', now() - interval '10 seconds', $2, 0)
            """,
            tid,
            mock_target.exports[0]["hash"],
        )

    # Worker processes batch again: detects unchanged hash and skips export
    processed_skip = await worker.process_batch()
    assert processed_skip == 1
    # Export target was NOT called again! (still length 1)
    assert len(mock_target.exports) == 1


@pytest.mark.asyncio
async def test_x7_worker_failure_and_save_isolation(pg_store):
    """'Done when' condition: Export errors or worker termination NEVER affect saves."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="resilient-user")
    svc = TurnService(pg_store, config=cfg)

    # 1. Target fails on network error
    failing_target = MockExportTarget(should_fail=True)
    worker = OutboxWorker(store=pg_store, targets={"default": failing_target})

    res = await svc.save_turn(
        user_query="Save during network outage",
        title_hint="Resilient Thread",
        account="resilient-user",
    )
    assert res["ok"] is True
    tid = res["thread_id"]

    # Worker tries to process job 3 minutes later
    future_time = datetime.now(timezone.utc) + timedelta(minutes=3)
    processed = await worker.process_batch(now=future_time)
    assert processed == 0  # Failed

    # Outbox job is still there with incremented attempts and backed off due_at
    async with pg_store.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT attempts, due_at FROM outbox WHERE thread_id = $1", tid)
        assert row is not None
        assert row["attempts"] == 1
        assert row["due_at"] > future_time

    # 2. Saving new turns while exporter is failing or completely offline continues to succeed
    for i in range(2, 6):
        res_i = await svc.save_turn(
            user_query=f"Turn {i} during exporter failure",
            thread_id=tid,
            account="resilient-user",
        )
        assert res_i["ok"] is True
        assert res_i["n"] == i

    # 3. Start background worker loop, terminate abruptly, ensure saves still work
    task = worker.start(poll_interval=0.1)
    await asyncio.sleep(0.2)
    await worker.stop()
    assert task.done()

    # Save immediately after worker stopped
    res_after = await svc.save_turn(
        user_query="Turn saved with worker dead",
        thread_id=tid,
        account="resilient-user",
    )
    assert res_after["ok"] is True
    assert res_after["n"] == 6
