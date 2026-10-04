r"""Path sanitization per §7.1 of the implementation plan.

Handles:
- Directory traversal prevention (../, ..\)
- Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
- Control characters and NUL bytes
- Trailing dots and spaces (Windows filesystem quirk)
- Unicode NFC normalization
- Length capping
- Vault-root jail assertion
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Windows reserved device names (case-insensitive)
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

# Characters illegal in filenames on Windows/macOS/Linux
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# High-entropy detection isn't needed here — that's in redactor.py
# This module only sanitizes path components.


def sanitize_slug(raw: str, max_chars: int = 40) -> str:
    """Sanitize a user/model-supplied string into a safe filesystem slug.

    Args:
        raw: The raw input string (title, thread name, etc.)
        max_chars: Maximum slug length.

    Returns:
        A safe, lowercase, kebab-case slug. Returns "untitled" if empty
        after sanitization.
    """
    # Step 1: Unicode NFC normalization
    text = unicodedata.normalize("NFC", raw)

    # Step 2: Strip control characters and NUL
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)

    # Step 3: Remove directory traversal sequences
    text = text.replace("../", "").replace("..\\", "")
    text = text.replace("..", "")
    text = text.replace("~", "")

    # Step 4: Remove illegal filesystem characters
    text = _ILLEGAL_CHARS.sub("", text)

    # Step 5: Convert to kebab-case
    # Replace whitespace, underscores, and dots with hyphens
    text = re.sub(r"[\s_\.]+", "-", text)

    # Collapse multiple hyphens
    text = re.sub(r"-{2,}", "-", text)

    # Strip leading/trailing hyphens
    text = text.strip("-")

    # Step 6: Lowercase
    text = text.lower()

    # Step 6b: Strip any remaining non-slug-safe characters
    text = re.sub(r"[^a-z0-9-]", "", text)

    # Collapse hyphens again after stripping
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip("-")
    text = text.rstrip(". ")

    # Step 8: Check for Windows reserved names
    # Compare the stem (without extension) against reserved names
    stem = text.split(".")[0].upper() if "." in text else text.upper()
    if stem in _WINDOWS_RESERVED:
        text = f"_{text}"

    # Step 9: Cap length
    if len(text) > max_chars:
        text = text[:max_chars].rstrip("-")

    # Step 10: Fallback if empty
    if not text:
        text = "untitled"

    return text


def assert_within_vault(path: Path, vault_root: Path) -> None:
    """Assert that a resolved path is within the vault root.

    This is the final guard against path traversal attacks.

    Args:
        path: The path to validate (will be resolved).
        vault_root: The vault root (will be resolved).

    Raises:
        ValueError: If the path escapes the vault root.
    """
    resolved = path.resolve()
    root = vault_root.resolve()
    # Use os.path for reliable prefix comparison on Windows
    resolved_str = str(resolved)
    root_str = str(root)
    if not resolved_str.startswith(root_str):
        raise ValueError(
            f"Path escapes vault root: {resolved_str} is not under {root_str}"
        )


def validate_full_path_length(path: Path, max_chars: int = 240) -> None:
    """Assert that the full absolute path length is within limits.

    Windows MAX_PATH is 260; we cap at 240 for safety margin.

    Raises:
        ValueError: If path exceeds max_chars.
    """
    abs_str = str(path.resolve())
    if len(abs_str) > max_chars:
        raise ValueError(
            f"Full path length {len(abs_str)} exceeds limit {max_chars}: "
            f"{abs_str[:80]}..."
        )
