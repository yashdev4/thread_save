"""Data models for thread-save MCP server.

Rebuilt for the invocation reliability layer:
- Fidelity enum with numeric ranks for upsert comparison (§3.2)
- SlotKey for addressing specific (n, role) slots (§3.1)
- BackfillTurn for gap repair (§2.3)
- ThreadMeta with open_turn and paused tracking (§4.3, §6.5)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, NamedTuple, Optional

from pydantic import BaseModel, Field


# ── Fidelity with ranked ordering (§3.2) ──────────────────────────────────

class Fidelity(str, Enum):
    """How faithfully a turn body was captured. Higher rank wins on upsert."""
    OPEN = "open"           # rank 0 — placeholder for the final unclosed reply
    STUB = "stub"           # rank 1 — missed turn, body is a marker
    TRUNCATED = "truncated" # rank 2 — chunk assembly timed out or server-side truncation (§3.2, §3.4)
    ABRIDGED = "abridged"   # rank 3 — model self-declared shortening, or a summary the server detected
    REPORTED = "reported"   # rank 4 — reply text as sent back by the model; cannot be checked as verbatim (B7 E-truth)
    VERBATIM = "verbatim"   # rank 5 — full content

    @property
    def rank(self) -> int:
        return _FIDELITY_RANKS[self]


_FIDELITY_RANKS: dict[Fidelity, int] = {
    Fidelity.OPEN: 0,
    Fidelity.STUB: 1,
    Fidelity.TRUNCATED: 2,
    Fidelity.ABRIDGED: 3,
    Fidelity.REPORTED: 4,
    Fidelity.VERBATIM: 5,
}

# §3.4 Server-side size limit per body: 100,000 characters
MAX_BODY_CHARS: int = 100_000


# ── Slot Key (§3.1) ──────────────────────────────────────────────────────

class SlotKey(NamedTuple):
    """Unique key for a turn slot within a thread."""
    n: int          # Server-assigned turn number (1-indexed)
    role: str       # "user" or "assistant"


# ── Thread Metadata (YAML front matter) ───────────────────────────────────

class ThreadMeta(BaseModel):
    """Represents the YAML front matter of a thread file."""
    schema_version: int = 1
    thread_id: str
    title: str
    slug: str
    account: str
    client: str = "claude-desktop"
    model: str = ""
    created: datetime
    updated: datetime
    page: int = 1
    prev: Optional[str] = None
    next: Optional[str] = None
    turn_count: int = 0
    turn_range: list[int] = Field(default_factory=lambda: [1, 0])
    bytes: int = 0
    gaps: list[int] = Field(default_factory=list)
    redacted: bool = False
    tags: list[str] = Field(default_factory=list)
    open_turn: Optional[int] = None   # §4.3 — n of the unclosed last reply
    paused: bool = False
    nonce: str = ""              # §6.5 — per-conversation opt-out
    continues: Optional[str] = None  # §1 G7 — continuation thread fallback if rehydration fails


# ── Turn Data ──────────────────────────────────────────────────────────────

class Attachment(BaseModel):
    """Marker for uncapturable content (artifacts, images, etc.)."""
    type: str
    title: str


class TurnData(BaseModel):
    """A single turn slot (one role within a conversation turn)."""
    turn_index: int         # n
    role: str               # "user" or "assistant"
    body: str               # The content
    timestamp: datetime
    model: str = ""
    fidelity: Fidelity = Fidelity.VERBATIM
    char_count: int = 0
    content_hash: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    recovered: bool = False  # True if backfilled after a gap
    anchor: str = ""         # First 80 chars, normalised (user turns only)
    turn_key: Optional[str] = None # triple hash key (user turns only)


# ── Page State (in-memory cache per thread) ───────────────────────────────

class PageState(BaseModel):
    """In-memory cache of current page state for a thread."""
    thread_id: str
    current_page: int = 1
    current_bytes: int = 0
    current_turn_count: int = 0
    global_turn_index: int = 0  # Highest n assigned so far
    file_path: str = ""


# ── Tool Input Models (§2) ────────────────────────────────────────────────

class BackfillTurn(BaseModel):
    """A single turn in a backfill request (§2.3)."""
    n: int = Field(description="The turn number to backfill.")
    user_query: Optional[str] = Field(
        default=None,
        description="The user's message for this turn.",
    )
    assistant_response: Optional[str] = Field(
        default=None,
        description="The assistant's response for this turn.",
    )
    fidelity: Literal["verbatim", "abridged"] = Field(
        default="verbatim",
        description="Fidelity of the provided content.",
    )


# ── Slot Index Entry ──────────────────────────────────────────────────────

class SlotEntry(NamedTuple):
    """Cached slot state for upsert comparison (§3.3)."""
    content_hash: str
    fidelity: Fidelity
    body_length: int
    turn_key: Optional[str] = None


# ── Active Thread Entry ───────────────────────────────────────────────────

class ActiveEntry(BaseModel):
    """Tracks an open thread for anchor search (read-only list, not fallback)."""
    thread_id: str
    last_write: datetime
    ttl_seconds: int = 21_600
    last_user_anchor: str = ""  # Normalised anchor of the latest user turn
