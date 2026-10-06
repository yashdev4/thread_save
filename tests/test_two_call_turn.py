"""B7 E8: two calls per reply (start + end) and skipped-turn detection.

P1-16: since the call moved to the end of the reply, models stopped making it on
every turn. The call is now asked for at the start of each reply (user message
only); a second call after the reply adds the reply to the same turn.
P1-17: a skipped call made the turn vanish, because the model sends back the
next_turn it was given. The previous-message anchor now reveals the skip.
"""

import os
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile

import pytest

from thread_save.config import CaptureMode, load_config
from thread_save.fsck import check_pg_fsck_conn
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import parse_page
from thread_save.storage.pg_store import PgStore
from thread_save.storage.writer import FileStore
from thread_save.tools.descriptions import public_result

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)

MSGS = [
    "how do I rotate an API key safely",
    "what about keys used by cron jobs",
    "can you write the migration checklist",
    "now turn that into a runbook",
]


@pytest.fixture
def vault():
    path = Path(tempfile.mkdtemp(prefix="tv_twocall_"))
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


def _users(vault: Path) -> dict[int, str]:
    return {n: body for (n, role), (_, body) in _slots(vault).items() if role == "user"}


async def _reply_turn(svc, msg, reply, *, thread_id=None, turn=None, prev=None, skip_end=False, **kw):
    """Call 1 at the start of the reply, call 2 after it (unless skipped)."""
    start = await svc.log_turn(user_message=msg, thread_id=thread_id, turn=turn,
                               prev_user_anchor=prev, **kw)
    if skip_end:
        return start, None
    end = await svc.log_turn(user_message=msg, reply=reply, thread_id=start["thread_id"],
                             turn=start["n"], prev_user_anchor=prev, **kw)
    return start, end


# ── FileStore ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_calls_per_reply_store_one_complete_turn_each(vault):
    svc = _svc(vault)
    tid, nxt, prev = None, None, None
    for i, msg in enumerate(MSGS, 1):
        start, end = await _reply_turn(svc, msg, f"## Answer {i}\n- step", thread_id=tid, turn=nxt, prev=prev)
        assert (start["n"], start["action"], end["n"], end["action"]) == (i, "write", i, "merge")
        tid, nxt, prev = end["thread_id"], end["next_turn"], msg
    slots = _slots(vault)
    assert sorted(slots) == sorted((n, r) for n in range(1, 5) for r in ("assistant", "user"))
    assert all(slots[(n, "assistant")][0] == "reported" for n in range(1, 5))
    assert len(list(vault.rglob("*_p[0-9][0-9].md"))) == 1


@pytest.mark.asyncio
async def test_skipped_reply_becomes_a_stub_not_a_duplicate_or_a_vanished_turn(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, MSGS[0], "a1")
    tid = e1["thread_id"]
    _, e2 = await _reply_turn(svc, MSGS[1], "a2", thread_id=tid, turn=e1["next_turn"], prev=MSGS[0])
    # Turn 3: no call at all. Turn 4 sends back the stale next_turn (3).
    s4, e4 = await _reply_turn(svc, MSGS[3], "a4", thread_id=tid, turn=e2["next_turn"], prev=MSGS[2])
    assert (s4["n"], s4["not_logged"], e4["n"], e4["action"]) == (4, [3], 4, "merge")
    slots = _slots(vault)
    assert slots[(3, "user")][0] == "stub"
    assert "can you write the migration" in slots[(3, "user")][1]
    assert [n for n, body in _users(vault).items() if body == MSGS[3]] == [4]
    assert slots[(4, "assistant")][1] == "a4"


@pytest.mark.asyncio
async def test_end_call_with_stale_turn_after_a_gap_still_merges(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, MSGS[0], "a1")
    tid = e1["thread_id"]
    s3 = await svc.log_turn(user_message=MSGS[2], thread_id=tid, turn=2, prev_user_anchor=MSGS[1])
    assert (s3["n"], s3["not_logged"]) == (3, [2])
    end = await svc.log_turn(user_message=MSGS[2], reply="a3", thread_id=tid, turn=2,
                             prev_user_anchor=MSGS[1])
    assert (end["n"], end["action"]) == (3, "merge")
    assert sorted(_users(vault)) == [1, 2, 3]


@pytest.mark.asyncio
async def test_missed_end_call_keeps_the_user_side(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, MSGS[0], "a1")
    tid = e1["thread_id"]
    s2, _ = await _reply_turn(svc, MSGS[1], None, thread_id=tid, turn=e1["next_turn"], prev=MSGS[0],
                              skip_end=True)
    # The last result the model saw was call 1's {turn: 2}; it may send 2 again
    _, e3 = await _reply_turn(svc, MSGS[2], "a3", thread_id=tid, turn=s2["n"], prev=MSGS[1])
    # Last turn of the chat: call 2 never comes
    await _reply_turn(svc, MSGS[3], None, thread_id=tid, turn=e3["next_turn"], prev=MSGS[2],
                      skip_end=True)
    slots = _slots(vault)
    assert _users(vault) == {1: MSGS[0], 2: MSGS[1], 3: MSGS[2], 4: MSGS[3]}
    assert (2, "assistant") not in slots and (4, "assistant") not in slots
    assert slots[(3, "assistant")][1] == "a3"


