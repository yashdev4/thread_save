import asyncio
from thread_save.config import load_config
from thread_save.storage.path_resolver import ensure_vault_structure
from thread_save.storage.writer import WriteEngine

async def main():
    config = load_config()
    ensure_vault_structure(config)
    engine = WriteEngine(config)

    r1 = await engine.save_turn(user_query="Hello", title_hint="Repeated")
    tid = r1["thread_id"]

    r2 = await engine.save_turn(user_query="continue", prev_response="resp 1", prev_user_anchor="Hello", thread_id=tid)
    r3 = await engine.save_turn(user_query="continue", prev_response="resp 2", prev_user_anchor="continue", thread_id=tid)
    r4 = await engine.save_turn(user_query="continue", prev_response="resp 3", prev_user_anchor="continue", thread_id=tid)
    r4_retry = await engine.save_turn(user_query="continue", prev_response="resp 3", prev_user_anchor="continue", thread_id=tid)
    
    stats = engine.get_thread_stats(tid)
    print("n2", r2)
    print("n3", r3)
    print("n4", r4)
    print("r4_retry", r4_retry)
    print("STATS", stats)

asyncio.run(main())
