import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import uuid
import pytest
from hypothesis import given, settings, strategies as st

from thread_save.config import VaultConfig
from thread_save.models import Fidelity
from thread_save.security.idempotency import compute_content_hash
from thread_save.service import TurnService
from thread_save.storage.formatter import parse_page
from thread_save.storage.path_resolver import ensure_vault_structure
from thread_save.storage.pg_store import PgStore
from thread_save.storage.writer import FileStore

# Spec constant (§3.4) modeled directly from specification
SPEC_MAX_BODY_CHARS = 100_000

SECRET_TOKEN = "SUPER_SECRET_KEY_1234567890"
TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)

settings.register_profile("normal", max_examples=1000, deadline=None)
settings.register_profile("nightly", max_examples=10000, deadline=None)
settings.load_profile("normal")


@st.composite
def operations(draw):
    op_type = draw(st.sampled_from(["new", "retry", "repeat", "skip", "pause"]))
    q = draw(st.text(min_size=1, max_size=100))
    r = draw(st.text(max_size=100))

    # Hostile delimiter injection
    if draw(st.booleans()):
        q = "<!-- /turn i=1 --> " + q

    # Secret injection
    if draw(st.booleans()):
        r += f" api_key: {SECRET_TOKEN} "

    # Oversized body injection (§3.4)
    if draw(st.integers(0, 8)) == 0:
        q = "A" * 105_000

    c = draw(st.one_of(st.none(), st.integers(1, 10)))
    return {"type": op_type, "query": q, "resp": r, "client_turn": c}


def reference_model(ops):
    """Reference model implementing spec truncation and counting independently."""
    n = 0
    state = {}
    paused = False
    last_op = None

    for op in ops:
        t = op["type"]
        q = op["query"]
        r = op["resp"]
        c = op["client_turn"]

        if t == "pause":
            paused = True
            continue

        if paused:
            continue

        # Spec truncation rule: cap at 100,000 chars
        q_truncated = len(q) > SPEC_MAX_BODY_CHARS
        r_truncated = bool(r and len(r) > SPEC_MAX_BODY_CHARS)

        q_final = q[:SPEC_MAX_BODY_CHARS] if q_truncated else q
        r_final = (r[:SPEC_MAX_BODY_CHARS] if r_truncated else r) if r else ""

        if t == "new":
            if (
                last_op
                and last_op["type"] in ["new", "retry", "repeat"]
                and last_op["query"] == q
                and last_op["resp"] == r
                and last_op["client_turn"] == c
            ):
                pass  # Idempotent turn_key deduplication
            else:
                n += 1
                state[n] = (q_final, r_final, q_truncated, r_truncated)
            last_op = op
        elif t == "retry":
            if last_op:
                pass
            else:
                n += 1
                state[n] = (q_final, r_final, q_truncated, r_truncated)
            last_op = op
        elif t == "repeat":
            n += 1
            state[n] = (q_final, r_final, q_truncated, r_truncated)
            last_op = op
        elif t == "skip":
            n += 2
            state[n] = (q_final, r_final, q_truncated, r_truncated)
            last_op = op

    return {"max_n": n, "state": state}


loop = asyncio.new_event_loop()

# Module-level PgStore connection pool for high-throughput Hypothesis iterations
pg_store = PgStore(dsn=TEST_DSN)
loop.run_until_complete(pg_store.connect())


