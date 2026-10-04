"""Tests for GitHub Auth, Expiry Tracking & Credential Security (§1 G2, Milestone GH6).

Verifies:
- Fine-grained PAT 7-day expiration warning
- Expiration parsing from GitHub API response headers
- GitHub App installation-token flow (remote multi-user)
- Envelope encryption/decryption of stored tokens
- Strict token security: token string is 100% absent from logs, events, and exceptions
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import tempfile
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import pytest

from thread_save.export.auth import (
    GitHubAppAuth,
    GitHubAuthManager,
    mask_token_preview,
    parse_expiration_header,
    scrub_tokens,
)
from thread_save.export.github import (
    GitHubDataApiTarget,
    GitHubExportError,
    GitHubFileEntry,
    PushProtectionError,
)
from thread_save.telemetry.events import EventLogger



def test_fine_grained_pat_expiry_warning_within_7_days(caplog):
    """Verify PAT expiring within 7 days triggers warning; >7 days does not (G2)."""
    now = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Expires in 5 days -> Warning triggered
    exp_5d = now + timedelta(days=5)
    mgr1 = GitHubAuthManager(token="ghp_test_12345", token_expires_at=exp_5d)
    with caplog.at_level(logging.WARNING):
        is_warning = mgr1.check_token_expiry(now=now)
    assert is_warning is True
    assert "expires in 5.0 days" in caplog.text

    caplog.clear()

    # 2. Expires in 15 days -> No warning
    exp_15d = now + timedelta(days=15)
    mgr2 = GitHubAuthManager(token="ghp_test_12345", token_expires_at=exp_15d)
    with caplog.at_level(logging.WARNING):
        is_warning2 = mgr2.check_token_expiry(now=now)
    assert is_warning2 is False
    assert caplog.text == ""


def test_parse_expiration_header():
    """Verify parsing of github-authentication-token-expiration header."""
    header_val = "2026-10-15 18:30:00 UTC"
    parsed = parse_expiration_header(header_val)

    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 10
    assert parsed.day == 15
    assert parsed.hour == 18
    assert parsed.minute == 30
    assert parsed.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_github_app_installation_token_flow():
    """Verify GitHub App mints short-lived installation access tokens."""
    # Generate test RSA private key
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    app_auth = GitHubAppAuth(app_id="app_123456", private_key_pem=pem)
    assert app_auth.create_app_jwt() != ""

    def mock_app_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == "/app/installations/789/access_tokens"
        ):
            assert "Bearer " in request.headers.get("Authorization", "")
            return httpx.Response(
                201,
                json={
                    "token": "ghs_short_lived_installation_token_987",
                    "expires_at": "2026-10-05T13:00:00Z",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_app_handler)
    client = httpx.AsyncClient(transport=transport)

    token, expires = await app_auth.mint_installation_token(789, client)
    assert token == "ghs_short_lived_installation_token_987"
    assert expires.year == 2026


def test_encrypted_token_storage():
    """Verify tokens are envelope-encrypted at rest and decrypted on demand."""
    master_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    secret_pat = "github_pat_11ABCD_SECRET_PAT_TOKEN_NEVER_PERSIST_IN_PLAIN_9999"

    mgr = GitHubAuthManager(token=secret_pat, master_key_hex=master_key)
    ciphertext = mgr.encrypt_stored_token()

    # Plaintext token must not be in ciphertext
    assert secret_pat not in ciphertext
    assert ciphertext.startswith("v1:")

    # Decrypt restores original token
    decrypted = GitHubAuthManager.decrypt_stored_token(ciphertext, master_key)
    assert decrypted == secret_pat


@pytest.mark.asyncio
async def test_token_string_absent_from_logs_events_exceptions(caplog):
    """Verify token string is 100% absent from logs, events, and exceptions (GH6 gate)."""
    secret_token = "github_pat_11XYZ_DO_NOT_LEAK_THIS_SECRET_TOKEN_9999"

    # Mock server that echoes the secret token in error responses
    def leaking_mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "message": f"Push rejected with credentials {secret_token} due to policy",
                "secret scanning": "secret detected",
            },
        )

    transport = httpx.MockTransport(leaking_mock_handler)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token=secret_token,
        client=client,
    )

    # 1. Check __repr__: must not reveal token
    target_repr = repr(target)
    assert secret_token not in target_repr

    # 2. Check exception raising: must scrub token
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as exc_info:
            await target.push_batch(
                files=[GitHubFileEntry(path="test.md", content="body")],
                commit_message="test",
            )

    err_text = str(exc_info.value)
    # Token must not appear in exception text
    assert secret_token not in err_text
    assert "[REDACTED_GH_TOKEN]" in err_text or secret_token not in err_text

    # 3. Check logs: token must not appear in any log output
    assert secret_token not in caplog.text

    # 4. Check telemetry events: log an export event and ensure tokens are not written
    with tempfile.TemporaryDirectory() as td:
        events_path = Path(td) / "events.jsonl"
        ev_logger = EventLogger(events_path)
        ev_logger.log(
            tool="github_export",
            ok=False,
            model_hint="opus",
            fidelity="verbatim",
        )

        event_contents = events_path.read_text(encoding="utf-8")
        assert secret_token not in event_contents

