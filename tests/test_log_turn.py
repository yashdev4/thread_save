"""B7 E2–E5: vault_log_turn protocol on FileStore.

One call archives one complete turn. The server issues next_turn, caps the
reply (E-budget), never labels model-sent replies verbatim (E-truth), records
skipped turns as not_logged stubs without asking for them (E-gaps), and drops
the reply entirely in user_only mode (E-floor).
"""

from dataclasses import replace
from pathlib import Path
import shutil
import tempfile

import pytest

from thread_save.config import CaptureMode, load_config
from thread_save.fsck import verify_vault
from thread_save.models import Fidelity
from thread_save.service import TurnService, classify_reply, reply_shape
from thread_save.storage.formatter import parse_page
from thread_save.storage.writer import FileStore


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


# P1-15: real replies from chat eatz7d were one line of ~800-1050 chars like this
FLATTENED = (
    "Five key themes from September Study Time sessions: (1) company-wide server migration "
    "nearly done (per-user setup emails); (2) onboarding and QA mostly good, version 4 prep "
    "on Sep 30; (3) task ownership and workload rules set Sep 8; (4) data-quality issues "
    "with duplicate invoices and missing product mapping; (5) access-tier leak in salary "
    "data. Speakers: Sheenam, Ayan, Kriti, Priyanshi, Harsh, Yash. " * 2
)


def test_reply_shape_and_flattened_retellings():
    assert reply_shape(FLATTENED) == "flattened"
    assert classify_reply(FLATTENED) == Fidelity.ABRIDGED
    inline_list = (
        "Summary of the call with the vendor: (1) pricing agreed at the lower tier; "
        "(2) delivery moves to March; (3) support contract still open and owned by finance; "
        "follow-up next week with both teams, and the legal review of the renewal clause."
    )
    assert 200 < len(inline_list) < 600 and reply_shape(inline_list) == "flattened"
    # Short one-liners and real paragraphs are ordinary replies
    assert reply_shape("Pick (1) red or (2) blue.") == "plain"
    assert reply_shape("word " * 100) == "plain"
    assert reply_shape("First paragraph.\n\nSecond paragraph.") == "plain"
    assert reply_shape("## Plan\n\n- fly\n- swim") == "markdown"
    assert reply_shape("```py\nx = 1\n```") == "markdown"
    assert classify_reply("## Plan\n\n- fly\n- swim") == Fidelity.REPORTED


@pytest.mark.asyncio
async def test_flattened_reply_is_stored_as_abridged(vault):
    svc = _svc(vault)
    r = await svc.log_turn(user_message="give me 5 key points", reply=FLATTENED)
    assert (r["reply_fidelity"], r["reply_shape"]) == ("abridged", "flattened")
    assert _slots(vault)[(1, "assistant")][0] == "abridged"
    assert svc.stats(r["thread_id"])["fidelity_counts"]["abridged"] == 1


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


