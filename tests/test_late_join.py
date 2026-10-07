"""Test suite for Late-Join Backfill (Milestone D2).

Verifies:
- If a new thread is created by a call whose client_turn_number > 1:
  - User turn gets n = client_turn_number
  - Stubs written for 1..n-1
  - Returns missing [1..n-1] (capped at 10, older ones marked lost)
- Backfilling missing turns produces a complete thread with 0 fsck violations.
- Verified on FileStore.
"""

from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import pytest

from thread_save.config import load_config
from thread_save.fsck import verify_vault
from thread_save.service import TurnService
from thread_save.storage.writer import FileStore


@pytest.mark.asyncio
async def test_d2_late_join_turn_5_filestore_fsck_clean():
    """First call arrives at client turn 5 -> missing [1,2,3,4] -> backfill -> fsck clean."""
    vault = tempfile.mkdtemp(prefix="tv_late_join_")
    try:
        cfg = replace(load_config(), vault_root=Path(vault))
        store = FileStore(cfg)
        svc = TurnService(store, config=cfg)

        # 1. First call arrives at client turn 5
        res = await svc.save_turn(
            user_query="Late arrival prompt",
            client_turn_number=5,
            title_hint="Late Join Conversation",
        )
        assert res["ok"] is True
        assert res["n"] == 5
        assert res["binding"] == "new"
        assert res["missing"] == [1, 2, 3, 4]
        tid = res["thread_id"]

        # 2. Check stats: stubs created for turns 1..4
        stats = svc.stats(tid)
        assert stats is not None
        assert stats["stub"] == 8  # 4 user stubs + 4 assistant stubs
        assert 1 in stats["gaps_open"]
        assert 4 in stats["gaps_open"]

        # 3. Backfill missing turns 1..4
        backfill_turns = [
            {
                "n": i,
                "user_query": f"Recovered user message {i}",
                "assistant_response": f"Recovered assistant reply {i}",
                "fidelity": "verbatim",
            }
            for i in range(1, 5)
        ]
        bf_res = await svc.backfill(tid, turns=backfill_turns)
        assert bf_res["ok"] is True
        assert bf_res["stored"] == [1, 2, 3, 4]

        # 4. Check stats: stubs now 0, gaps recovered
        stats_after = svc.stats(tid)
        assert stats_after["stub"] == 0
        assert stats_after["gaps_open"] == []
        assert sorted(stats_after["gaps_recovered"]) == [1, 2, 3, 4]

        # 5. Run fsck - must be 100% clean
        fsck_code = verify_vault(Path(vault))
        assert fsck_code == 0
    finally:
        shutil.rmtree(vault, ignore_errors=True)


@pytest.mark.asyncio
async def test_d2_late_join_cap_10_older_marked_lost():
    """First call arrives at turn 15 -> missing capped at 10 (turns 5..14), older (1..4) marked lost."""
    vault = tempfile.mkdtemp(prefix="tv_late_join_cap_")
    try:
        cfg = replace(load_config(), vault_root=Path(vault))
        store = FileStore(cfg)
        svc = TurnService(store, config=cfg)

        # First call arrives at turn 15
        res = await svc.save_turn(
            user_query="Far late arrival prompt",
            client_turn_number=15,
            title_hint="Far Late Join",
        )
        assert res["ok"] is True
        assert res["n"] == 15
        assert "missing" in res
        assert len(res["missing"]) == 10
        # Returns the 10 most recent missing turns: 5..14
        assert res["missing"] == list(range(5, 15))

        # Check gap states in tracker: older (1..4) are marked lost
        tid = res["thread_id"]
        gaps = store._gaps._get_gaps(tid)
        for i in range(1, 5):
            assert gaps[i].state.value == "lost"
        for i in range(5, 15):
            assert gaps[i].requested is True
    finally:
        shutil.rmtree(vault, ignore_errors=True)

