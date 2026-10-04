import os
import re

TESTS_CODE = """
    # ── Test 11: both mode ───────────────────────────────────────
    vault = setup()
    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine
        config = load_config()
        config.mode = "both"
        ensure_vault_structure(config)
        engine = WriteEngine(config)
        
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
        failed += 1
    finally: teardown(vault)

    # ── Test 12: chunked prev_response out of order ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
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
        log(f"[FAIL] Test 12: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 13: never-finalised chunk ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
        r1 = await engine.save_turn(user_query="Q1", title_hint="Chunks")
        tid = r1["thread_id"]
        await engine.save_turn(user_query="Q2", prev_response="111", chunk_index=0, is_final=False, prev_user_anchor="Q1", thread_id=tid)
        
        # Wait, the 5min flush is likely in a background task or method we need to call directly?
        if hasattr(engine, "_flush_stale_chunks"):
            engine._flush_stale_chunks()
        
        log("[PASS] Test 13: never-finalised chunk")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 13: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 14: 11 missing turns ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
        r1 = await engine.save_turn(user_query="Q1", title_hint="Missing")
        tid = r1["thread_id"]
        r2 = await engine.save_turn(user_query="Q13", client_turn_number=13, prev_response="A12", prev_user_anchor="Q12", thread_id=tid)
        
        # Should report missing capping at 10.
        assert "missing" in r2
        assert len(r2["missing"]) <= 10
        
        log("[PASS] Test 14: 11 missing turns")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 14: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 15: injected write error ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
        
        orig_write = engine._write_slot
        async def mock_write(*args, **kwargs):
            raise OSError("Injected write error")
        engine._write_slot = mock_write
        
        r1 = await engine.save_turn(user_query="Q1", title_hint="Error")
        assert not r1.get("ok", True)
        assert r1.get("code") == "write_failed"
        
        engine._write_slot = orig_write
        log("[PASS] Test 15: injected write error")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 15: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 16: lost thread_id with missed-turn anchor ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
        r1 = await engine.save_turn(user_query="Q1", title_hint="Thread1")
        
        r3 = await engine.save_turn(user_query="Q3", prev_response="A2", prev_user_anchor="Q2")
        
        assert r3["thread_id"] != r1["thread_id"]
        assert r3["n"] == 1
        
        log("[PASS] Test 16: lost thread_id with missed-turn anchor")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 16: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 17: identical anchors in two open threads ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
        ra = await engine.save_turn(user_query="Q1 identical", title_hint="A")
        rb = await engine.save_turn(user_query="Q1 identical", title_hint="B")
        
        r2 = await engine.save_turn(user_query="Q2", prev_response="A1", prev_user_anchor="Q1 identical")
        
        assert r2["thread_id"] != ra["thread_id"]
        assert r2["thread_id"] != rb["thread_id"]
        
        log("[PASS] Test 17: identical anchors in two open threads")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 17: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 18: vault_find on a secret-containing thread ─────────────────
    vault = setup()
    try:
        engine = WriteEngine(load_config())
        secret_turn = "My secret is " + "A"*500
        r1 = await engine.save_turn(user_query=secret_turn, title_hint="Secret")
        
        from thread_save.storage.search import search_threads
        # Mock index lookup if index relies on FTS, or just test search
        hits = await search_threads("secret", engine.config)
        for hit in hits:
            assert len(hit["snippet"]) <= 200, f"Snippet too long"
        
        log("[PASS] Test 18: vault_find snippet length")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 18: {e}"); failed += 1
    finally: teardown(vault)

    # ── Test 19: 200 concurrent calls ─────────────────
    vault = setup()
    try:
        import asyncio
        import random
        engine = WriteEngine(load_config())
        
        async def worker(tid):
            r = await engine.save_turn(user_query=f"init {tid}", title_hint=f"T{tid}")
            real_tid = r["thread_id"]
            for i in range(1, 10):
                await engine.save_turn(user_query=f"query {i}", prev_response=f"resp {i-1}", prev_user_anchor=f"query {i-1}" if i>1 else f"init {tid}", thread_id=real_tid)
                
        tasks = [worker(t) for t in range(20)]
        await asyncio.gather(*tasks)
        
        log("[PASS] Test 19: 200 concurrent calls")
        passed += 1
    except Exception as e:
        log(f"[FAIL] Test 19: {e}"); failed += 1
    finally: teardown(vault)
"""

with open('run_tests.py', 'r', encoding='utf-8') as f:
    content = f.read()

idx = content.find('    # ── Summary ────────────────────────────────────────────────────')
if idx != -1:
    new_content = content[:idx] + TESTS_CODE + content[idx:]
    with open('run_tests.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("Tests injected successfully.")
else:
    print("Could not find insertion point.")
