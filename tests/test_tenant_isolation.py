"""Verification suite for Milestone X6 (Custodial Security & Tenant Isolation).

Covers:
- Pre-insert redaction verification (secrets never touch database)
- Envelope encryption & account key derivation (AES-256-GCM + HKDF)
- Strict cross-account tenant isolation with ZERO leaks:
  - find / FTS isolation
  - stats isolation
  - bind_thread & thread_txn isolation
  - canonical render & download isolation
  - viewer token account mismatch isolation
- Thread deletion & tombstone cascade (W-10) without collateral damage
- Full account deletion cascade without affecting other tenants
- Account retention policy configuration & automated purge job
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import pytest

from thread_save.config import VaultConfig
from thread_save.security.encryption import (
    derive_account_key,
    encrypt_body,
    decrypt_body,
)
from thread_save.service import TurnService
from thread_save.storage.pg_store import PgStore
from thread_save.storage.renderer import render_thread_markdown
from thread_save.web.viewer import create_viewer_token, verify_viewer_token

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_x6_redaction_before_insert(pg_store):
    """Verify secrets are redacted before hitting the database."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="redact-user")
    svc = TurnService(pg_store, config=cfg)

    secret_key = "sk-ant-api03-1234567890123456789012345678901234567890-abcdefghij"
    secret_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThisSignature"

    prompt = f"Here is my secret Anthropic key: {secret_key} and my token: {secret_jwt}"
    res = await svc.save_turn(
        user_query=prompt,
        title_hint="Secret Thread",
        account="redact-user",
    )
    assert res["ok"]
    tid = res["thread_id"]

    # Directly inspect raw PostgreSQL row
    async with pg_store.pool.acquire() as conn:
        acc_uuid = await pg_store.resolve_account_uuid(conn, "redact-user")
        row = await conn.fetchrow(
            "SELECT body FROM turns WHERE thread_id = $1 AND n = 1 AND role = 'user'",
            tid,
        )
        assert row is not None
        stored_body = row["body"]

        # Assert raw secrets are NOT in the database
        assert secret_key not in stored_body
        assert secret_jwt not in stored_body
        assert "[REDACTED:" in stored_body


@pytest.mark.asyncio
async def test_x6_envelope_encryption():
    """Verify AES-256-GCM envelope encryption and account key derivation."""
    master_key = "global-master-encryption-key-for-threadvault-32bytes!"
    key_alice = derive_account_key(master_key, "alice")
    key_bob = derive_account_key(master_key, "bob")

    assert key_alice != key_bob
    assert len(key_alice) == 32
    assert len(key_bob) == 32

    plaintext = "Sensitive conversation turn content that requires encryption."
    ciphertext = encrypt_body(plaintext, key_alice)
    assert ciphertext != plaintext

    # Decrypt with Alice's key succeeds
    decrypted = decrypt_body(ciphertext, key_alice)
    assert decrypted == plaintext

    # Decrypt with Bob's key fails
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt_body(ciphertext, key_bob)

    # Tampered ciphertext fails
    tampered = ciphertext[:-4] + "AAAA"
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt_body(tampered, key_alice)


