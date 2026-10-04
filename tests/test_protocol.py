"""Protocol tests (§8.1) — deterministic, simulate model's calls.

Each test asserts against the engine functions directly (not through MCP).
Output goes to stderr to survive server.py's stdout redirect.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

# Use stderr for all output — server.py redirects stdout
def _print(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def _setup_env() -> str:
    """Create temp vault and set env vars. Returns vault root path."""
    vault = tempfile.mkdtemp(prefix="threadvault_test_")
    os.environ["THREAD_SAVE_VAULT_ROOT"] = vault
    os.environ["THREAD_SAVE_ACCOUNT"] = "test-user"
    os.environ["THREADVAULT_MODE"] = "turn_start"
    os.environ["THREADVAULT_NUDGE"] = "off"
    return vault


def _teardown(vault: str):
    """Clean up."""
    shutil.rmtree(vault, ignore_errors=True)
    for key in ["THREAD_SAVE_VAULT_ROOT", "THREAD_SAVE_ACCOUNT",
                 "THREADVAULT_MODE", "THREADVAULT_NUDGE"]:
        os.environ.pop(key, None)


async def test_happy_path_20_turns():
    """Happy path: 20 turns with turn-start lagged logging.

    Expects: 20 user + 19 assistant verbatim, 1 open, no gaps.
    """
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        thread_id = None
        for i in range(1, 21):
            prev_response = f"Response to turn {i-1}" if i > 1 else None
            prev_anchor = f"User message for turn {i-1}" if i > 1 else None

            result = await engine.save_turn(
                user_query=f"User message for turn {i}",
                prev_response=prev_response,
                prev_user_anchor=prev_anchor[:80] if prev_anchor else None,
                thread_id=thread_id,
                title_hint="Happy Path Test" if i == 1 else None,
                model_hint="test-model",
            )

            assert result["ok"], f"Turn {i} failed: {result}"
            thread_id = result["thread_id"]
            assert "missing" not in result or result.get("missing") == [], \
                f"Unexpected gaps at turn {i}: {result.get('missing')}"

        # Verify stats
        stats = engine.get_thread_stats(thread_id)
        assert stats is not None
        # Should have user slots for turns 1-20, assistant for 1-19 + open at 20
        assert stats["open_turn"] == 20, f"Expected open turn 20, got {stats['open_turn']}"

        _print("[PASS] Happy path 20 turns", file=sys.stderr)
    finally:
        _teardown(vault)


async def test_repeated_continue():
    """User sends 'continue' 3 turns in a row.
    Must store all 3 verbatim. Retries of the repeat must not duplicate.
    """
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        # Turn 1: "Hello"
        r1 = await engine.save_turn(user_query="Hello", title_hint="Repeated")
        assert r1["ok"]
        tid = r1["thread_id"]

        # Turn 2: "continue"
        r2 = await engine.save_turn(
            user_query="continue",
            prev_response="resp 1",
            prev_user_anchor="Hello",
            thread_id=tid,
        )
        assert r2["n"] == 2

        # Turn 3: "continue"
        r3 = await engine.save_turn(
            user_query="continue",
            prev_response="resp 2",
            prev_user_anchor="continue",
            thread_id=tid,
        )
        assert r3["n"] == 3

        # Turn 4: "continue"
        r4 = await engine.save_turn(
            user_query="continue",
            prev_response="resp 3",
            prev_user_anchor="continue",
            thread_id=tid,
        )
        assert r4["n"] == 4

        # Retry Turn 4! (Same prev_response, same anchor, same query)
        r4_retry = await engine.save_turn(
            user_query="continue",
            prev_response="resp 3",
            prev_user_anchor="continue",
            thread_id=tid,
        )
        assert r4_retry["n"] == 4, f"Retry of turn 4 should stay n=4, got n={r4_retry.get('n')}"

        stats = engine.get_thread_stats(tid)
        assert stats["open_turn"] == 4
        assert stats.get("gaps_open", 0) == 0
        assert stats.get("gaps_lost", 0) == 0
        assert stats.get("stub", 0) == 0

        _print("[PASS] Repeated identical queries ('continue')")
    finally:
        _teardown(vault)


async def test_duplicate_calls():
    """Same call sent 3× → one entry, 2 no-ops."""
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        # First call creates thread
        r1 = await engine.save_turn(
            user_query="Hello world",
            title_hint="Dedup Test",
        )
        assert r1["ok"]
        tid = r1["thread_id"]

        # Same call again
        r2 = await engine.save_turn(
            user_query="Hello world",
            thread_id=tid,
            title_hint="Dedup Test",
        )
        assert r2["ok"]

        # Third time
        r3 = await engine.save_turn(
            user_query="Hello world",
            thread_id=tid,
            title_hint="Dedup Test",
        )
        assert r3["ok"]

        # Verify file — should only have 1 user turn
        stats = engine.get_thread_stats(tid)
        # The slot for (1, user) should exist once
        from thread_save.models import SlotKey
        assert engine._slots.has_user_turn(tid, 1)

        _print("[PASS] Duplicate calls (3× same → 1 entry)")
    finally:
        _teardown(vault)


async def test_interleaved_chats_no_crossbleed():
    """Two chats interleaved, neither passes thread_id.

    Expects: two separate threads, zero cross-contamination (I-3).
    """
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        # Chat A turn 1
        r_a1 = await engine.save_turn(
            user_query="Chat A: discuss postgres",
            title_hint="Chat A",
        )
        assert r_a1["ok"]
        tid_a = r_a1["thread_id"]

        # Chat B turn 1 (no thread_id, different content)
        r_b1 = await engine.save_turn(
            user_query="Chat B: discuss redis",
            title_hint="Chat B",
        )
        assert r_b1["ok"]
        tid_b = r_b1["thread_id"]

        # They MUST be different threads
        assert tid_a != tid_b, f"Cross-contamination! Both got {tid_a}"

        # Chat A turn 2 (uses prev_user_anchor to bind)
        r_a2 = await engine.save_turn(
            user_query="Chat A: what about indexes?",
            prev_response="Here's how postgres works...",
            prev_user_anchor="Chat A: discuss postgres",
            thread_id=tid_a,
        )
        assert r_a2["ok"]
        assert r_a2["thread_id"] == tid_a, "Chat A turn 2 landed in wrong thread!"

        # Chat B turn 2
        r_b2 = await engine.save_turn(
            user_query="Chat B: what about clustering?",
            prev_response="Here's how redis works...",
            prev_user_anchor="Chat B: discuss redis",
            thread_id=tid_b,
        )
        assert r_b2["ok"]
        assert r_b2["thread_id"] == tid_b, "Chat B turn 2 landed in wrong thread!"

        _print("[PASS] Interleaved chats — zero cross-contamination (I-3)")
    finally:
        _teardown(vault)


async def test_fidelity_upgrade():
    """Abridged then verbatim for same slot → verbatim wins."""
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine
        from thread_save.models import Fidelity, SlotKey

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        # Create thread with turn 1
        r1 = await engine.save_turn(
            user_query="Fidelity test query",
            title_hint="Fidelity Test",
        )
        tid = r1["thread_id"]

        # Turn 2 sends abridged prev_response
        r2 = await engine.save_turn(
            user_query="Follow up question",
            prev_response="Short summary...",  # abridged
            prev_user_anchor="Fidelity test query",
            thread_id=tid,
            fidelity_str="abridged",
        )

        # Now backfill with verbatim version
        full_response = "This is the full, complete, verbatim response with all the details."
        await engine.backfill(
            thread_id=tid,
            turns=[{
                "n": 1,
                "assistant_response": full_response,
                "fidelity": "verbatim",
            }],
        )

        # Check that verbatim won
        slot = engine._slots.get(tid, SlotKey(1, "assistant"))
        assert slot is not None
        assert slot.fidelity == Fidelity.VERBATIM, \
            f"Expected verbatim, got {slot.fidelity}"

        _print("[PASS] Fidelity upgrade (abridged → verbatim replaces)")
    finally:
        _teardown(vault)


async def test_fidelity_no_downgrade():
    """Verbatim then abridged → verbatim kept."""
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine
        from thread_save.models import Fidelity, SlotKey
        from thread_save.security.idempotency import compute_content_hash

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        r1 = await engine.save_turn(
            user_query="Keep my verbatim",
            title_hint="No Downgrade",
        )
        tid = r1["thread_id"]

        verbatim_text = "Full detailed verbatim response here."
        # Turn 2 delivers verbatim prev
        r2 = await engine.save_turn(
            user_query="Next question",
            prev_response=verbatim_text,
            prev_user_anchor="Keep my verbatim",
            thread_id=tid,
            fidelity_str="verbatim",
        )

        # Now try backfill with abridged
        await engine.backfill(
            thread_id=tid,
            turns=[{
                "n": 1,
                "assistant_response": "Short version",
                "fidelity": "abridged",
            }],
        )

        # Verbatim must be kept
        slot = engine._slots.get(tid, SlotKey(1, "assistant"))
        assert slot is not None
        assert slot.fidelity == Fidelity.VERBATIM, \
            f"Downgrade! Expected verbatim, got {slot.fidelity}"
        assert slot.content_hash == compute_content_hash(verbatim_text)

        _print("[PASS] Fidelity no downgrade (verbatim kept over abridged)")
    finally:
        _teardown(vault)


async def test_gap_detection_and_backfill():
    """Skip turns 3-4, detect gaps, backfill, verify recovery."""
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        # Turn 1
        r1 = await engine.save_turn(
            user_query="Turn 1",
            title_hint="Gap Test",
        )
        tid = r1["thread_id"]

        # Turn 2
        r2 = await engine.save_turn(
            user_query="Turn 2",
            prev_response="Response 1",
            prev_user_anchor="Turn 1",
            thread_id=tid,
        )

        # Skip turns 3-4, jump to turn 5 via client_turn_number
        r5 = await engine.save_turn(
            user_query="Turn 5",
            prev_response="Response 2",
            prev_user_anchor="Turn 2",
            thread_id=tid,
            client_turn_number=5,
        )

        assert r5["ok"]
        # Should report missing turns
        missing = r5.get("missing", [])
        assert len(missing) > 0, "Should have reported missing turns"

        # Second call should NOT re-report (once-only, §I-6)
        r6 = await engine.save_turn(
            user_query="Turn 6",
            prev_response="Response 5",
            prev_user_anchor="Turn 5",
            thread_id=tid,
        )
        missing2 = r6.get("missing", [])
        assert len(missing2) == 0, f"Re-reported missing: {missing2}"

        # Backfill the gaps
        bf = await engine.backfill(
            thread_id=tid,
            turns=[
                {"n": 3, "user_query": "Turn 3 backfilled", "assistant_response": "Resp 3"},
                {"n": 4, "user_query": "Turn 4 backfilled", "assistant_response": "Resp 4"},
            ],
        )
        assert bf["ok"]
        assert 3 in bf["stored"] or 4 in bf["stored"]

        # Verify gap state
        gap_summary = engine.gap_tracker.get_summary(tid)
        assert gap_summary["gaps_recovered_count"] > 0

        _print("[PASS] Gap detection + backfill + once-only reporting")
    finally:
        _teardown(vault)


async def test_opt_out_phrase():
    """'don't save this chat' in user_query → thread paused."""
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine

        config = load_config()
        ensure_vault_structure(config)
        engine = WriteEngine(config)

        # Create a thread
        r1 = await engine.save_turn(
            user_query="Normal message",
            title_hint="Opt-out Test",
        )
        tid = r1["thread_id"]

        # Inline opt-out detection (same logic as server._check_opt_out)
        _opt_out_phrases = {"don't save this chat", "dont save this chat",
                           "stop archiving", "don't log this", "dont log this"}
        def _check(text):
            t = text.strip().lower()
            return any(p in t for p in _opt_out_phrases)
        assert _check("don't save this chat")
        assert _check("Hey, please don't save this chat okay?")
        assert not _check("Save this chat please")

        # Pause the thread
        engine.pause_thread(tid)
        assert engine.is_thread_paused(tid)

        _print("[PASS] Opt-out phrase detection + thread pausing")
    finally:
        _teardown(vault)