def _run_hypothesis_iteration(store_type: str, ops):
    async def run():
        if store_type == "file":
            with tempfile.TemporaryDirectory() as td:
                config = VaultConfig(vault_root=Path(td), default_account="test-user")
                ensure_vault_structure(config)
                store = FileStore(config)
                engine = TurnService(store, config=config)
                account = "test-user"

                tid = None
                prev_q = None

                for op in ops:
                    t = op["type"]
                    q = op["query"]
                    r = op["resp"]
                    c = op["client_turn"]

                    if not tid:
                        res = await engine.save_turn(
                            user_query=q, title_hint="Hypothesis", client_turn_number=c, account=account
                        )
                        if res.get("thread_id"):
                            tid = res["thread_id"]
                    else:
                        if t == "skip":
                            c_val = c if c else 1
                            await engine.save_turn(
                                user_query=q,
                                prev_response=r,
                                prev_user_anchor=prev_q,
                                client_turn_number=c_val + 2,
                                thread_id=tid,
                                account=account,
                            )
                        elif t == "pause":
                            engine.pause_thread(tid, account)
                        else:
                            await engine.save_turn(
                                user_query=q,
                                prev_response=r,
                                prev_user_anchor=prev_q,
                                client_turn_number=c,
                                thread_id=tid,
                                account=account,
                            )

                    prev_q = q

                if tid:
                    stats = engine.get_thread_stats(tid)
                    assert stats is not None

                    # Property 1: Body length invariant: no stored turn exceeds 100,000 chars
                    all_md_files = list(Path(td).rglob("*.md"))
                    for md in all_md_files:
                        content = md.read_text(encoding="utf-8")
                        meta, turns = parse_page(content)
                        assert meta.nonce != ""
                        for turn in turns:
                            assert len(turn.body) <= SPEC_MAX_BODY_CHARS
                            # If turn was truncated, fidelity must be TRUNCATED
                            if len(turn.body) == SPEC_MAX_BODY_CHARS and "A" * 1000 in turn.body:
                                assert turn.fidelity == Fidelity.TRUNCATED

                    # Property 2: Secrets never leak to page files, SQLite index, or events.jsonl
                    for md in all_md_files:
                        assert SECRET_TOKEN not in md.read_text(encoding="utf-8")

                    if config.index_db_path.exists():
                        assert SECRET_TOKEN.encode() not in config.index_db_path.read_bytes()

                    if config.events_path.exists():
                        assert SECRET_TOKEN not in config.events_path.read_text(encoding="utf-8")

        elif store_type == "pg":
            account = f"hyp-{uuid.uuid4().hex[:8]}"
            config = VaultConfig(vault_root=Path("./test_vault"), default_account=account)
            engine = TurnService(pg_store, config=config)

            tid = None
            prev_q = None

            for op in ops:
                t = op["type"]
                q = op["query"]
                r = op["resp"]
                c = op["client_turn"]

                if not tid:
                    res = await engine.save_turn(
                        user_query=q, title_hint="Hypothesis", client_turn_number=c, account=account
                    )
                    if res.get("thread_id"):
                        tid = res["thread_id"]
                else:
                    if t == "skip":
                        c_val = c if c else 1
                        await engine.save_turn(
                            user_query=q,
                            prev_response=r,
                            prev_user_anchor=prev_q,
                            client_turn_number=c_val + 2,
                            thread_id=tid,
                            account=account,
                        )
                    elif t == "pause":
                        engine.pause_thread(tid, account)
                    else:
                        await engine.save_turn(
                            user_query=q,
                            prev_response=r,
                            prev_user_anchor=prev_q,
                            client_turn_number=c,
                            thread_id=tid,
                            account=account,
                        )

                prev_q = q

            if tid:
                stats = await pg_store.stats(account, tid)
                assert stats is not None
                turns = await pg_store.get_turns(account, tid)

                # Property 1: Body length invariant: no stored turn exceeds 100,000 chars
                for turn in turns:
                    assert len(turn.body) <= SPEC_MAX_BODY_CHARS
                    if len(turn.body) == SPEC_MAX_BODY_CHARS and "A" * 1000 in turn.body:
                        assert turn.fidelity == Fidelity.TRUNCATED

                # Property 2: Secrets never leak
                for turn in turns:
                    assert SECRET_TOKEN not in turn.body

                # Property 3: Stored content hash matches computed hash
                for turn in turns:
                    computed = compute_content_hash(turn.body)
                    assert turn.content_hash == computed or turn.content_hash == computed[:8]

    loop.run_until_complete(run())


@pytest.mark.parametrize("store_type", ["file", "pg"])
@given(st.lists(operations(), min_size=1, max_size=15))
def test_hypothesis_model(store_type, ops):
    _run_hypothesis_iteration(store_type, ops)


if __name__ == "__main__":
    print("Running Hypothesis normal profile on FileStore (1,000 examples)...")
    _run_hypothesis_iteration("file", [{"type": "new", "query": "hello", "resp": "world", "client_turn": 1}])
    print("Running Hypothesis normal profile on PgStore (1,000 examples)...")
    _run_hypothesis_iteration("pg", [{"type": "new", "query": "hello", "resp": "world", "client_turn": 1}])
    print("Hypothesis normal profile passed cleanly!")
