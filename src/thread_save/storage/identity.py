"""Thread identity — binding, anchor search, turn numbering (§4).

Rebuilt for reliability layer:
- Anchor-based binding replaces active.json fallback (§4.1)
- active.json retained as read-only list of open threads (not a fallback)
- Normalise function: NFC, collapse whitespace, strip, lowercase, first 80 chars
- Split-never-merge policy (§I-3): ambiguous → new thread

The active.json cross-chat bleed bug (§1, I-3) is eliminated by
never using "most recent thread for this client" as a binding key.
"""

from __future__ import annotations

import base64
import ulid
import json
import os
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from thread_save.config import VaultConfig
from thread_save.models import ActiveEntry


def generate_thread_id() -> str:
    """Generate a ULID string."""
    return str(ulid.new())


def generate_thread_id_short(thread_id: str) -> str:
    """Extract a 6-char short ID from ULID random tail."""
    return thread_id[-6:].lower()


def normalise_anchor(text: str, max_chars: int = 80) -> str:
    """Normalise text for anchor matching (§4.1).

    NFC → collapse whitespace → strip → lowercase → first 80 chars.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\x00", "")
    # Collapse all whitespace (including newlines) to single space
    text = " ".join(text.split())
    text = text.strip().lower()
    return text[:max_chars]


# How loosely a model-copied message opening may match the stored one (E8-3)
ANCHOR_PREFIX_MIN = 16
ANCHOR_SIMILARITY = 0.8


def anchors_agree(sent: str, stored: str) -> bool:
    """Does a model-sent message opening name the stored user message?

    The model copies "the first 80 characters" of a message it read earlier, so
    small differences (cut short, a typo, changed punctuation) still count.
    """
    a, b = normalise_anchor(sent), normalise_anchor(stored)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter >= ANCHOR_PREFIX_MIN and (a.startswith(b) or b.startswith(a)):
        return True
    return SequenceMatcher(None, a, b).ratio() >= ANCHOR_SIMILARITY


# ── Active Threads Registry (read-only list, NOT a binding fallback) ──────

def read_active_threads(config: VaultConfig) -> dict[str, ActiveEntry]:
    """Read the active threads list.

    Used by anchor search to narrow the set of candidates.
    NOT used as a fallback binding mechanism.
    """
    path = config.active_json_path
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    now = datetime.now(timezone.utc)
    result: dict[str, ActiveEntry] = {}

    for key, entry_data in data.items():
        try:
            entry = ActiveEntry(**entry_data)
            last = entry.last_write
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (now - last).total_seconds()
            if elapsed < entry.ttl_seconds:
                result[key] = entry
        except Exception:
            continue

    return result


def write_active_threads(
    config: VaultConfig,
    entries: dict[str, ActiveEntry],
) -> None:
    """Write active threads list atomically."""
    path = config.active_json_path
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    for key, entry in entries.items():
        data[key] = {
            "thread_id": entry.thread_id,
            "last_write": entry.last_write.isoformat(),
            "ttl_seconds": entry.ttl_seconds,
            "last_user_anchor": entry.last_user_anchor,
        }

    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def update_active_thread(
    config: VaultConfig,
    client_key: str,
    thread_id: str,
    user_anchor: str = "",
) -> None:
    """Update the active thread entry for a client."""
    entries = read_active_threads(config)
    entries[client_key] = ActiveEntry(
        thread_id=thread_id,
        last_write=datetime.now(timezone.utc),
        ttl_seconds=config.active_ttl_seconds,
        last_user_anchor=user_anchor,
    )
    write_active_threads(config, entries)


def search_active_by_anchor(
    config: VaultConfig,
    normalised_anchor: str,
) -> Optional[str]:
    """Search open threads for one whose last_user_anchor matches.

    §4.1 binding algorithm:
    - Exactly one match → return thread_id
    - Zero or >1 matches → return None (caller creates new thread, I-3)
    """
    entries = read_active_threads(config)

    matches = [
        entry.thread_id
        for entry in entries.values()
        if entry.last_user_anchor == normalised_anchor
    ]

    if len(matches) == 1:
        return matches[0]
    return None  # Split, never merge