@pytest.mark.asyncio
async def test_x6_cross_account_tenant_isolation_zero_leaks(pg_store):
    """Zero leaks: Tenant A can never read, find, bind, or render Tenant B's data."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="tenant-a")
    svc = TurnService(pg_store, config=cfg)

    # Tenant A creates and populates thread
    res_a = await svc.save_turn(
        user_query="Tenant A secret blueprint for widget",
        title_hint="Blueprint A",
        account="tenant-a",
    )
    assert res_a["ok"]
    tid_a = res_a["thread_id"]

    # Tenant B creates and populates thread
    res_b = await svc.save_turn(
        user_query="Tenant B financial report Q4",
        title_hint="Finance B",
        account="tenant-b",
    )
    assert res_b["ok"]
    tid_b = res_b["thread_id"]

    # 1. Search / FTS isolation: Tenant B searching for Tenant A's content gets 0 hits
    hits_b = await svc.find(query="blueprint", account="tenant-b")
    assert len(hits_b) == 0

    hits_a = await svc.find(query="blueprint", account="tenant-a")
    assert len(hits_a) == 1
    assert hits_a[0].thread_id == tid_a

    # 2. Stats isolation: Tenant B cannot query stats for Tenant A's thread
    stats_b = await pg_store.stats("tenant-b", tid_a)
    assert stats_b is None

    # 3. Render isolation: Tenant B cannot render Tenant A's thread
    with pytest.raises(ValueError, match="not found"):
        await render_thread_markdown(pg_store, "tenant-b", tid_a)

    # 4. Bind isolation: Tenant B trying to bind with Tenant A's thread_id cannot hijack it
    bind_attempt = await pg_store.bind_thread("tenant-b", tid_a, None)
    assert bind_attempt.thread_id is None or bind_attempt.thread_id != tid_a

    # 5. Viewer token isolation: Tenant B's viewer token for tid_a fails account verification
    token_b_for_a = create_viewer_token(tid_a, "tenant-b")
    _, acc = verify_viewer_token(token_b_for_a)
    assert acc == "tenant-b"
    # Even if token is valid HMAC for (tid_a, tenant-b), rendering under tenant-b fails
    with pytest.raises(ValueError, match="not found"):
        await render_thread_markdown(pg_store, acc, tid_a)


@pytest.mark.asyncio
async def test_x6_thread_and_account_deletion_cascade(pg_store):
    """Verify thread & account deletion cascades and tombstones without collateral damage."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="del-user-1")
    svc = TurnService(pg_store, config=cfg)

    # Create thread 1 for user 1
    res1 = await svc.save_turn(user_query="Turn 1 user 1", title_hint="Thread 1", account="del-user-1")
    tid1 = res1["thread_id"]

    # Create thread 2 for user 1
    res2 = await svc.save_turn(user_query="Turn 1 thread 2 user 1", title_hint="Thread 2", account="del-user-1")
    tid2 = res2["thread_id"]

    # Create thread for user 2
    res_other = await svc.save_turn(user_query="Other user thread", title_hint="Other", account="del-user-2")
    tid_other = res_other["thread_id"]

    # 1. Delete thread 1 for user 1
    deleted = await pg_store.delete_thread("del-user-1", tid1)
    assert deleted is True

    # Thread 1 is tombstoned
    assert await pg_store.is_tombstoned("del-user-1", tid1) is True
    # Thread 1 turns are gone
    async with pg_store.pool.acquire() as conn:
        turn_count = await conn.fetchval("SELECT count(*) FROM turns WHERE thread_id = $1", tid1)
        assert turn_count == 0

    # User 1's Thread 2 and User 2's thread are unaffected
    assert await pg_store.is_tombstoned("del-user-1", tid2) is False
    assert await pg_store.is_tombstoned("del-user-2", tid_other) is False

    # Attempting to save turn to tombstoned thread 1 returns tombstoned and writes nothing
    save_tomb = await svc.save_turn(user_query="Attempt write to dead thread", thread_id=tid1, account="del-user-1")
    assert save_tomb.get("tombstoned") is True

    # 2. Delete user 1 full account
    acc_deleted = await pg_store.delete_account("del-user-1")
    assert acc_deleted is True

    # User 1's Thread 2 is gone
    async with pg_store.pool.acquire() as conn:
        u1_threads = await conn.fetchval(
            "SELECT count(*) FROM threads WHERE id IN ($1, $2)", tid1, tid2
        )
        assert u1_threads == 0

    # User 2's data is completely intact
    stats_other = await pg_store.stats("del-user-2", tid_other)
    assert stats_other is not None
    assert stats_other.total_turns >= 1


