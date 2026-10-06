"""B6 L1/L2/L4: FileStore survives a process restart and never leaves header-only files.

Each "restart" is a fresh FileStore + TurnService on the same vault directory,
which is what a new Claude Desktop stdio process (or a Render redeploy) sees.
"""

from dataclasses import replace
from pathlib import Path
import re
import shutil
import tempfile

import pytest

from thread_save.config import load_config
from thread_save.fsck import verify_vault
from thread_save.service import TurnService
from thread_save.storage.formatter import parse_page
from thread_save.storage.writer import FileStore


@pytest.fixture
def vault():
    path = Path(tempfile.mkdtemp(prefix="tv_restart_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _service(vault: Path, **overrides) -> TurnService:
    cfg = replace(load_config(), vault_root=vault, default_account="tester", **overrides)
    return TurnService(FileStore(cfg), config=cfg)


def _pages(vault: Path) -> list[Path]:
    return sorted(vault.rglob("*_p[0-9][0-9].md"))


def _turns(vault: Path) -> list[tuple[int, str, str, str]]:
    out = []
    for p in _pages(vault):
        _, turns = parse_page(p.read_text(encoding="utf-8"))
        out += [(t.turn_index, t.role, t.fidelity.value, t.body) for t in turns]
    return out


@pytest.mark.asyncio
async def test_log_turn_resumes_same_file_after_restart(vault):
    svc = _service(vault)
    r1 = await svc.log_turn(user_message="plan my trip to Goa", reply="Here is a 5-day plan.", title_hint="Goa trip")
    r2 = await svc.log_turn(
        user_message="make it 3 days", reply="Here is the 3-day version.",
        thread_id=r1["thread_id"], turn=r1["next_turn"], prev_user_anchor="plan my trip to Goa",
    )

    svc2 = _service(vault)  # process restart
    r3 = await svc2.log_turn(
        user_message="add beaches", reply="Added Baga and Palolem.",
        thread_id=r1["thread_id"], turn=r2["next_turn"], prev_user_anchor="make it 3 days",
    )

    assert r3["thread_id"] == r1["thread_id"]
    assert r3["binding"] == "id"
    assert r3["n"] == 3
    assert len(_pages(vault)) == 1
    bodies = [b for _, _, _, b in _turns(vault)]
    assert "Here is the 3-day version." in bodies
    assert "Added Baga and Palolem." in bodies
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_retry_after_restart_is_no_op(vault):
    svc = _service(vault)
    args = dict(user_message="hello", reply="hi there", title_hint="Retry")
    r1 = await svc.log_turn(**args)
    before = _pages(vault)[0].read_text(encoding="utf-8")

    svc2 = _service(vault)
    r1_again = await svc2.log_turn(thread_id=r1["thread_id"], **args)

    assert r1_again["action"] == "no_op"
    assert r1_again["n"] == 1
    assert _pages(vault)[0].read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_legacy_save_turn_restart_keeps_reply_and_thread(vault):
    """The reported bug: restart split the chat and dropped prev_response."""
    svc = _service(vault)
    r1 = await svc.save_turn(user_query="first question", title_hint="Legacy")
    await svc.save_turn(
        user_query="second question", prev_response="first answer",
        prev_user_anchor="first question", thread_id=r1["thread_id"],
    )

    svc2 = _service(vault)
    r3 = await svc2.save_turn(
        user_query="third question", prev_response="second answer",
        prev_user_anchor="second question", thread_id=r1["thread_id"],
    )

    assert r3["thread_id"] == r1["thread_id"]
    assert r3["n"] == 3
    assert len(_pages(vault)) == 1
    turns = _turns(vault)
    assert (2, "assistant", "verbatim", "second answer") in turns  # the open slot was filled
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_anchor_only_resume_after_restart(vault):
    """thread_id lost by the client: bind by the previous user message via active.json."""
    svc = _service(vault)
    long_msg = "this opening message is deliberately longer than forty characters to test anchors"
    r1 = await svc.save_turn(user_query=long_msg, title_hint="Anchor")

    svc2 = _service(vault)
    r2 = await svc2.save_turn(user_query="follow up", prev_response="answer", prev_user_anchor=long_msg)

    assert r2["thread_id"] == r1["thread_id"]
    assert r2["binding"] == "anchor"
    assert r2["n"] == 2


@pytest.mark.asyncio
async def test_multi_page_restart_and_front_matter(vault):
    svc = _service(vault, page_max_turns=4)
    tid, nxt = None, None
    for i in range(1, 5):
        r = await svc.log_turn(user_message=f"q{i}", reply=f"a{i}", thread_id=tid, turn=nxt, title_hint="Pages")
        tid, nxt = r["thread_id"], r["next_turn"]

    svc2 = _service(vault, page_max_turns=4)
    r = await svc2.log_turn(user_message="q5", reply="a5", thread_id=tid, turn=nxt)
    assert r["n"] == 5

    pages = _pages(vault)
    assert [re.search(r"_p(\d+)\.md$", p.name).group(1) for p in pages] == ["01", "02", "03"]
    metas = [parse_page(p.read_text(encoding="utf-8"))[0] for p in pages]
    assert [m.page for m in metas] == [1, 2, 3]
    assert metas[0].prev is None
    assert metas[1].prev == pages[0].name
    assert metas[2].prev == pages[1].name
    assert metas[-1].turn_range == [5, 5]
    assert metas[0].turn_range == [1, 2]
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_failed_first_save_leaves_no_file(vault):
    svc = _service(vault)
    store = svc.store

    async def boom(*args, **kwargs):
        raise OSError("disk full")

    real_write = store._atomic_write
    store._atomic_write = boom
    with pytest.raises(OSError):
        await svc.log_turn(user_message="hi", reply="hello", title_hint="Fail")
    assert _pages(vault) == []
    assert store._registry.all_ids() == []

    store._atomic_write = real_write
    r = await svc.log_turn(user_message="hi", reply="hello", title_hint="Fail")
    assert r["n"] == 1
    assert len(_pages(vault)) == 1
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_failed_save_on_existing_thread_rolls_back(vault):
    svc = _service(vault)
    store = svc.store
    r1 = await svc.log_turn(user_message="q1", reply="a1", title_hint="Rollback")
    tid = r1["thread_id"]
    ps_before = store._page_states[tid].model_dump()

    async def boom(*args, **kwargs):
        raise OSError("disk full")

    real_write = store._atomic_write
    store._atomic_write = boom
    with pytest.raises(OSError):
        await svc.log_turn(user_message="q2", reply="a2", thread_id=tid, turn=2)
    assert store._page_states[tid].model_dump() == ps_before

    store._atomic_write = real_write
    r2 = await svc.log_turn(user_message="q2", reply="a2", thread_id=tid, turn=2)
    assert r2["n"] == 2
    assert verify_vault(vault) == 0


@pytest.mark.asyncio
async def test_find_lists_threads_after_restart(vault):
    svc = _service(vault)
    await svc.log_turn(user_message="hello", reply="hi", title_hint="Searchable Title")

    svc2 = _service(vault)
    hits = await svc2.find(query="searchable")
    assert [h.title for h in hits] == ["Searchable Title"]


@pytest.mark.asyncio
async def test_restore_ignores_other_accounts(vault):
    svc = _service(vault)
    r1 = await svc.log_turn(user_message="private", reply="ok", title_hint="Mine")

    other = _service(vault)
    r = await other.log_turn(user_message="x", reply="y", thread_id=r1["thread_id"], account="someone-else")
    assert r["thread_id"] != r1["thread_id"]
    assert r["binding"] == "new"
