from datetime import datetime, timezone, timedelta
from typing import Optional
from thread_save.storage.protocol import Store

async def reap_orphans(store: Store, account_id: str) -> int:
    """Reap orphaned threads (A 3.1).
    
    A thread is an orphan if:
    - It has exactly 1 turn
    - It is older than 24 hours
    - There exists another thread created within +/- 2 minutes with the exact same turn 1,
      and that twin thread has > 1 turn.
    """
    reaped = 0
    now = datetime.now(timezone.utc)
    
    threads = store.get_all_threads(account_id)
    # Filter candidates: exactly 1 turn, >24h old
    candidates = []
    others = []
    
    for t in threads:
        created = t.get("created_at")
        if not created: continue
        # created might be a string or datetime
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created).astimezone(timezone.utc)
            except Exception:
                continue
                
        age = now - created
        turn_count = t.get("turn_count", 0)
        
        # Single-turn thread has turn_count <= 2 (user 1 + optional open placeholder)
        if turn_count <= 2 and age > timedelta(hours=24):
            candidates.append((t, created))
        else:
            others.append((t, created))
            
    for cand_t, cand_created in candidates:
        cand_anchor = cand_t.get("first_user_anchor") or cand_t.get("last_user_anchor")
        if not cand_anchor:
            continue
        
        # Check for twin that continued
        is_orphan = False
        for oth_t, oth_created in others:
            if oth_t.get("turn_count", 0) <= 2:
                continue
                
            time_diff = abs((cand_created - oth_created).total_seconds())
            oth_anchor = oth_t.get("first_user_anchor") or oth_t.get("last_user_anchor")
            if time_diff <= 120 and oth_anchor == cand_anchor:
                is_orphan = True
                break
                
        if is_orphan:
            store.delete_thread(account_id, cand_t["thread_id"])
            reaped += 1
            
    return reaped
