"""Data models for thread-save MCP server.

Rebuilt for the invocation reliability layer:
- Fidelity enum with numeric ranks for upsert comparison (§3.2)
- SlotKey for addressing specific (n, role) slots (§3.1)
- SaveTurnInput with lagged-logging fields (§2.1)
- BackfillTurn/BackfillInput for gap repair (§2.3)
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
    ABRIDGED = "abridged"   # rank 3 — model self-declared shortening
    VERBATIM = "verbatim"   # rank 4 — full content

    @property
    def rank(self) -> int:
        return _FIDELITY_RANKS[self]


_FIDELITY_RANKS: dict[Fidelity, int] = {
    Fidelity.OPEN: 0,
    Fidelity.STUB: 1,
    Fidelity.TRUNCATED: 2,
    Fidelity.ABRIDGED: 3,
    Fidelity.VERBATIM: 4,
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

class SaveTurnInput(BaseModel):
    """Input schema for vault_save_turn (§2.1).

    Lagged logging: user_query is the new message, prev_response is the
    model's previous reply (complete and stable by the time this is called).
    """
    user_query: str = Field(
        description="The user's latest message, verbatim.",
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Thread ID from the previous result. Omit on the first turn.",
    )
    prev_response: Optional[str] = Field(
        default=None,
        description="Your previous reply in this conversation, verbatim. Omit on the first turn.",
    )
    prev_user_anchor: Optional[str] = Field(
        default=None,
        description="First 80 characters of the user's previous message. Omit on the first turn.",
    )
    client_turn_number: Optional[int] = Field(
        default=None,
        description="Your count of turns in this conversation. A hint for gap detection.",
    )
    title_hint: Optional[str] = Field(
        default=None,
        description="A short descriptive title. First turn only.",
    )
    fidelity: Literal["verbatim", "abridged"] = Field(
        default="verbatim",
        description="Self-declared fidelity for prev_response.",
    )
    chunk_index: Optional[int] = Field(
        default=None,
        description="For prev_response > ~6000 chars: 0-indexed chunk number.",
    )
    is_final: bool = Field(
        default=True,
        description="Whether this is the final chunk of prev_response.",
    )
    model_hint: Optional[str] = Field(
        default=None,
        description="Optional model identifier for telemetry.",
    )
    current_response: Optional[str] = Field(
        default=None,
        description="For 'both' mode: your current reply to be saved now.",
    )


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


class BackfillInput(BaseModel):
    """Input schema for vault_backfill (§2.3)."""
    thread_id: str = Field(description="Thread ID to backfill.")
    turns: list[BackfillTurn] = Field(
        description="Turns to add. Maximum 10 per call.",
    )


class FindInput(BaseModel):
    """Input schema for vault_find."""
    query: Optional[str] = Field(
        default=None,
        description="Keyword, date, or thread_id fragment to search for.",
    )
    limit: int = Field(
        default=10,
        description="Maximum number of results to return.",
    )


class StatsInput(BaseModel):
    """Input schema for vault_stats."""
    thread_id: Optional[str] = Field(
        default=None,
        description="Thread ID. Omit for the most recently active thread.",
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
