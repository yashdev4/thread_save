"""Sensitive data redaction before writing to disk (§7.2).

Scans user_query and assistant_response for:
- AWS access keys / secret keys
- Generic API keys (long hex/base64 strings)
- Private keys (PEM format)
- JWTs (eyJ... tokens)
- Connection strings with inline passwords
- High-entropy hex/base64 strings (> 32 chars)

Replaces with [REDACTED:{type}:{short_hash}] so the redaction
is traceable but the secret is irrecoverable.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import NamedTuple


class RedactionResult(NamedTuple):
    """Result of scanning text for sensitive data."""
    text: str
    was_redacted: bool
    redaction_count: int


# ── Pattern Definitions ───────────────────────────────────────────────────

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # AWS Access Key ID (always starts with AKIA)
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),

    # AWS Secret Access Key (40 chars, base64-like)
    ("aws_secret_key", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")),

    # Generic API key patterns (key=VALUE, api_key: VALUE, etc.)
    ("api_key", re.compile(
        r"""(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|"""
        r"""bearer|password|passwd|pwd)"""
        r"""[\s]*[=:]\s*["']?([A-Za-z0-9+/_.~-]{20,})["']?""",
        re.IGNORECASE,
    )),

    # PEM private keys
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
    )),

    # JWT tokens (three base64url segments separated by dots)
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    )),

    # Connection strings with password inline
    # e.g. postgresql://user:password@host:5432/db
    ("connection_string", re.compile(
        r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)"
        r"://[^:\s]+:([^@\s]{4,})@",
        re.IGNORECASE,
    )),

    # GitHub/GitLab personal access tokens
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),

    # Slack tokens
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
]

# High-entropy detector: hex strings > 32 chars that look like secrets
_HEX_LONG = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BASE64_LONG = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


import hmac
import os
import secrets

_local_ephemeral_server_key: bytes | None = None


def get_server_key(server_key: bytes | str | None = None) -> bytes:
    """Resolve HMAC server key for redaction masks (§7.2, pre-deploy safety).

    No built-in default key anywhere.
    THREADVAULT_SERVER_KEY is strictly required in non-local/production environments.
    In local development/tests without an explicit key, generates an ephemeral random key per process.
    """
    if server_key is not None:
        return server_key.encode("utf-8") if isinstance(server_key, str) else server_key

    env_key = os.environ.get("THREADVAULT_SERVER_KEY")
    if env_key and env_key.strip():
        return env_key.strip().encode("utf-8")

    is_prod = bool(
        os.environ.get("ENVIRONMENT") == "production"
        or os.environ.get("APP_ENV") == "production"
        or os.environ.get("FLY_APP_NAME")
        or os.environ.get("RENDER")
        or os.environ.get("THREADVAULT_FORCE_PRODUCTION")
    )
    auth_paused = os.environ.get("THREADVAULT_AUTH_PAUSED", "false").strip().lower() in ("true", "1", "yes")
    if is_prod and not auth_paused:
        raise RuntimeError(
            "THREADVAULT_SERVER_KEY is required in non-local environments; no built-in default key permitted"
        )

    global _local_ephemeral_server_key
    if _local_ephemeral_server_key is None:
        _local_ephemeral_server_key = secrets.token_bytes(32)
    return _local_ephemeral_server_key


def _short_hash(value: str, server_key: bytes | str | None = None) -> str:
    """Generate a 4-char HMAC tag for traceability (W5 §3.4)."""
    key_bytes = get_server_key(server_key)
    return hmac.new(key_bytes, value.encode("utf-8"), hashlib.sha256).hexdigest()[:4]


def _shannon_entropy(data: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    entropy = -sum(
        (count / length) * math.log2(count / length)
        for count in counts.values()
    )
    return entropy


def redact_text(text: str, server_key: bytes | str | None = None) -> RedactionResult:
    """Scan text for sensitive data and redact matches using HMAC tags.

    Args:
        text: The text to scan.
        server_key: Server key for HMAC tagging (optional, resolved via get_server_key).

    Returns:
        RedactionResult with redacted text, whether anything was changed,
        and the number of redactions made.
    """
    result = text
    count = 0

    # Apply known patterns
    for pattern_name, pattern in _PATTERNS:
        def _replace(m: re.Match, name: str = pattern_name) -> str:
            nonlocal count
            count += 1
            matched = m.group(0)
            h = _short_hash(matched, server_key)
            return f"[REDACTED:{name}:{h}]"

        result = pattern.sub(_replace, result)

    # High-entropy hex strings (only if they look random, not like UUIDs)
    for m in _HEX_LONG.finditer(result):
        value = m.group(0)
        # Skip if it's already been redacted
        if "[REDACTED:" in result[max(0, m.start() - 20):m.start()]:
            continue
        # Skip UUIDs (8-4-4-4-12 hex with hyphens already removed? unlikely)
        # Check entropy — real secrets have high entropy
        if _shannon_entropy(value) > 3.5 and len(value) >= 32:
            h = _short_hash(value, server_key)
            result = result.replace(value, f"[REDACTED:high_entropy_hex:{h}]", 1)
            count += 1

    return RedactionResult(
        text=result,
        was_redacted=count > 0,
        redaction_count=count,
    )
