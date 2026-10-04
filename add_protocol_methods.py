import os
import re

def main():
    path = "src/thread_save/storage/writer.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Add protocol implementation methods
    protocol_methods = """
    # ---------------------------------------------------------
    # Store Protocol Implementation (X1)
    # ---------------------------------------------------------
    
    async def bind_thread(self, account_id: str, thread_id: str | None, anchor: str) -> dict:
        from thread_save.storage.protocol import BindResult
        res = self._bind_thread(thread_id, anchor)
        return BindResult(thread_id=res[0], method=res[1])

    async def upsert_turn(
        self, account_id: str, thread_id: str, n: int, role: str, 
        body: str, fidelity: int, recovered: bool, chars: int, hash_: str
    ) -> dict:
        from thread_save.storage.protocol import UpsertResult
        from thread_save.models import Fidelity
        # Fidelity enum mapping
        fid_enum = Fidelity(fidelity) if isinstance(fidelity, int) else fidelity
        action = await self._write_slot(
            thread_id=thread_id,
            n=n,
            role=role,
            body=body,
            fidelity=fid_enum,
            recovered=recovered,
        )
        return UpsertResult(action=action.value, n=n)

    async def next_turn_number(self, account_id: str, thread_id: str) -> int:
        n, gap_ns = self._assign_n(thread_id, None, None, None)
        return n

    async def record_gap(self, account_id: str, thread_id: str, n: int) -> None:
        self._gaps.register(thread_id, n, "open")
        
    async def find(self, account_id: str, query: str, limit: int) -> list:
        # vault_find is not implemented in FileStore natively yet, just return empty or use _registry
        return []

    async def stats(self, account_id: str, thread_id: str) -> dict:
        from thread_save.storage.protocol import ThreadStats
        s = self.get_thread_stats(thread_id)
        if not s:
            return ThreadStats(total_turns=0, verbatim=0, abridged=0, stubs=0, gaps_open=[], gaps_recovered=[])
        return ThreadStats(
            total_turns=s.get("total_turns", 0),
            verbatim=s.get("fidelity_counts", {}).get("verbatim", 0),
            abridged=s.get("fidelity_counts", {}).get("abridged", 0),
            stubs=s.get("fidelity_counts", {}).get("stub", 0),
            gaps_open=s.get("gaps_open", []),
            gaps_recovered=s.get("gaps_recovered", [])
        )
"""
    
    # Insert methods into FileStore
    content = content.replace("    # \"?\"? Core Tools (A 5) \"?\"?\"?\"?\"?\"?\"?\"?", protocol_methods + "\n    # \"?\"? Core Tools (A 5) \"?\"?\"?\"?\"?\"?\"?\"?")
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    main()