@pytest.mark.asyncio
async def test_x6_retention_policy_and_purge_job(pg_store):
    """Verify per-account retention policy and automated expiration purge job."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="ret-user")
    svc = TurnService(pg_store, config=cfg)

    # Create thread for ret-user
    res = await svc.save_turn(
        user_query="Expiring thread content",
        title_hint="To Expire",
        account="ret-user",
    )
    tid = res["thread_id"]

    # Create thread for permanent-user (no retention)
    res_perm = await svc.save_turn(
        user_query="Permanent thread content",
        title_hint="Keep Forever",
        account="permanent-user",
    )
    tid_perm = res_perm["thread_id"]

    # Configure 30-day retention for ret-user
    await pg_store.set_account_retention("ret-user", retention_days=30)

    # 1. Purge job with current time: thread was just created, so 0 threads purged
    purged = await pg_store.purge_expired_threads()
    assert tid not in purged

    # 2. Simulate 45 days passing
    future_time = datetime.now(timezone.utc) + timedelta(days=45)
    purged_future = await pg_store.purge_expired_threads(now=future_time)
    assert tid in purged_future

    # Verify tid is now tombstoned and deleted
    assert await pg_store.is_tombstoned("ret-user", tid) is True
    async with pg_store.pool.acquire() as conn:
        remaining = await conn.fetchval("SELECT count(*) FROM threads WHERE id = $1", tid)
        assert remaining == 0

    # Verify permanent-user's thread was NOT purged
    assert tid_perm not in purged_future
    stats_perm = await pg_store.stats("permanent-user", tid_perm)
    assert stats_perm is not None


@pytest.mark.asyncio
async def test_h6_vault_find_with_envelope_encryption(pg_store):
    """Milestone H6: vault_find with envelope encryption.
    
    Verifies:
    - search titles only (body-only keywords do not match)
    - search_mode: "titles_only" is explicitly returned
    - never silently empty: returns explanatory notice when empty
    - plaintext search returns search_mode: "full_text"
    """
    from thread_save.web.mcp_server import create_http_mcp_server
    from thread_save.web.context import current_account_id

    # 1. Plaintext setup (envelope_encryption_enabled=False)
    cfg_plain = VaultConfig(
        vault_root=Path("./test_vault"),
        default_account="crypto-user",
        envelope_encryption_enabled=False,
    )
    svc_plain = TurnService(pg_store, config=cfg_plain)

    res_plain = await svc_plain.save_turn(
        user_query="Quantum computing entanglement secrets in turn body",
        title_hint="Physics Notes",
        account="crypto-user",
    )
    assert res_plain["ok"]
    tid = res_plain["thread_id"]

    # In plaintext mode, body keyword matches and search_mode is full_text
    server_plain = create_http_mcp_server(svc_plain)
    token = current_account_id.set("crypto-user")
    try:
        # Search body keyword
        find_tool_plain = server_plain._tool_manager._tools["vault_find"].fn
        res_find_plain = await find_tool_plain(query="entanglement")
        assert res_find_plain["ok"] is True
        assert res_find_plain["search_mode"] == "full_text"
        assert res_find_plain["count"] >= 1
        assert any(t["thread_id"] == tid for t in res_find_plain["threads"])
    finally:
        current_account_id.reset(token)

    # 2. Envelope encryption setup (envelope_encryption_enabled=True)
    cfg_enc = VaultConfig(
        vault_root=Path("./test_vault"),
        default_account="crypto-user",
        envelope_encryption_enabled=True,
        master_key="custodial-master-key-32bytes-long!",
    )
    svc_enc = TurnService(pg_store, config=cfg_enc)
    server_enc = create_http_mcp_server(svc_enc)

    token = current_account_id.set("crypto-user")
    try:
        find_tool_enc = server_enc._tool_manager._tools["vault_find"].fn

        # A: Search by title keyword ("Physics") -> matches in titles_only mode
        res_title = await find_tool_enc(query="Physics")
        assert res_title["ok"] is True
        assert res_title["search_mode"] == "titles_only"
        assert res_title["count"] >= 1
        assert any(t["thread_id"] == tid for t in res_title["threads"])

        # B: Search by body-only keyword ("entanglement") -> 0 hits, never silently empty!
        res_body = await find_tool_enc(query="entanglement")
        assert res_body["ok"] is True
        assert res_body["search_mode"] == "titles_only"
        assert res_body["count"] == 0
        assert res_body["threads"] == []
        # NEVER SILENTLY EMPTY: must have an informative notice explaining encryption & titles only
        assert "notice" in res_body
        assert "envelope encryption" in res_body["notice"].lower()
        assert "titles only" in res_body["notice"].lower()

        # C: Search for completely non-existent term -> 0 hits, never silently empty!
        res_none = await find_tool_enc(query="nonexistent-xyz")
        assert res_none["ok"] is True
        assert res_none["search_mode"] == "titles_only"
        assert res_none["count"] == 0
        assert res_none["threads"] == []
        assert "notice" in res_none
        assert "envelope encryption" in res_none["notice"].lower()

    finally:
        current_account_id.reset(token)

