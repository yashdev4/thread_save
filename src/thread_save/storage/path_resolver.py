"""Path resolution and directory management (§1.1, §1.2).

Resolves vault paths using the nested layout:
    vault/{account}/{YYYY}/{MM}/{filename}_p{NN}.md

Creates directories on demand. All paths are validated against
the vault root jail before use.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from thread_save.config import VaultConfig
from thread_save.security.sanitizer import (
    assert_within_vault,
    sanitize_slug,
    validate_full_path_length,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9-]{1,20}$")


def validate_slug(slug: str) -> None:
    """Validate that a stored topic slug conforms to ^[a-z0-9-]{1,20}$."""
    if not SLUG_PATTERN.match(slug):
        raise ValueError(f"Invalid slug '{slug}': must match ^[a-z0-9-]{{1,20}}$")


def resolve_thread_dir(
    config: VaultConfig,
    account: str,
    dt: datetime,
) -> Path:
    """Resolve the directory for a thread file.

    Layout: vault_root / {account} / {YYYY} / {MM} /

    Args:
        config: Vault configuration.
        account: Account name (slug already sanitised per W2).
        dt: Timestamp for year/month partitioning.

    Returns:
        Resolved, validated directory path.
    """
    year = f"{dt.year:04d}"
    month = f"{dt.month:02d}"

    dir_path = config.vault_root / account / year / month
    assert_within_vault(dir_path, config.vault_root)
    return dir_path


def build_filename(
    dt: datetime,
    account: str,
    thread_id_short: str,
    slug: str,
    page: int,
) -> str:
    """Build a thread filename per §1.2 naming convention.

    Format: {YYYY-MM-DD}T{HHmm}_{account}_{thread_id_short}_{slug}_p{NN}.md

    Args:
        dt: Thread creation timestamp.
        account: Sanitized account name.
        thread_id_short: 6-char base32 ID.
        slug: Sanitized kebab-case topic slug (must match ^[a-z0-9-]{1,20}$).
        page: Page number (1-indexed).

    Returns:
        The filename string (no directory).
    """
    validate_slug(slug)
    date_part = dt.strftime("%Y-%m-%dT%H%M")
    page_str = f"p{page:02d}"
    return f"{date_part}_{account}_{thread_id_short}_{slug}_{page_str}.md"


def resolve_thread_path(
    config: VaultConfig,
    account: str,
    dt: datetime,
    thread_id_short: str,
    slug: str,
    page: int,
) -> Path:
    """Resolve the full path for a thread file.

    Combines directory resolution with filename building.
    Validates the full path length.

    Returns:
        Full absolute path to the thread file.
    """
    dir_path = resolve_thread_dir(config, account, dt)
    filename = build_filename(dt, account, thread_id_short, slug, page)
    full_path = dir_path / filename

    validate_full_path_length(full_path, config.path_max_chars)
    assert_within_vault(full_path, config.vault_root)

    return full_path


def ensure_directory(path: Path) -> None:
    """Create a directory and all parents if they don't exist.

    Args:
        path: Directory path to create.
    """
    path.mkdir(parents=True, exist_ok=True)


def ensure_vault_structure(config: VaultConfig) -> None:
    """Create the vault root and essential subdirectories.

    Creates:
        - vault_root/
        - vault_root/_index/
        - vault_root/_assets/

    Also writes a .gitignore with '*' to prevent accidental commits.
    """
    config.vault_root.mkdir(parents=True, exist_ok=True)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    config.assets_dir.mkdir(parents=True, exist_ok=True)

    gitignore = config.vault_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