@pytest.mark.asyncio
async def test_retried_end_call_replaces_the_reply_and_repeats_are_no_ops(vault):
    svc = _svc(vault)
    start, _ = await _reply_turn(svc, MSGS[0], "first answer")
    kw = dict(user_message=MSGS[0], thread_id=start["thread_id"], turn=start["n"])
    retry = await svc.log_turn(reply="regenerated answer", **kw)
    again = await svc.log_turn(reply="regenerated answer", **kw)
    assert (retry["n"], retry["action"], again["action"]) == (1, "merge", "no_op")
    assert _slots(vault)[(1, "assistant")][1] == "regenerated answer"
    assert len(_slots(vault)) == 2


@pytest.mark.asyncio
async def test_first_call_mid_chat_keeps_earlier_numbers_for_backfill(vault):
    svc = _svc(vault)
    start, end = await _reply_turn(svc, "save this chat from now on", "Saving from here.",
                                   turn=5, prev=MSGS[3])
    assert (start["n"], start["not_logged"], end["n"], end["action"]) == (5, [1, 2, 3, 4], 5, "merge")
    filled = await svc.backfill(start["thread_id"],
                                [{"n": n, "user_query": m} for n, m in enumerate(MSGS, 1)])
    assert filled["ok"]
    assert _users(vault) == {1: MSGS[0], 2: MSGS[1], 3: MSGS[2], 4: MSGS[3],
                             5: "save this chat from now on"}


@pytest.mark.asyncio
async def test_first_call_mid_chat_without_turn_still_marks_a_gap(vault):
    svc = _svc(vault)
    start, end = await _reply_turn(svc, "save this chat from now on", "Saving.", prev=MSGS[3])
    assert (start["n"], start["not_logged"], end["n"], end["action"]) == (2, [1], 2, "merge")


@pytest.mark.asyncio
async def test_copied_anchor_with_small_differences_is_not_a_gap(vault):
    svc = _svc(vault)
    first = "How do I rotate an API key safely, without downtime for the cron jobs?"
    _, e1 = await _reply_turn(svc, first, "a1")
    tid, prevs = e1["thread_id"], (
        "How do I rotate an API key safely",                                     # cut short
        "how do i rotate an api key safely without downtime for the cron jobs",  # punctuation
        "How do I rotate an API key safley, without downtime for the cron jobs?",  # typo
    )
    for i, prev in enumerate(prevs):
        s = await svc.log_turn(user_message=f"follow-up {i}", thread_id=tid, turn=e1["next_turn"],
                               prev_user_anchor=prev)
        assert s["not_logged"] == [], prev
        # undo: each probe is its own turn 2 candidate; keep the thread at one turn
        shutil.rmtree(vault, ignore_errors=True)
        svc = _svc(vault)
        _, e1 = await _reply_turn(svc, first, "a1")
        tid = e1["thread_id"]


@pytest.mark.asyncio
async def test_skipped_end_call_is_filled_by_the_next_start_call(vault):
    """E8-7: every call 1 carries the reply of the turn before; it fills a skipped call 2."""
    svc = _svc(vault)
    s1, _ = await _reply_turn(svc, MSGS[0], None, skip_end=True)
    tid = s1["thread_id"]
    s2 = await svc.log_turn(user_message=MSGS[1], thread_id=tid, turn=s1["n"] + 1,
                            prev_user_anchor=MSGS[0], last_reply="## Answer 1\n- step")
    assert (s2["n"], s2["recovered_reply"], s2["not_logged"]) == (2, 1, [])
    slots = _slots(vault)
    assert slots[(1, "assistant")] == ("reported", "## Answer 1\n- step")
    assert (2, "assistant") not in slots


@pytest.mark.asyncio
async def test_last_reply_never_overwrites_a_logged_reply(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, MSGS[0], "a1")
    s2 = await svc.log_turn(user_message=MSGS[1], thread_id=e1["thread_id"], turn=e1["next_turn"],
                            prev_user_anchor=MSGS[0], last_reply="something else")
    assert s2["recovered_reply"] is None
    assert _slots(vault)[(1, "assistant")][1] == "a1"


@pytest.mark.asyncio
async def test_last_reply_is_dropped_in_user_only_mode(vault):
    svc = _svc(vault, capture=CaptureMode.USER_ONLY)
    r1 = await svc.log_turn(user_message=MSGS[0])
    r2 = await svc.log_turn(user_message=MSGS[1], thread_id=r1["thread_id"], turn=r1["next_turn"],
                            prev_user_anchor=MSGS[0], last_reply="a1")
    assert r2["recovered_reply"] is None
    assert all(role == "user" for (_, role) in _slots(vault))


