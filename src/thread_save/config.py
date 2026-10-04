"""Configuration loader for thread-save MCP server.

Reads from environment variables with sensible defaults.
All paths are resolved to absolute at load time.

Reliability-layer additions (§6.3, §6.4, §I-6, §I-8):
  THREADVAULT_MODE   — turn_start | both
  THREADVAULT_NUDGE  — off | on
  vault/.paused      — global kill switch
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class VaultMode(str, Enum):
    """§6.3 — Controls when the model is expected to call vault_save_turn."""
    TURN_START = "turn_start"   # Once at the start of each reply (default)
    BOTH = "both"               # Start + wrap-up (safe due to upsert)


class NudgeMode(str, Enum):
    """§6.4 — Whether results include a log_next_turn hint."""
    OFF = "off"
    ON = "on"


@dataclass(frozen=True)
class VaultConfig:
    """Immutable configuration for the thread vault."""

    # Root directory where all threads are stored
    vault_root: Path

    # Default account name (top-level partition)
    default_account: str = "default"

    # Page rollover thresholds (§4.2)
    page_max_bytes: int = 400_000  # 400 KB
    page_max_turns: int = 60

    # Identity TTL for active thread tracking
    active_ttl_seconds: int = 21_600  # 6 hours

    # Chunk buffer timeout (§4.3)
    chunk_timeout_seconds: int = 300  # 5 minutes

    # Slug constraints
    slug_max_chars: int = 40
    path_max_chars: int = 240

    # Redaction (§7.2)
    redaction_enabled: bool = True

    # Invocation reliability (§6.3, §6.4)
    mode: VaultMode = VaultMode.TURN_START
    nudge: NudgeMode = NudgeMode.OFF

    # Gap limits (§I-6)
    max_missing_reported: int = 10
    max_backfill_per_call: int = 10
    gap_lost_hours: int = 24

    # Events rotation (§7)
    events_max_bytes: int = 10_000_000  # 10 MB

    # ── Derived paths ──────────────────────────────────────────────────

    @property
    def index_dir(self) -> Path:
        return self.vault_root / "_index"

    @property
    def index_db_path(self) -> Path:
        return self.index_dir / "threads.sqlite"

    @property
    def active_json_path(self) -> Path:
        return self.index_dir / "active.json"

    @property
    def assets_dir(self) -> Path:
        return self.vault_root / "_assets"

    @property
    def events_path(self) -> Path:
        return self.index_dir / "events.jsonl"

    @property
    def pause_file(self) -> Path:
        """§I-8 — global kill switch."""
        return self.vault_root / ".paused"

    @property
    def is_paused(self) -> bool:
        """§I-8 — check if the vault is globally paused."""
        return self.pause_file.exists()


def load_config() -> VaultConfig:
    """Load configuration from environment variables.

    Environment variables:
        THREAD_SAVE_VAULT_ROOT      — Path to vault root (default: ~/thread_vault)
        THREAD_SAVE_ACCOUNT         — Default account name
        THREAD_SAVE_PAGE_MAX_BYTES  — Page size threshold in bytes
        THREAD_SAVE_PAGE_MAX_TURNS  — Max turns per page
        THREAD_SAVE_ACTIVE_TTL      — Active thread TTL in seconds
        THREAD_SAVE_REDACTION       — Enable/disable redaction
        THREADVAULT_MODE            — turn_start | both
        THREADVAULT_NUDGE           — off | on
    """
    vault_root_str = os.environ.get(
        "THREAD_SAVE_VAULT_ROOT",
        str(Path.home() / "thread_vault"),
    )
    vault_root = Path(vault_root_str).resolve()

    kwargs: dict = {"vault_root": vault_root}

    if account := os.environ.get("THREAD_SAVE_ACCOUNT"):
        kwargs["default_account"] = account

    if page_bytes := os.environ.get("THREAD_SAVE_PAGE_MAX_BYTES"):
        kwargs["page_max_bytes"] = int(page_bytes)

    if page_turns := os.environ.get("THREAD_SAVE_PAGE_MAX_TURNS"):
        kwargs["page_max_turns"] = int(page_turns)

    if ttl := os.environ.get("THREAD_SAVE_ACTIVE_TTL"):
        kwargs["active_ttl_seconds"] = int(ttl)

    if redaction := os.environ.get("THREAD_SAVE_REDACTION"):
        kwargs["redaction_enabled"] = redaction.lower() in ("true", "1", "yes")

    if mode_str := os.environ.get("THREADVAULT_MODE"):
        kwargs["mode"] = VaultMode(mode_str.lower())

    if nudge_str := os.environ.get("THREADVAULT_NUDGE"):
        kwargs["nudge"] = NudgeMode(nudge_str.lower())

    return VaultConfig(**kwargs)
