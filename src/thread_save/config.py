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
    slug_max_chars: int = 20
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

    # OAuth JWT Signing Key (Milestone H2) - loaded from secret config, never generated at startup
    jwt_secret: str = "threadvault-default-jwt-secret-key-change-in-prod-32bytes-min!"

    # Allowed hosts for Host header validation (Milestone H3)
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")

    # Viewer token TTL in seconds (Milestone H5, default 15m)
    viewer_ttl_seconds: int = 900

    # Envelope encryption (§2 S8, Milestone H6)
    envelope_encryption_enabled: bool = False
    master_key: Optional[str] = None

    # Public URL for OAuth issuer & metadata (§pre-deploy safety)
    public_url: str = "http://localhost:8000"

    # Enforce OAuth authentication (fail-closed default in load_config / server startup)
    enforce_auth: bool = False

    # Local offload policy (§1 G7, Milestone GH7)
    offload_after_days: int = 14

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
    def offloaded_json_path(self) -> Path:
        """§1 G7 — Pointer file for cold threads offloaded to GitHub."""
        return self.index_dir / "offloaded.json"

    @property
    def export_manifest_path(self) -> Path:
        """§1 G7 — Local export manifest tracking exported page hashes and commits."""
        return self.index_dir / "export_manifest.json"

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

    jwt_secret_val = os.environ.get("THREADVAULT_JWT_SECRET") or os.environ.get("JWT_SECRET_KEY")
    is_prod = bool(
        os.environ.get("ENVIRONMENT") == "production"
        or os.environ.get("RENDER")
        or os.environ.get("FLY_APP_NAME")
    )
    if not jwt_secret_val:
        if is_prod:
            raise ValueError(
                "Production deployment requires THREADVAULT_JWT_SECRET secret environment variable!"
            )
        jwt_secret_val = "threadvault-default-jwt-secret-key-change-in-prod-32bytes-min!"
    kwargs["jwt_secret"] = jwt_secret_val

    # Host allowlist (§H3)
    hosts = ["localhost", "127.0.0.1", "testserver"]
    if env_hosts := os.environ.get("THREADVAULT_ALLOWED_HOSTS"):
        for h in env_hosts.split(","):
            h = h.strip().lower()
            if h and h not in hosts:
                hosts.append(h)
    if fly_app := os.environ.get("FLY_APP_NAME"):
        fly_domain = f"{fly_app.strip().lower()}.fly.dev"
        if fly_domain not in hosts:
            hosts.append(fly_domain)
    if render_domain := os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        render_domain = render_domain.strip().lower()
        if render_domain not in hosts:
            hosts.append(render_domain)
    kwargs["allowed_hosts"] = tuple(hosts)

    if viewer_ttl_str := os.environ.get("THREADVAULT_VIEWER_TTL_SECONDS"):
        kwargs["viewer_ttl_seconds"] = int(viewer_ttl_str)

    if env_enc := os.environ.get("THREADVAULT_ENVELOPE_ENCRYPTION"):
        kwargs["envelope_encryption_enabled"] = env_enc.lower() in ("true", "1", "yes")

    if master_k := os.environ.get("THREADVAULT_MASTER_KEY"):
        kwargs["master_key"] = master_k
        kwargs["envelope_encryption_enabled"] = True

    if pub_url := os.environ.get("THREADVAULT_PUBLIC_URL"):
        kwargs["public_url"] = pub_url.strip().rstrip("/")

    auth_val = os.environ.get("THREADVAULT_ENFORCE_AUTH", "true").strip().lower()
    kwargs["enforce_auth"] = auth_val in ("true", "1", "yes")

    if offload_days_str := os.environ.get("THREADVAULT_OFFLOAD_AFTER_DAYS"):
        kwargs["offload_after_days"] = int(offload_days_str)

    return VaultConfig(**kwargs)
