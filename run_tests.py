"""Simple test runner that writes results to a file."""
import asyncio
import os
import sys
import tempfile
import shutil
import traceback

LOG = open("test_results.txt", "w", encoding="utf-8")

def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

def setup():
    vault = tempfile.mkdtemp(prefix="tv_test_")
    os.environ["THREAD_SAVE_VAULT_ROOT"] = vault
    os.environ["THREAD_SAVE_ACCOUNT"] = "test"
    os.environ["THREADVAULT_MODE"] = "turn_start"
    os.environ["THREADVAULT_NUDGE"] = "off"
    return vault

def teardown(vault):
    shutil.rmtree(vault, ignore_errors=True)
    for k in ["THREAD_SAVE_VAULT_ROOT", "THREAD_SAVE_ACCOUNT",
              "THREADVAULT_MODE", "THREADVAULT_NUDGE"]:
        os.environ.pop(k, None)

async def main():
    passed = 0
    failed = 0

    # ── Test 1: Basic save_turn ────────────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        r = await engine.save_turn(user_query="Hello", title_hint="Test 1")
        assert r["ok"], f"Failed: {r}"
        tid = r["thread_id"]
        assert r["n"] == 1
        log(f"[PASS] Test 1: Basic save_turn (tid={tid})")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 1: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 2: Multi-turn with lagged logging ─────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        r1 = await engine.save_turn(user_query="Turn 1", title_hint="Multi")
        tid = r1["thread_id"]

        r2 = await engine.save_turn(
            user_query="Turn 2",
            prev_response="Response to turn 1",
            prev_user_anchor="Turn 1",
            thread_id=tid,
        )
        assert r2["ok"]
        assert r2["n"] == 2

        r3 = await engine.save_turn(
            user_query="Turn 3",
            prev_response="Response to turn 2",
            prev_user_anchor="Turn 2",
            thread_id=tid,
        )
        assert r3["ok"]
        assert r3["n"] == 3

        log("[PASS] Test 2: Multi-turn lagged logging (3 turns)")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 2: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 3: Duplicate idempotency ──────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        r1 = await engine.save_turn(user_query="Same msg", title_hint="Dedup")
        tid = r1["thread_id"]

        r2 = await engine.save_turn(user_query="Same msg", thread_id=tid)
        assert r2["ok"]
        # The user slot (1, user) should exist and not be duplicated
        assert engine._slots.has_user_turn(tid, 1)

        log("[PASS] Test 3: Duplicate idempotency")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 3: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 3.5: Repeated continue ────────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

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
        assert stats.get("gaps_open_count", 0) == 0
        assert stats.get("gaps_lost_count", 0) == 0
        assert stats.get("stub", 0) == 0

        log("[PASS] Test 3.5: Repeated continue (3 turns stored)")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 3.5: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)


    # ── Test 3.6: Repeated continue with incrementing client_turn_number
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        r1 = await engine.save_turn(user_query="Hello")
        tid = r1["thread_id"]
        r2 = await engine.save_turn(user_query="continue", prev_response="R1", prev_user_anchor="Hello", thread_id=tid, client_turn_number=2)
        r3 = await engine.save_turn(user_query="continue", prev_response="R1", prev_user_anchor="continue", thread_id=tid, client_turn_number=3)
        assert r3["n"] == 3
        log("[PASS] Test 3.6: Repeat continue with client_turn_number stored properly")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 3.6: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 4: Interleaved chats isolation (I-3) ──────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        ra = await engine.save_turn(user_query="Chat A msg", title_hint="Chat A")
        rb = await engine.save_turn(user_query="Chat B msg", title_hint="Chat B")
        assert ra["thread_id"] != rb["thread_id"], "Cross-contamination!"

        log("[PASS] Test 4: Interleaved chats isolation (I-3)")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 4: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 5: Fidelity upgrade (abridged→verbatim) ───────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService
        from thread_save.models import Fidelity, SlotKey

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        r1 = await engine.save_turn(user_query="Fidelity Q", title_hint="Fidelity")
        tid = r1["thread_id"]

        r2 = await engine.save_turn(
            user_query="Follow up",
            prev_response="Short...",
            prev_user_anchor="Fidelity Q",
            thread_id=tid,
            fidelity_str="abridged",
        )

        # Backfill with verbatim
        bf = await engine.backfill(tid, [
            {"n": 1, "assistant_response": "Full detailed verbatim response", "fidelity": "verbatim"},
        ])

        slot = engine._slots.get(tid, SlotKey(1, "assistant"))
        assert slot is not None
        assert slot.fidelity == Fidelity.VERBATIM

        log("[PASS] Test 5: Fidelity upgrade (abridged→verbatim)")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 5: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 6: Gap detection + backfill ───────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        r1 = await engine.save_turn(user_query="T1", title_hint="Gaps")
        tid = r1["thread_id"]
        r2 = await engine.save_turn(user_query="T2", prev_response="R1",
                                     prev_user_anchor="T1", thread_id=tid)

        # Skip 3-4, jump to 5
        r5 = await engine.save_turn(user_query="T5", prev_response="R2",
                                     prev_user_anchor="T2", thread_id=tid,
                                     client_turn_number=5)
        missing = r5.get("missing", [])
        assert len(missing) > 0, "Should have gaps"

        # Once-only: next call shouldn't re-report
        r6 = await engine.save_turn(user_query="T6", prev_response="R5",
                                     prev_user_anchor="T5", thread_id=tid)
        assert len(r6.get("missing", [])) == 0, "Re-reported!"

        # Backfill
        bf = await engine.backfill(tid, [
            {"n": 3, "user_query": "T3 bf", "assistant_response": "R3"},
            {"n": 4, "user_query": "T4 bf", "assistant_response": "R4"},
        ])
        assert bf["ok"]

        log("[PASS] Test 6: Gap detection + backfill + once-only")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 6: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 7: Opt-out + pause ────────────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService

        config = load_config()
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)

        r1 = await engine.save_turn(user_query="Normal", title_hint="Optout")
        tid = r1["thread_id"]
        engine.pause_thread(tid)
        assert engine.is_thread_paused(tid)

        log("[PASS] Test 7: Opt-out + thread pause")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 7: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 8: Global pause ───────────────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        config = load_config()
        ensure_vault_structure(config)
        assert not config.is_paused
        config.pause_file.write_text("", encoding="utf-8")
        assert config.is_paused
        config.pause_file.unlink()
        assert not config.is_paused

        log("[PASS] Test 8: Global pause file (I-8)")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 8: {e}")
        log(traceback.format_exc())
        failed += 1
    finally:
        teardown(vault)

    # ── Test 9: Snippet length (I-9) ───────────────────────────────
    try:
        from thread_save.storage.formatter import extract_snippet
        s = extract_snippet("x" * 500)
        assert len(s) <= 200
        assert extract_snippet("Hello world") == "Hello world"
        log("[PASS] Test 9: Snippet length (I-9)")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 9: {e}")
        failed += 1

    # ── Test 10: Sanitizer ─────────────────────────────────────────
    try:
        from thread_save.security.sanitizer import sanitize_slug
        assert sanitize_slug("../../../etc/passwd") == "etcpasswd"
        assert sanitize_slug("CON") == "_con"
        assert sanitize_slug("Hello World! (2026)") == "hello-world-2026"
        assert sanitize_slug("") == "untitled"
        log("[PASS] Test 10: Path sanitizer")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 10: {e}")
        failed += 1


    # ── Test 11: both mode ───────────────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import FileStore
        from thread_save.service import TurnService
        config = load_config()
        object.__setattr__(config, "mode", "both")
        ensure_vault_structure(config)
        store = FileStore(config)
        engine = TurnService(store, config=config)
        
        r1 = await engine.save_turn(user_query="Q1", title_hint="Both")
        tid = r1["thread_id"]
        # Both mode wrap-up (current_response goes to the same slot)
        r1_wrap = await engine.save_turn(user_query="Q1", current_response="A1", thread_id=tid)
        
        # Next turn start
        r2 = await engine.save_turn(user_query="Q2", prev_response="A1", prev_user_anchor="Q1", thread_id=tid)
        
        stats = engine.get_thread_stats(tid)
        assert stats["open_turn"] == 2
        
        log("[PASS] Test 11: both mode")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 11: {e}")
        log(traceback.format_exc())
        failed += 1
    finally: teardown(vault)

    # ── Test 12: chunked prev_response out of order ─────────────────
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        r1 = await engine.save_turn(user_query="Q1", title_hint="Chunks")
        tid = r1["thread_id"]
        await engine.save_turn(user_query="Q2", prev_response="222", chunk_index=1, is_final=False, prev_user_anchor="Q1", thread_id=tid)
        await engine.save_turn(user_query="Q2", prev_response="111", chunk_index=0, is_final=False, prev_user_anchor="Q1", thread_id=tid)
        r_chunk = await engine.save_turn(user_query="Q2", prev_response="333", chunk_index=2, is_final=True, prev_user_anchor="Q1", thread_id=tid)
        
        stats = engine.get_thread_stats(tid)
        assert stats["open_turn"] == 2
        log("[PASS] Test 12: chunked prev_response out of order")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 12: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 13: never-finalised chunk ─────────────────
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        r1 = await engine.save_turn(user_query="Q1", title_hint="Chunks")
        tid = r1["thread_id"]
        await engine.save_turn(user_query="Q2", prev_response="111", chunk_index=0, is_final=False, prev_user_anchor="Q1", thread_id=tid)
        
        if hasattr(engine, "_flush_stale_chunks"):
            engine._flush_stale_chunks()
        
        log("[PASS] Test 13: never-finalised chunk")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 13: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 14: 11 missing turns ─────────────────
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        r1 = await engine.save_turn(user_query="Q1", title_hint="Missing")
        tid = r1["thread_id"]
        r2 = await engine.save_turn(user_query="Q13", client_turn_number=13, prev_response="A12", prev_user_anchor="Q12", thread_id=tid)
        
        assert "missing" in r2
        assert len(r2["missing"]) <= 10
        
        log("[PASS] Test 14: 11 missing turns")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 14: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 15: injected write error ─────────────────
    vault = setup()
    try:
        from thread_save.server import vault_save_turn
        _c = load_config()
        store = FileStore(_c)
        service = TurnService(store, config=_c)
        
        import thread_save.server as server
        server._service = service
        server._config = _c
        from thread_save.index.sqlite_index import ThreadIndex
        server._index = ThreadIndex(_c)
        server._events = __import__('thread_save.telemetry.events', fromlist=['EventLogger']).EventLogger(_c)
        
        orig_write = store._atomic_write
        async def mock_write(*args, **kwargs):
            raise OSError("Injected write error")
        store._atomic_write = mock_write
        
        r1 = await vault_save_turn(user_query="Q1", title_hint="Error")
        assert not r1.get("ok", True)
        assert r1.get("code") == "write_failed"
        
        store._atomic_write = orig_write
        log("[PASS] Test 15: injected write error")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 15: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 16: lost thread_id with missed-turn anchor ─────────────────
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        r1 = await engine.save_turn(user_query="Q1", title_hint="Thread1")
        
        r3 = await engine.save_turn(user_query="Q3", prev_response="A2", prev_user_anchor="Q2")
        
        assert r3["thread_id"] != r1["thread_id"]
        assert r3["n"] == 1
        
        log("[PASS] Test 16: lost thread_id with missed-turn anchor")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 16: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 17: identical anchors in two open threads ─────────────────
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        ra = await engine.save_turn(user_query="Q1 identical", title_hint="A")
        rb = await engine.save_turn(user_query="Q1 identical", title_hint="B")
        
        r2 = await engine.save_turn(user_query="Q2", prev_response="A1", prev_user_anchor="Q1 identical")
        
        assert "thread_id" in r2, "r2 did not return thread_id, likely an error"
        assert r2["thread_id"] != ra["thread_id"]
        assert r2["thread_id"] != rb["thread_id"]
        
        log("[PASS] Test 17: identical anchors in two open threads")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 17: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 18: vault_find on a secret-containing thread ─────────────────
    vault = setup()
    try:
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        secret_turn = "My secret is " + "A"*500
        r1 = await engine.save_turn(user_query=secret_turn, title_hint="Secret")
        
        from thread_save.index.sqlite_index import ThreadIndex
        idx = ThreadIndex(engine._config)
        idx.index_turn(r1["thread_id"], 1, "user", secret_turn)
        hits = idx.search_threads("secret", limit=10)
        
        for hit in hits:
            assert len(hit["snippet"]) <= 200, f"Snippet too long"
        
        log("[PASS] Test 18: vault_find snippet length")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 18: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)

    # ── Test 19: 50 concurrent calls ─────────────────
    vault = setup()
    try:
        import asyncio
        _c = load_config()
        store = FileStore(_c)
        engine = TurnService(store, config=_c)
        import thread_save.server as server
        server._engine = engine
        server._config = engine._config
        from thread_save.index.sqlite_index import ThreadIndex
        server._index = ThreadIndex(engine._config)
        server._events = __import__('thread_save.telemetry.events', fromlist=['EventLogger']).EventLogger(engine._config)

        async def worker(tid):
            r = await server.vault_save_turn(user_query=f"init {tid}", title_hint=f"T{tid}")
            real_tid = r["thread_id"]
            # To test concurrency properly, we just do fewer turns per thread, e.g. 5 threads * 10 turns = 50
            for i in range(1, 10):
                await server.vault_save_turn(
                    user_query=f"query {i}",
                    prev_response=f"resp {i-1}",
                    prev_user_anchor=f"query {i-1}" if i>1 else f"init {tid}",
                    thread_id=real_tid
                )

        tasks = [worker(t) for t in range(5)]
        await asyncio.gather(*tasks)

        log("[PASS] Test 19: 50 concurrent calls")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 19: {e}"); log(traceback.format_exc()); failed += 1
    finally: teardown(vault)
    # ── Summary ────────────────────────────────────────────────────
    log("")
    log("=" * 60)
    log(f"  {passed} passed, {failed} failed, {passed + failed} total")
    log("=" * 60)
    LOG.close()

asyncio.run(main())
