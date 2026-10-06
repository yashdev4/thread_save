from __future__ import annotations

from typing import AsyncContextManager, Optional, Protocol
from pydantic import BaseModel

from thread_save.models import Fidelity, ThreadMeta
from thread_save.security.idempotency import SlotIndex


class Stub(BaseModel):
    n: int
    anchor: str = ""


class BindResult(BaseModel):
    thread_id: Optional[str]
    method: str


class UpsertResult(BaseModel):
    action: str
    n: int


class ThreadStats(BaseModel):
    total_turns: int
    verbatim: int
    abridged: int
    reported: int = 0
    truncated: int = 0
    stubs: int
    gaps_open: list[int]
    gaps_recovered: list[int]
    open_turn: Optional[int] = None
    gaps_open_count: int = 0
    gaps_lost_count: int = 0


class ThreadHit(BaseModel):
    thread_id: str
    title: str
    snippet: str
    updated_at: str


class ThreadTxn(Protocol):
    """Everything for one save happens inside one of these (§4.1)."""

    async def load_slots(self) -> SlotIndex:
        ...

    async def upsert_turn(
        self,
        n: int,
        role: str,
        body: str,
        fidelity: Fidelity,
        recovered: bool = False,
        turn_key: str | None = None,
        page: int | None = None,
        model: str = "",
        anchor: str = "",
    ) -> UpsertResult:
        ...

    async def write_stubs(self, stubs: list[Stub]) -> None:
        ...

    async def record_gaps(self, gaps: list[int]) -> None:
        ...

    async def update_thread_meta(self, **fields) -> None:
        ...

    async def enqueue_export(self) -> None:
        ...


class Store(Protocol):
    """Persistence backend interface (FileStore | PgStore)."""

    async def bind_thread(
        self,
        account_id: str,
        thread_id: str | None,
        anchor: str | None,
    ) -> BindResult:
        ...

    async def create_thread(
        self,
        account_id: str,
        title_hint: str | None,
        tags: list[str] | None = None,
        client: str = "claude-desktop",
        continues: str | None = None,
    ) -> ThreadMeta:
        ...

    def thread_txn(
        self,
        account_id: str,
        thread_id: str,
    ) -> AsyncContextManager[ThreadTxn]:
        ...

    async def is_tombstoned(self, account_id: str, thread_id: str) -> bool:
        ...

    async def find(
        self,
        account_id: str,
        query: str | None,
        limit: int,
        titles_only: bool = False,
    ) -> list[ThreadHit]:
        ...

    async def stats(
        self,
        account_id: str,
        thread_id: str | None,
    ) -> Optional[ThreadStats]:
        ...
