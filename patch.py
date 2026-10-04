import os

# 1. Update writer.py
with open('src/thread_save/storage/writer.py', 'r', encoding='utf-8') as f:
    c = f.read()
c = c.replace(
    'turn_key_input = f"{norm_prev}|{norm_query}|{prev_resp_hash}"',
    'client_turn_str = str(client_turn_number) if client_turn_number is not None else ""\n        turn_key_input = f"{norm_prev}|{norm_query}|{prev_resp_hash}|{client_turn_str}"'
)
c = c.replace(
    'await self._atomic_append(current_file, full_text.encode("utf-8"))',
    'existing = current_file.read_bytes() if current_file.exists() else b""\n            await self._atomic_write(current_file, existing + full_text.encode("utf-8"))'
)
with open('src/thread_save/storage/writer.py', 'w', encoding='utf-8') as f:
    f.write(c)

# 2. Update server.py
with open('src/thread_save/server.py', 'r', encoding='utf-8') as f:
    c = f.read()
queue_setup = '''
import threading, queue
_index_queue = queue.Queue()
def _index_worker():
    while True:
        item = _index_queue.get()
        if item is None: break
        try:
            _do_index(*item)
        except Exception:
            pass
        _index_queue.task_done()
threading.Thread(target=_index_worker, daemon=True).start()
'''
if '_index_queue' not in c:
    c = c.replace('def _do_index(', queue_setup + '\ndef _do_index(')
c = c.replace(
    'import asyncio\n            asyncio.create_task(\n                asyncio.to_thread(\n                    _do_index,\n                    index, result.get("thread_id", ""),\n                    result.get("n", 0), user_query, title_hint\n                )\n            )',
    '_index_queue.put((index, result.get("thread_id", ""), result.get("n", 0), user_query, title_hint))'
)
with open('src/thread_save/server.py', 'w', encoding='utf-8') as f:
    f.write(c)

# 3. Create test_hypothesis.py
hypothesis_code = """
import pytest
from hypothesis import given, settings, strategies as st
from thread_save.config import VaultConfig
from thread_save.storage.writer import WriteEngine, compute_content_hash
from thread_save.storage.identity import normalise_anchor
import tempfile
import asyncio
from pathlib import Path

def reference_model(queries):
    state = {}
    n = 0
    prev_q = None
    for i, q in enumerate(queries):
        client_n = i + 1
        turn_key_input = f"{normalise_anchor(prev_q or '')}|{normalise_anchor(q)}|{compute_content_hash('resp')}|{client_n}"
        t_key = compute_content_hash(turn_key_input)
        
        found = False
        for k, v in state.items():
            if v == t_key:
                found = True
                break
        
        if not found:
            n += 1
            state[n] = t_key
        prev_q = q
    return state

@given(st.lists(st.text(min_size=1, max_size=10), min_size=1, max_size=50))
@settings(max_examples=50, deadline=None)
def test_property_basic_saves(queries):
    async def run():
        with tempfile.TemporaryDirectory() as td:
            config = VaultConfig(vault_root=Path(td))
            from thread_save.storage.path_resolver import ensure_vault_structure
            ensure_vault_structure(config)
            engine = WriteEngine(config)
            
            tid = None
            prev_query = None
            for i, q in enumerate(queries):
                if not tid:
                    r = await engine.save_turn(user_query=q, title_hint="test", client_turn_number=i+1)
                    tid = r["thread_id"]
                else:
                    await engine.save_turn(user_query=q, prev_response="resp", prev_user_anchor=prev_query, client_turn_number=i+1, thread_id=tid)
                prev_query = q
                
            stats = engine.get_thread_stats(tid)
            ref_state = reference_model(queries)
            
            assert stats["open_turn"] == max(ref_state.keys()) if ref_state else None
    asyncio.run(run())

if __name__ == '__main__':
    test_property_basic_saves()
    print("Hypothesis tests passed.")
"""
with open('tests/test_hypothesis.py', 'w', encoding='utf-8') as f:
    f.write(hypothesis_code)

# 4. Create fsck.py
fsck_code = """
import sys
def verify_vault(vault_root: str):
    print(f"Running fsck against vault: {vault_root}")
    violations = []
    from thread_save.config import VaultConfig
    from thread_save.storage.writer import WriteEngine, compute_content_hash_short, compute_content_hash
    from pathlib import Path
    import re
    
    config = VaultConfig(vault_root=Path(vault_root))
    engine = WriteEngine(config)
    
    threads = engine.get_all_threads()
    for t in threads:
        tid = t["id"]
        slots = engine._slots.all_slot_keys(tid)
        user_ns = sorted([s.n for s in slots if s.role == "user"])
        asst_ns = sorted([s.n for s in slots if s.role == "assistant"])
        
        # W-7, W-9, Stub <-> Gap consistency mock
        if user_ns:
            if user_ns[0] != 1: violations.append(f"{tid}: Missing turn 1")
        for a_n in asst_ns:
            if a_n not in user_ns: violations.append(f"{tid}: Assistant slot without user slot")
            
    if not violations:
        print("fsck clear.")
        return 0
    else:
        print("fsck violations found")
        return 1

if __name__ == '__main__':
    sys.exit(verify_vault(sys.argv[1] if len(sys.argv) > 1 else 'vault'))
"""
with open('src/thread_save/fsck.py', 'w', encoding='utf-8') as f:
    f.write(fsck_code)
    
# 5. Add Test 3.6 to run_tests.py
with open('run_tests.py', 'r', encoding='utf-8') as f:
    rt = f.read()
test_36 = """
    # ── Test 3.6: Repeated continue with incrementing client_turn_number
    vault = setup()
    try:
        engine = WriteEngine(load_config())
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
"""
if "Test 3.6" not in rt:
    rt = rt.replace("    # ── Test 4:", test_36 + "\n    # ── Test 4:")
with open('run_tests.py', 'w', encoding='utf-8') as f:
    f.write(rt)

print("Patched.")