def test_results_name_the_next_step():
    start = {"ok": True, "thread_id": "T", "n": 3, "next_turn": 4, "awaiting_reply": True,
             "action": "write"}
    assert public_result(start, "vault_local_log_turn") == {
        "ok": True, "thread_id": "T", "turn": 3,
        "then": "This turn is archived without your reply until you call vault_local_log_turn "
                "with thread_id=T, turn=3 and reply, as the last step of this reply.",
    }
    end = {**start, "awaiting_reply": False}
    assert public_result(end) == {
        "ok": True, "thread_id": "T", "next_turn": 4,
        "next": "At the start of your next reply, call vault_log_turn with thread_id=T and turn=4.",
    }


@pytest.mark.asyncio
async def test_service_marks_call_1_and_user_only_gets_next_turn(vault):
    full = await _svc(vault).log_turn(user_message="q1")
    assert full["awaiting_reply"] is True and public_result(full)["turn"] == 1
    other = Path(tempfile.mkdtemp(prefix="tv_twocall_uo_"))
    try:
        r = await _svc(other, capture=CaptureMode.USER_ONLY).log_turn(user_message="q1")
        pub = public_result(r)
        assert "then" not in pub and pub["next_turn"] == 2
    finally:
        shutil.rmtree(other, ignore_errors=True)


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
async def test_pg_two_calls_with_a_skipped_reply_and_a_mid_chat_start(pg_store):
    svc = TurnService(pg_store, config=load_config())
    acct = "twocall-e8"
    _, e1 = await _reply_turn(svc, MSGS[0], "a1", account=acct)
    tid = e1["thread_id"]
    # Turn 2 skipped entirely; turn 3 sends the stale next_turn (2)
    s3, e3 = await _reply_turn(svc, MSGS[2], "a3", thread_id=tid, turn=e1["next_turn"], prev=MSGS[1],
                               account=acct)
    assert (s3["n"], s3["not_logged"], e3["n"], e3["action"]) == (3, [2], 3, "merge")
    # A second chat whose first call comes at its 4th message
    m, me = await _reply_turn(svc, "save from here", "ok", turn=4, prev=MSGS[3], account=acct)
    assert (m["n"], m["not_logged"], me["n"], me["action"]) == (4, [1, 2, 3], 4, "merge")

    async with pg_store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT n, role, fidelity, body FROM turns WHERE thread_id = $1 ORDER BY n, role", tid
        )
        users = {r["n"]: (r["fidelity"], r["body"].strip()) for r in rows if r["role"] == "user"}
        assert users[2][0] == Fidelity.STUB.rank
        assert [n for n, (_, b) in users.items() if b == MSGS[2]] == [3]
        assert {(r["n"], r["role"]) for r in rows} >= {(3, "assistant")}
        violations, _ = await check_pg_fsck_conn(conn)
        assert violations == []


# ── P1-18: a call that lost its thread_id continues the chat's file ───────

def _files(vault):
    return sorted(p.name for p in vault.rglob("*_p[0-9][0-9].md"))


@pytest.mark.asyncio
async def test_end_call_of_the_first_turn_without_thread_id_stays_in_the_same_file(vault):
    svc = _svc(vault)
    start = await svc.log_turn(user_message=MSGS[0])
    end = await svc.log_turn(user_message=MSGS[0], reply="a1", turn=start["n"])
    assert (end["thread_id"], end["n"], end["action"], end["binding"]) == (start["thread_id"], 1, "merge", "recent")
    assert len(_files(vault)) == 1


@pytest.mark.asyncio
async def test_next_turn_without_thread_id_and_a_retyped_anchor_stays_in_the_same_file(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, "can u tell me about the prority projects present in odoo board", "list")
    # The model drops the id and corrects the typo when copying the opening words
    s2 = await svc.log_turn(user_message="more", turn=e1["next_turn"],
                            prev_user_anchor="can u tell me about the priority projects present in odoo board")
    assert (s2["thread_id"], s2["n"], s2["binding"], s2["not_logged"]) == (e1["thread_id"], 2, "recent", [])
    assert len(_files(vault)) == 1


@pytest.mark.asyncio
async def test_mangled_thread_id_is_recovered(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, MSGS[0], "a1")
    tid = e1["thread_id"]
    wrong = tid[:5] + ("A" if tid[5] != "A" else "B") + tid[6:]
    s2 = await svc.log_turn(user_message=MSGS[1], thread_id=wrong, turn=2, prev_user_anchor=MSGS[0])
    assert (s2["thread_id"], s2["binding"]) == (tid, "id_fuzzy")
    assert len(_files(vault)) == 1


@pytest.mark.asyncio
async def test_a_new_chat_never_joins_a_recent_one(vault):
    svc = _svc(vault)
    _, e1 = await _reply_turn(svc, "save this chat", "saved")
    s = await svc.log_turn(user_message="save this chat")  # first call of a new chat
    assert s["thread_id"] != e1["thread_id"] and s["binding"] == "new"


@pytest.mark.asyncio
async def test_two_recent_chats_with_the_same_latest_message_are_not_guessed(vault):
    svc = _svc(vault)
    _, a = await _reply_turn(svc, "more", "a")
    _, b = await _reply_turn(svc, "more", "b")
    s = await svc.log_turn(user_message="next question", turn=2, prev_user_anchor="more")
    assert s["thread_id"] not in (a["thread_id"], b["thread_id"])

