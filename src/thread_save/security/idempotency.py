"""Slot-based idempotent upsert engine (§3).

Replaces the ring buffer from v1. The slot index tracks
(n, role) → (hash, fidelity, length) per thread, enabling:
- Idempotent upsert: same hash → no-op
- Fidelity-ranked replacement: verbatim beats abridged
- Length-based tiebreaking: longer body wins at same rank

The slot dict is rebuilt from the page on first load.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import Enum
from typing import Optional

from thread_save.models import Fidelity, SlotEntry, SlotKey


class WriteAction(Enum):
    """What the writer should do with an incoming turn."""
    WRITE_NEW = "write_new"     # Slot is empty → insert
    REPLACE = "replace"         # Incoming is better → overwrite
    NO_OP = "no_op"             # Existing is equal or better → skip


import unicodedata


def canonical_v1(text: str) -> str:
    """Normalise text per spec canonical_v1 (W-5, §3.4).

    Rules:
    - Unicode NFC normalization
    - CRLF and CR -> LF
    - Strip trailing newlines then add exactly one '\n'
    - DO NOT strip leading whitespace (preserves indentation)
    """
    if not text:
        return ""
    norm = unicodedata.normalize("NFC", text)
    norm = norm.replace("\x00", "")
    norm = norm.replace("\r\n", "\n").replace("\r", "\n")
    norm = norm.rstrip("\n") + "\n"
    return norm


def compute_content_hash(text: str) -> str:
    """SHA-256 hash of canonical_v1 normalised content for dedup and integrity (hash_v1)."""
    canon = canonical_v1(text)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def compute_content_hash_short(text: str) -> str:
    """8-char hash for display in HTML comments."""
    return compute_content_hash(text)[:8]


class SlotIndex:
    """Per-thread index of known turn slots.

    Thread-safe within a single event loop (access is serialised by
    the per-file asyncio.Lock in the writer).

    Usage:
        index = SlotIndex()
        action = index.evaluate(key, hash, fidelity, length)
        if action != WriteAction.NO_OP:
            # perform write
            index.record(key, hash, fidelity, length)
    """

    def __init__(self) -> None:
        self._threads: dict[str, dict[SlotKey, SlotEntry]] = {}

    def _get_slots(self, thread_id: str) -> dict[SlotKey, SlotEntry]:
        if thread_id not in self._threads:
            self._threads[thread_id] = {}
        return self._threads[thread_id]

    def evaluate(
        self,
        thread_id: str,
        key: SlotKey,
        content_hash: str,
        fidelity: Fidelity,
        body_length: int,
    ) -> WriteAction:
        """Determine what to do with an incoming turn (§3.3).

        Upsert rule:
        - empty slot → WRITE_NEW
        - same content hash → NO_OP
        - incoming has higher fidelity rank → REPLACE
        - same rank, incoming is longer → REPLACE (abridgement repaired)
        - otherwise → NO_OP (keep existing)
        """
        slots = self._get_slots(thread_id)
        existing = slots.get(key)

        if existing is None:
            return WriteAction.WRITE_NEW

        if existing.content_hash == content_hash:
            return WriteAction.NO_OP

        if fidelity.rank > existing.fidelity.rank:
            return WriteAction.REPLACE

        if fidelity.rank == existing.fidelity.rank and body_length > existing.body_length:
            return WriteAction.REPLACE

        return WriteAction.NO_OP

    def record(
        self,
        thread_id: str,
        key: SlotKey,
        content_hash: str,
        fidelity: Fidelity,
        body_length: int,
        turn_key: Optional[str] = None,
    ) -> None:
        """Record a slot after successful write."""
        slots = self._get_slots(thread_id)
        slots[key] = SlotEntry(
            content_hash=content_hash,
            fidelity=fidelity,
            body_length=body_length,
            turn_key=turn_key,
        )

    def get(
        self,
        thread_id: str,
        key: SlotKey,
    ) -> Optional[SlotEntry]:
        """Get the current slot entry, or None."""
        return self._get_slots(thread_id).get(key)

    def highest_n(self, thread_id: str) -> int:
        """Return the highest n assigned in this thread, or 0."""
        slots = self._get_slots(thread_id)
        if not slots:
            return 0
        return max(k.n for k in slots)

    def has_user_turn(self, thread_id: str, n: int) -> bool:
        """Check if a user turn exists at position n."""
        return SlotKey(n, "user") in self._get_slots(thread_id)

    def find_turn_key(self, thread_id: str, turn_key: str) -> Optional[int]:
        """Find the user turn number n that holds the given turn_key."""
        slots = self._get_slots(thread_id)
        for k, v in slots.items():
            if k.role == "user" and v.turn_key == turn_key:
                return k.n
        return None

    def has_assistant_turn(self, thread_id: str, n: int) -> bool:
        """Check if an assistant turn exists at position n."""
        return SlotKey(n, "assistant") in self._get_slots(thread_id)

    def find_user_turn_by_anchor(
        self,
        thread_id: str,
        normalised_anchor: str,
        anchor_map: dict[int, str],
    ) -> Optional[int]:
        """Find a user turn n whose normalised anchor matches.

        Args:
            thread_id: Thread to search.
            normalised_anchor: The anchor to match.
            anchor_map: {n: normalised_anchor} for known user turns.

        Returns:
            The n if exactly one match, else None.
        """
        matches = [n for n, a in anchor_map.items() if a == normalised_anchor]
        if len(matches) == 1:
            return matches[0]
        return None  # Zero or ambiguous → caller must split (I-3)

    def all_slot_keys(self, thread_id: str) -> list[SlotKey]:
        """List all known slot keys for a thread."""
        return list(self._get_slots(thread_id).keys())

    def get_recent_turn_keys(self, thread_id: str, limit: int = 10) -> list[str]:
        """Return the most recent user turn_keys for a thread (§1 G7)."""
        slots = self._get_slots(thread_id)
        user_items = [
            (k.n, v.turn_key)
            for k, v in slots.items()
            if k.role == "user" and v.turn_key
        ]
        # Sort descending by turn number n
        user_items.sort(key=lambda item: item[0], reverse=True)
        return [tk for _, tk in user_items[:limit]]
