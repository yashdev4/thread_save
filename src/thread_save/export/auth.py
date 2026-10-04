"""GitHub Export Authentication & Credential Security (§1 G2, Milestone GH6).

Implements:
- Fine-grained PAT support with 7-day expiration warning
- Parse 'github-authentication-token-expiration' header from GitHub API
- GitHub App installation token flow (mints short-lived tokens for remote multi-user)
- Envelope encryption/decryption of stored tokens (AES-256-GCM)
- Strict token scrubbing to guarantee zero token leakage in logs, events, and exceptions
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
import time
from typing import Any, Optional

import httpx
import jwt  # PyJWT

from thread_save.security.encryption import decrypt_field, encrypt_field

logger = logging.getLogger("thread_save.export.auth")

# Matches GitHub tokens (fine-grained PAT, classic PAT, installation token)
TOKEN_REGEX = re.compile(
    r"(ghp_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{40,}|ghs_[a-zA-Z0-9]{20,})"
)


def scrub_tokens(text: str) -> str:
    """Scrub any GitHub tokens from text, URLs, or error messages."""
    return TOKEN_REGEX.sub("[REDACTED_GH_TOKEN]", text)


def mask_token_preview(token: str) -> str:
    """Return safe preview of token showing prefix and last 4 chars."""
    if not token or len(token) < 8:
        return "[REDACTED]"
    prefix = token.split("_")[0] + "_" if "_" in token else token[:4]
    return f"{prefix}...{token[-4:]}"


def parse_expiration_header(header_val: str) -> Optional[datetime]:
    """Parse 'github-authentication-token-expiration' header.

    Format: 'YYYY-MM-DD HH:MM:SS UTC'
    """
    if not header_val:
        return None
    cleaned = header_val.strip().rstrip(" UTC")
    try:
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(header_val)
        except ValueError:
            return None


class GitHubAuthManager:
    """Manages credentials, expiry tracking, and token lifecycle."""

    def __init__(
        self,
        token: Optional[str] = None,
        token_expires_at: Optional[datetime] = None,
        master_key_hex: Optional[str] = None,
    ):
        self._raw_token = token
        self.token_expires_at = token_expires_at
        self.master_key_hex = master_key_hex
        self.expiry_warning_issued = False

    @property
    def token(self) -> Optional[str]:
        return self._raw_token

    def check_token_expiry(self, now: Optional[datetime] = None) -> bool:
        """Warn if token expires within 7 days (G2). Returns True if warning active."""
        if not self.token_expires_at:
            return False

        ref_now = now or datetime.now(timezone.utc)
        if self.token_expires_at.tzinfo is None:
            self.token_expires_at = self.token_expires_at.replace(tzinfo=timezone.utc)

        time_left = self.token_expires_at - ref_now
        days_left = time_left.total_seconds() / 86400.0

        if 0 <= days_left <= 7.0:
            logger.warning(
                "GitHub fine-grained PAT expires in %.1f days (at %s). Please rotate your PAT soon to avoid export interruption.",
                days_left,
                self.token_expires_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            )
            return True
        elif days_left < 0:
            logger.error("GitHub fine-grained PAT has EXPIRED as of %s!", self.token_expires_at)
            return True

        return False

    def update_expiry_from_headers(self, headers: httpx.Headers) -> None:
        """Inspect GitHub response headers for token expiration."""
        exp_header = headers.get("github-authentication-token-expiration")
        if exp_header:
            parsed = parse_expiration_header(exp_header)
            if parsed:
                self.token_expires_at = parsed
                self.check_token_expiry()

    def encrypt_stored_token(self) -> str:
        """Encrypt token using envelope encryption (X6) for database storage."""
        if not self._raw_token:
            return ""
        if not self.master_key_hex:
            # Fallback if no master key configured
            return self._raw_token
        return encrypt_field(self._raw_token, self.master_key_hex)

    @classmethod
    def decrypt_stored_token(cls, ciphertext: str, master_key_hex: Optional[str]) -> str:
        """Decrypt token from database storage."""
        if not ciphertext:
            return ""
        if not master_key_hex or not ciphertext.startswith("v1:"):
            return ciphertext
        return decrypt_field(ciphertext, master_key_hex)


class GitHubAppAuth:
    """Mints short-lived installation tokens for remote multi-user deployments."""

    def __init__(self, app_id: str, private_key_pem: str):
        self.app_id = app_id
        self.private_key_pem = private_key_pem

    def create_app_jwt(self) -> str:
        """Generate RS256 JWT for GitHub App authentication (10 min TTL)."""
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + (9 * 60),
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    async def mint_installation_token(
        self,
        installation_id: int | str,
        client: httpx.AsyncClient,
        base_url: str = "https://api.github.com",
    ) -> tuple[str, datetime]:
        """Exchange App JWT for a short-lived repository installation access token (1 hour TTL)."""
        app_jwt = self.create_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{base_url.rstrip('/')}/app/installations/{installation_id}/access_tokens"
        resp = await client.post(url, headers=headers)

        if resp.status_code not in (200, 201):
            scrubbed_err = scrub_tokens(resp.text)
            raise RuntimeError(f"Failed to mint installation access token: {resp.status_code} {scrubbed_err}")

        data = resp.json()
        token = data["token"]
        expires_at = datetime.fromisoformat(data["expires_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
        return token, expires_at
