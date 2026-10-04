
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