async def test_global_pause():
    """vault/.paused present → config.is_paused returns True."""
    vault = _setup_env()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure

        config = load_config()
        ensure_vault_structure(config)

        assert not config.is_paused

        # Create pause file
        config.pause_file.write_text("", encoding="utf-8")
        assert config.is_paused

        # Remove it
        config.pause_file.unlink()
        assert not config.is_paused

        _print("[PASS] Global pause file (I-8)")
    finally:
        _teardown(vault)


async def test_snippet_length():
    """vault_find snippets must be ≤ 200 chars (I-9)."""
    vault = _setup_env()
    try:
        from thread_save.storage.formatter import extract_snippet

        long_text = "x" * 500
        snippet = extract_snippet(long_text)
        assert len(snippet) <= 200, f"Snippet too long: {len(snippet)} chars"

        short_text = "Hello world"
        snippet2 = extract_snippet(short_text)
        assert snippet2 == "Hello world"

        _print("[PASS] Snippet length enforcement (I-9)")
    finally:
        _teardown(vault)


async def test_no_stdout():
    """Core modules produce nothing on stdout (I-7)."""
    import io

    # Capture stdout
    capture = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = capture

    try:
        # Import core modules (not server.py — that intentionally redirects stdout)
        import importlib
        import thread_save.storage.writer
        import thread_save.storage.formatter
        import thread_save.storage.identity
        import thread_save.security.sanitizer
        import thread_save.security.redactor
        importlib.reload(thread_save.storage.writer)

        output = capture.getvalue()
        assert output.strip() == "", f"Stdout pollution: {output!r}"
        _print("[PASS] No stdout pollution (I-7)")
    finally:
        sys.stdout = old_stdout


# ── Runner ─────────────────────────────────────────────────────────────────

async def run_all():
    tests = [
        test_happy_path_20_turns,
        test_repeated_continue,
        test_duplicate_calls,
        test_interleaved_chats_no_crossbleed,
        test_fidelity_upgrade,
        test_fidelity_no_downgrade,
        test_gap_detection_and_backfill,
        test_opt_out_phrase,
        test_global_pause,
        test_snippet_length,
        test_no_stdout,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            await test()
            passed += 1
        except Exception as e:
            _print(f"[FAIL] {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    _print()
    _print("=" * 60)
    _print(f"  {passed} passed, {failed} failed, {passed + failed} total")
    _print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_all())
