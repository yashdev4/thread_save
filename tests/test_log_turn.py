"""B7 E2–E5: vault_log_turn protocol on FileStore and PgStore.

One call archives one complete turn. The server issues next_turn, caps the
reply (E-budget), never labels model-sent replies verbatim (E-truth), records
skipped turns as not_logged stubs without asking for them (E-gaps), and drops
the reply entirely in user_only mode (E-floor).
"""

from dataclasses import replace
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from thread_save.config import CaptureMode, load_config
from thread_save.fsck import check_pg_fsck_conn, verify_vault
from thread_save.models import Fidelity
from thread_save.service import TurnService, classify_reply
from thread_save.storage.formatter import parse_page
from thread_save.storage.pg_store import PgStore
from thread_save.storage.writer import FileStore

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
def vault():
    path = Path(tempfile.mkdtemp(prefix="tv_logturn_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _svc(vault: Path, **overrides) -> TurnService:
    cfg = replace(load_config(), vault_root=vault, default_account="tester", **overrides)
    return TurnService(FileStore(cfg), config=cfg)


def _slots(vault: Path) -> dict[tuple[int, str], tuple[str, str]]:
    out = {}
    for p in sorted(vault.rglob("*_p[0-9][0-9].md")):
        _, turns = parse_page(p.read_text(encoding="utf-8"))
        for t in turns:
            out[(t.turn_index, t.role)] = (t.fidelity.value, t.body)
    return out


# ── E-truth classifier ────────────────────────────────────────────────────

def test_classify_reply():
    assert classify_reply("Here is the plan.\n\n1. Fly\n2. Swim") == Fidelity.REPORTED
    assert classify_reply("[Extended breakdown covering failure scenarios and gates]") == Fidelity.ABRIDGED
    assert classify_reply(
        "Here's how.\n\n[Provided 10 branch implementations with code, SQL and roadmap]"
    ) == Fidelity.ABRIDGED
    assert classify_reply("Your key is [REDACTED].") == Fidelity.ABRIDGED
    # Links, footnotes and checkboxes are ordinary content
    assert classify_reply("See [the docs](https://example.com/a/very/long/path/to/docs).") == Fidelity.REPORTED
    assert classify_reply("Done [1]\n- [x] item") == Fidelity.REPORTED


# ── FileStore ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_call_writes_both_sides_no_open_slot(vault):
    svc = _svc(vault)
    r = await svc.log_turn(user_message="hello", reply="Hi! How can I help?", title_hint="Greeting")
    assert r["ok"] and r["n"] == 1 and r["next_turn"] == 2 and r["binding"] == "new"
    slots = _slots(vault)
    assert slots[(1, "user")] == ("verbatim", "hello")
    assert slots[(1, "assistant")] == ("reported", "Hi! How can I help?")
    stats = svc.stats(r["thread_id"])
    assert stats["open_turn"] is None
    assert stats["fidelity_counts"]["reported"] == 1
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_chain_by_next_turn_and_legit_repeats(vault):
    svc = _svc(vault)
    r = await svc.log_turn(user_message="tell me a story", reply="Once upon a time...")
    tid = r["thread_id"]
    # "continue" twice is two real turns (W-5), not a retry
    r2 = await svc.log_turn(user_message="continue", reply="The dragon woke.", thread_id=tid, turn=r["next_turn"])
    r3 = await svc.log_turn(user_message="continue", reply="The knight fled.", thread_id=tid, turn=r2["next_turn"])
    assert (r2["n"], r3["n"]) == (2, 3)
    assert r3["next_turn"] == 4
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_retry_is_no_op(vault):
    svc = _svc(vault)
    r1 = await svc.log_turn(user_message="q", reply="a")
    again = await svc.log_turn(user_message="q", reply="a", thread_id=r1["thread_id"])
    assert again["action"] == "no_op" and again["n"] == 1 and again["next_turn"] == 2


@pytest.mark.asyncio
async def test_skipped_turns_become_not_logged_stubs_and_are_never_requested(vault):
    svc = _svc(vault)
    r1 = await svc.log_turn(user_message="q1", reply="a1")
    # The model skipped turns 2 and 3; it reports the turn number it is on
    r4 = await svc.log_turn(user_message="q4", reply="a4", thread_id=r1["thread_id"], turn=4)
    assert r4["n"] == 4
    assert r4["not_logged"] == [2, 3]
    assert "missing" not in r4
    slots = _slots(vault)
    assert slots[(2, "user")][0] == "stub" and slots[(3, "assistant")][0] == "stub"
    stats = svc.stats(r1["thread_id"])
    assert stats["gaps_open"] == [2, 3]
    # Nothing in any later result asks for those turns
    r5 = await svc.log_turn(user_message="q5", reply="a5", thread_id=r1["thread_id"], turn=5)
    assert "missing" not in r5
    assert svc.store.gap_tracker.report_missing(r1["thread_id"]) == []
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_stale_or_absurd_turn_never_overwrites_or_floods(vault):
    svc = _svc(vault)
    r1 = await svc.log_turn(user_message="q1", reply="a1")
    tid = r1["thread_id"]
    stale = await svc.log_turn(user_message="q2", reply="a2", thread_id=tid, turn=1)
    assert stale["n"] == 2
    assert _slots(vault)[(1, "user")] == ("verbatim", "q1")
    flood = await svc.log_turn(user_message="q3", reply="a3", thread_id=tid, turn=10_000)
    assert flood["n"] == 3 and flood["not_logged"] == []


@pytest.mark.asyncio
async def test_reply_budget_truncates(vault):
    svc = _svc(vault, reply_max_chars=100)
    r = await svc.log_turn(user_message="long please", reply="x" * 250)
    assert r["reply_fidelity"] == "truncated"
    fid, body = _slots(vault)[(1, "assistant")]
    assert fid == "truncated" and len(body) == 100


@pytest.mark.asyncio
async def test_default_reply_budget_is_8000(vault):
    svc = _svc(vault)
    assert svc._config.reply_max_chars == 8_000
    await svc.log_turn(user_message="q", reply="y" * 12_000)
    assert len(_slots(vault)[(1, "assistant")][1]) == 8_000


@pytest.mark.asyncio
async def test_summary_reply_is_stored_as_abridged(vault):
    svc = _svc(vault)
    await svc.log_turn(user_message="extend again", reply="[Extended breakdown covering failure scenarios and gates]")
    assert _slots(vault)[(1, "assistant")][0] == "abridged"


@pytest.mark.asyncio
async def test_user_only_mode_drops_reply(vault):
    svc = _svc(vault, capture=CaptureMode.USER_ONLY)
    r = await svc.log_turn(user_message="private question", reply="should not be stored")
    slots = _slots(vault)
    assert slots == {(1, "user"): ("verbatim", "private question")}
    assert r["reply_fidelity"] is None
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_opt_out_pauses(vault):
    svc = _svc(vault)
    r1 = await svc.log_turn(user_message="q1", reply="a1")
    r2 = await svc.log_turn(user_message="please don't save this chat", reply="ok", thread_id=r1["thread_id"])
    assert r2 == {"ok": True, "paused": True}
    r3 = await svc.log_turn(user_message="q3", reply="a3", thread_id=r1["thread_id"])
    assert r3.get("paused") is True
    assert (2, "user") not in _slots(vault)


@pytest.mark.asyncio
async def test_secrets_are_redacted_before_storage(vault):
    svc = _svc(vault)
    await svc.log_turn(
        user_message="my key is sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        reply="Thanks, I won't repeat it.",
    )
    assert "AAAAAAAAAAAAAAAAAAAA" not in _slots(vault)[(1, "user")][1]


# ── E2b: one stored turn per reply, whenever the model calls ─────────────

async def _two_turns(svc):
    r1 = await svc.log_turn(user_message="first question", reply="first answer")
    return r1, dict(thread_id=r1["thread_id"], prev_user_anchor="first question")


@pytest.mark.asyncio
async def test_early_then_end_call_is_one_turn(vault):
    svc = _svc(vault)
    r1, ctx = await _two_turns(svc)
    early = await svc.log_turn(user_message="second question", turn=r1["next_turn"], **ctx)
    end = await svc.log_turn(user_message="second question", reply="second answer",
                             turn=early["next_turn"], **ctx)
    assert (early["n"], end["n"], end["action"], end["next_turn"]) == (2, 2, "merge", 3)
    slots = _slots(vault)
    assert slots[(2, "assistant")] == ("reported", "second answer")
    assert (3, "user") not in slots
    # The next real turn continues at 3
    r3 = await svc.log_turn(user_message="third", reply="a3", thread_id=r1["thread_id"],
                            turn=end["next_turn"], prev_user_anchor="second question")
    assert r3["n"] == 3
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_early_then_end_call_on_first_turn_is_one_turn(vault):
    svc = _svc(vault)
    early = await svc.log_turn(user_message="hello there", title_hint="Hi")
    end = await svc.log_turn(user_message="hello there", reply="Hi! How can I help?",
                             thread_id=early["thread_id"], turn=early["next_turn"])
    assert (end["n"], end["action"]) == (1, "merge")
    assert set(_slots(vault)) == {(1, "user"), (1, "assistant")}


@pytest.mark.asyncio
async def test_placeholder_then_full_reply_keeps_the_full_one(vault):
    svc = _svc(vault)
    r1, ctx = await _two_turns(svc)
    early = await svc.log_turn(user_message="second question", reply="Let me look.",
                               turn=r1["next_turn"], **ctx)
    end = await svc.log_turn(user_message="second question", reply="Here is the full answer.",
                             turn=early["next_turn"], **ctx)
    assert end["n"] == 2
    assert _slots(vault)[(2, "assistant")][1] == "Here is the full answer."
    assert (3, "user") not in _slots(vault)


@pytest.mark.asyncio
async def test_retry_replaces_reply_newest_wins(vault):
    svc = _svc(vault)
    r1, ctx = await _two_turns(svc)
    first = await svc.log_turn(user_message="second question", reply="a long first attempt " * 5,
                               turn=r1["next_turn"], **ctx)
    # Retry: the model's context is rolled back, so it sends the same turn number
    retry = await svc.log_turn(user_message="second question", reply="short retry",
                               turn=r1["next_turn"], **ctx)
    assert (first["n"], retry["n"], retry["action"]) == (2, 2, "merge")
    assert _slots(vault)[(2, "assistant")][1] == "short retry"
    # Network retry of the same call changes nothing
    again = await svc.log_turn(user_message="second question", reply="short retry",
                               turn=r1["next_turn"], **ctx)
    assert (again["n"], again["action"]) == (2, "no_op")
    assert (3, "user") not in _slots(vault)
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_repeat_after_merge_is_no_op(vault):
    svc = _svc(vault)
    r1, ctx = await _two_turns(svc)
    early = await svc.log_turn(user_message="second question", turn=r1["next_turn"], **ctx)
    kw = dict(user_message="second question", reply="second answer", turn=early["next_turn"], **ctx)
    await svc.log_turn(**kw)
    again = await svc.log_turn(**kw)
    assert (again["n"], again["action"]) == (2, "no_op")


@pytest.mark.asyncio
async def test_identical_user_messages_stay_separate_turns(vault):
    svc = _svc(vault)
    r = await svc.log_turn(user_message="tell me a story", reply="Once upon a time...")
    tid, prev = r["thread_id"], "tell me a story"
    ns = []
    for i in range(3):
        r = await svc.log_turn(user_message="continue", reply=f"part {i}", thread_id=tid,
                               turn=r["next_turn"], prev_user_anchor=prev)
        ns.append(r["n"])
        prev = "continue"
    assert ns == [2, 3, 4]
    assert [_slots(vault)[(n, "assistant")][1] for n in ns] == ["part 0", "part 1", "part 2"]


@pytest.mark.asyncio
async def test_without_anchor_a_doubtful_repeat_is_appended(vault):
    svc = _svc(vault)
    r1, ctx = await _two_turns(svc)
    r2 = await svc.log_turn(user_message="continue", reply="part 1", thread_id=r1["thread_id"],
                            turn=r1["next_turn"])
    r3 = await svc.log_turn(user_message="continue", reply="part 2", thread_id=r1["thread_id"],
                            turn=r2["next_turn"])
    # Turn 2 already has a reply and `turn` names a new turn: kept, never overwritten
    assert (r2["n"], r3["n"]) == (2, 3)
    assert _slots(vault)[(2, "assistant")][1] == "part 1"


@pytest.mark.asyncio
async def test_user_only_double_call_is_one_turn(vault):
    svc = _svc(vault, capture=CaptureMode.USER_ONLY)
    r1 = await svc.log_turn(user_message="q1")
    r2 = await svc.log_turn(user_message="q1", thread_id=r1["thread_id"], turn=r1["next_turn"])
    assert (r2["n"], r2["action"]) == (1, "no_op")
    assert _slots(vault) == {(1, "user"): ("verbatim", "q1")}


# ── PgStore ───────────────────────────────────────────────────────────────

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
async def test_pg_log_turn_chain_gaps_and_ranks(pg_store):
    cfg = load_config()
    svc = TurnService(pg_store, config=cfg)
    acct = "logturn-user"
    r1 = await svc.log_turn(user_message="q1", reply="a1", title_hint="PG", account=acct)
    tid = r1["thread_id"]
    r2 = await svc.log_turn(user_message="q2", reply="a2", thread_id=tid, turn=r1["next_turn"], account=acct)
    again = await svc.log_turn(user_message="q2", reply="a2", thread_id=tid, turn=r1["next_turn"], account=acct)
    r5 = await svc.log_turn(user_message="q5", reply="[Provided a long summary of the earlier answer]",
                            thread_id=tid, turn=5, account=acct)

    assert (r1["n"], r2["n"], again["n"], again["action"], r5["n"]) == (1, 2, 2, "no_op", 5)
    assert r5["not_logged"] == [3, 4]

    async with pg_store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT n, role, fidelity FROM turns WHERE thread_id = $1 ORDER BY n, role", tid
        )
        ranks = {(r["n"], r["role"]): r["fidelity"] for r in rows}
        assert ranks[(1, "user")] == Fidelity.VERBATIM.rank == 5
        assert ranks[(1, "assistant")] == Fidelity.REPORTED.rank == 4
        assert ranks[(3, "user")] == Fidelity.STUB.rank
        assert ranks[(5, "assistant")] == Fidelity.ABRIDGED.rank
        violations, _ = await check_pg_fsck_conn(conn)
        assert violations == []

    stats = await pg_store.stats(acct, tid)
    assert stats.reported == 2 and stats.verbatim == 3


@pytest.mark.asyncio
async def test_pg_second_call_for_a_turn_is_merged(pg_store):
    cfg = load_config()
    svc = TurnService(pg_store, config=cfg)
    acct = "logturn-merge"
    r1 = await svc.log_turn(user_message="q1", reply="a1", account=acct)
    tid = r1["thread_id"]
    ctx = dict(thread_id=tid, prev_user_anchor="q1", account=acct)
    early = await svc.log_turn(user_message="q2", turn=r1["next_turn"], **ctx)
    end = await svc.log_turn(user_message="q2", reply="a long second answer", turn=early["next_turn"], **ctx)
    retry = await svc.log_turn(user_message="q2", reply="short", turn=r1["next_turn"], **ctx)
    assert (early["n"], end["n"], end["action"], retry["n"], retry["action"]) == (2, 2, "merge", 2, "merge")

    async with pg_store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT n, role, body FROM turns WHERE thread_id = $1 ORDER BY n, role", tid
        )
        assert [(r["n"], r["role"]) for r in rows] == [(1, "assistant"), (1, "user"), (2, "assistant"), (2, "user")]
        assert rows[2]["body"].strip() == "short"
        violations, _ = await check_pg_fsck_conn(conn)
        assert violations == []
