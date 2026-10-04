"""Tests for Local Offload, Pointer Management, Rehydration, and Continuation (§1 G7, Milestone GH7).

Covers:
1. Offload frees disk files and writes pointer to _index/offloaded.json.
2. Resumed conversation binds and dedups correctly using pointer's recent_turn_keys.
3. Rehydrate fetches from GitHub at commit, verifies hashes, restores files cleanly, and appends new turns.
4. Unreachable GitHub (or hash mismatch) creates continuation thread with 'continues: <thread_id>' in front matter, returning ok: true.
5. fsck passes cleanly in offloaded state, and detects stray files for offloaded threads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.export.github import GitHubDataApiTarget, GitHubExportError
from thread_save.export.offload import (
    OffloadManager,
    compute_page_hash,
    verify_page_hash,
)
from thread_save.fsck import verify_vault
from thread_save.service import TurnService
from thread_save.storage.formatter import parse_front_matter
from thread_save.storage.path_resolver import ensure_vault_structure
from thread_save.storage.writer import FileStore


@pytest.fixture
def offload_vault(tmp_path: Path) -> tuple[FileStore, TurnService, VaultConfig]:
    vault_root = tmp_path / "offload_vault"
    vault_root.mkdir(parents=True, exist_ok=True)
    config = VaultConfig(
        vault_root=vault_root,
        default_account="test-user",
        page_max_turns=3,  # Low threshold to test multi-page rollover
        page_max_bytes=10_000,
        offload_after_days=14,
    )
    ensure_vault_structure(config)
    store = FileStore(config)
    service = TurnService(store, config=config)
    return store, service, config


@pytest.mark.asyncio
async def test_offload_frees_disk_files_and_writes_pointer(offload_vault):
    """Offload condition passes -> files removed from disk -> pointer written to _index/offloaded.json."""
    store, service, config = offload_vault
    offload_mgr: OffloadManager = store.offload_manager

    # 1. Create a thread with multiple turns across 2 pages
    r1 = await service.save_turn(
        user_query="How do I configure postgres asyncpg pool in Python?",
        prev_response="Use asyncpg.create_pool with min_size and max_size.",
        title_hint="Asyncpg Pool Config",
    )
    tid = r1["thread_id"]

    await service.save_turn(
        user_query="What timeout settings are recommended?",
        prev_response="Set timeout=10 and command_timeout=30.",
        thread_id=tid,
        client_turn_number=2,
    )

    # 3rd turn triggers rollover or page 2
    await service.save_turn(
        user_query="How do I handle pool exhaustion under load?",
        prev_response="Use queue limits and backoff.",
        thread_id=tid,
        client_turn_number=3,
    )
    await service.save_turn(
        user_query="What about connection health checks?",
        prev_response="Execute SELECT 1 on checkout.",
        thread_id=tid,
        client_turn_number=4,
    )

    page_files = list(config.vault_root.rglob("*.md"))
    assert len(page_files) >= 2, "Expected at least 2 page files across rollover"

    # 2. Simulate export to GitHub: record in manifest
    manifest_data: dict[str, str] = {}
    for pf in page_files:
        rel = pf.relative_to(config.vault_root).as_posix()
        manifest_data[rel] = compute_page_hash(pf.read_text(encoding="utf-8"))

    commit_sha = "9c1f000000000000000000000000000000000001"
    repo_name = "test-user/threadvault-archive"
    offload_mgr.manifest_mgr.record_export(
        repo=repo_name,
        commit_sha=commit_sha,
        pages_exported=manifest_data,
        blob_shas={k: "blob_sha_123" for k in manifest_data},
    )

    # 3. Check offload condition when idle < 14 days -> should be False
    is_off, reason = offload_mgr.is_thread_offloadable(store, tid)
    assert not is_off
    assert "days" in reason

    # 4. Check offload condition with future reference time (e.g. 15 days later)
    now_future = datetime.now(timezone.utc) + timedelta(days=15)
    is_off_future, reason_future = offload_mgr.is_thread_offloadable(store, tid, now=now_future)
    assert is_off_future, f"Expected offloadable, got reason: {reason_future}"

    # 5. Execute offload
    pointer = offload_mgr.offload_thread(store, tid, repo=repo_name, commit=commit_sha)
    assert pointer.thread_id == tid
    assert pointer.repo == repo_name
    assert pointer.commit == commit_sha
    assert len(pointer.pages) >= 2
    assert pointer.max_n == 4
    assert len(pointer.recent_turn_keys) > 0
    assert pointer.delim != ""

    # Verify: local markdown page files are DELETED from disk
    remaining_pages = list(config.vault_root.rglob("*.md"))
    assert len(remaining_pages) == 0, f"Expected 0 page files remaining on disk, found {remaining_pages}"

    # Verify: _index/offloaded.json contains the thread pointer
    assert config.offloaded_json_path.exists()
    offloaded_raw = json.loads(config.offloaded_json_path.read_text(encoding="utf-8"))
    assert tid in offloaded_raw
    assert offloaded_raw[tid]["commit"] == commit_sha
    assert offloaded_raw[tid]["repo"] == repo_name


@pytest.mark.asyncio
async def test_resumed_conversation_dedup_via_pointer_keys(offload_vault):
    """Resumed conversation with duplicate turn dedups via pointer recent_turn_keys without rehydration."""
    store, service, config = offload_vault
    offload_mgr: OffloadManager = store.offload_manager

    # Create thread
    r1 = await service.save_turn(
        user_query="Explain distributed consensus algorithms like Raft and Paxos.",
        title_hint="Distributed Consensus",
    )
    tid = r1["thread_id"]

    page_files = list(config.vault_root.rglob("*.md"))
    manifest_data = {
        pf.relative_to(config.vault_root).as_posix(): compute_page_hash(pf.read_text("utf-8"))
        for pf in page_files
    }
    commit_sha = "aabbcc11223344556677889900aabbcc11223344"
    offload_mgr.manifest_mgr.record_export(
        repo="user/repo",
        commit_sha=commit_sha,
        pages_exported=manifest_data,
        blob_shas={k: "blob_1" for k in manifest_data},
    )

    # Offload thread
    offload_mgr.offload_thread(store, tid, repo="user/repo", commit=commit_sha)
    assert len(list(config.vault_root.rglob("*.md"))) == 0

    # Resend the exact same turn
    dup_res = await service.save_turn(
        user_query="Explain distributed consensus algorithms like Raft and Paxos.",
        thread_id=tid,
    )
    assert dup_res["ok"] is True
    assert dup_res["thread_id"] == tid
    assert dup_res.get("action") == "no_op"
    # Ensure no files were re-created on disk for duplicate
    assert len(list(config.vault_root.rglob("*.md"))) == 0


@pytest.mark.asyncio
async def test_rehydrate_restores_files_cleanly(offload_vault):
    """Rehydrate fetches pages from GitHub at commit, verifies hashes, restores disk files, appends new turn."""
    store, service, config = offload_vault
    offload_mgr: OffloadManager = store.offload_manager

    # 1. Create original thread
    r1 = await service.save_turn(
        user_query="What is B-tree indexing in relational databases?",
        prev_response="B-trees provide balanced logarithmic search and ordered traversals.",
        title_hint="B-Tree Indexing",
    )
    tid = r1["thread_id"]

    page_files = list(config.vault_root.rglob("*.md"))
    page_contents = {
        pf.relative_to(config.vault_root).as_posix(): pf.read_text("utf-8")
        for pf in page_files
    }
    manifest_data = {k: compute_page_hash(v) for k, v in page_contents.items()}
    commit_sha = "1234567890abcdef1234567890abcdef12345678"

    offload_mgr.manifest_mgr.record_export(
        repo="user/archive",
        commit_sha=commit_sha,
        pages_exported=manifest_data,
        blob_shas={k: "blob_1" for k in manifest_data},
    )
    offload_mgr.offload_thread(store, tid, repo="user/archive", commit=commit_sha)
    assert len(list(config.vault_root.rglob("*.md"))) == 0

    # 2. Setup mock GitHub target serving page_contents at commit_sha
    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        # Match /repos/user/archive/contents/{path}?ref={commit}
        for path, text in page_contents.items():
            if f"/repos/user/archive/contents/{path}" in url_str:
                return httpx.Response(200, text=text)
        return httpx.Response(404, json={"message": "Not found"})

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)
    target = GitHubDataApiTarget(
        repo="user/archive",
        token="github_pat_valid_test_token_1234567890",
        client=mock_client,
    )

    # Attach target to TurnService
    service._github_target = target

    # 3. Send a NEW turn to the offloaded thread
    res = await service.save_turn(
        user_query="Can you explain LSM trees and how they differ from B-trees?",
        thread_id=tid,
        client_turn_number=2,
    )
    assert res["ok"] is True
    assert res["thread_id"] == tid
    assert res["binding"] in ("rehydrated", "id")

    # 4. Verify: files are restored on disk and updated with turn 2
    restored_files = list(config.vault_root.rglob("*.md"))
    assert len(restored_files) > 0

    all_content = "\n".join(f.read_text("utf-8") for f in restored_files)
    assert "LSM trees" in all_content
    assert "B-tree indexing" in all_content

    # 5. Verify: thread removed from _index/offloaded.json
    offloaded_raw = json.loads(config.offloaded_json_path.read_text(encoding="utf-8"))
    assert tid not in offloaded_raw

    # 6. Verify: fsck passes cleanly on the restored vault
    fsck_res = verify_vault(config.vault_root)
    assert fsck_res == 0


@pytest.mark.asyncio
async def test_unreachable_github_fallback_to_continuation_thread(offload_vault):
    """If GitHub is unreachable or returns hash mismatch, create continuation thread with 'continues: <tid>'."""
    store, service, config = offload_vault
    offload_mgr: OffloadManager = store.offload_manager

    # 1. Create original thread
    r1 = await service.save_turn(
        user_query="Explain TLS 1.3 handshake sequence and forward secrecy.",
        title_hint="TLS 1.3 Handshake",
    )
    tid = r1["thread_id"]

    page_files = list(config.vault_root.rglob("*.md"))
    manifest_data = {
        pf.relative_to(config.vault_root).as_posix(): compute_page_hash(pf.read_text("utf-8"))
        for pf in page_files
    }
    commit_sha = "ffeeddccbbaa0099887766554433221100ffeedd"
    offload_mgr.manifest_mgr.record_export(
        repo="user/archive",
        commit_sha=commit_sha,
        pages_exported=manifest_data,
        blob_shas={k: "blob_1" for k in manifest_data},
    )
    offload_mgr.offload_thread(store, tid, repo="user/archive", commit=commit_sha)

    # 2. Mock GitHub target to fail with 500 server error
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal GitHub Server Error")

    transport = httpx.MockTransport(failing_handler)
    mock_client = httpx.AsyncClient(transport=transport)
    target = GitHubDataApiTarget(
        repo="user/archive",
        token="github_pat_valid_test_token_1234567890",
        client=mock_client,
    )
    service._github_target = target

    # 3. Send new turn -> rehydration fails, fallback triggers
    res = await service.save_turn(
        user_query="What about 0-RTT resumption security risks?",
        thread_id=tid,
        client_turn_number=2,
    )

    # 4. Assert invariant: never blocks save, returns ok: true
    assert res["ok"] is True
    new_tid = res["thread_id"]
    assert new_tid != tid, "Expected new continuation thread_id"
    assert res.get("binding") == "continuation"
    assert res.get("continues") == tid

    # 5. Assert: new continuation thread markdown file on disk contains 'continues: <tid>'
    new_files = list(config.vault_root.rglob("*.md"))
    assert len(new_files) == 1
    content = new_files[0].read_text("utf-8")
    meta, _ = parse_front_matter(content)
    assert meta.continues == tid
    assert meta.thread_id == new_tid
    assert "0-RTT resumption" in content


@pytest.mark.asyncio
async def test_fsck_validates_offloaded_state_and_detects_strays(tmp_path: Path):
    """fsck passes in pure offloaded state, and fails if stray files exist on disk for offloaded threads."""
    vault_root = tmp_path / "fsck_offload_vault"
    vault_root.mkdir(parents=True, exist_ok=True)
    config = VaultConfig(vault_root=vault_root, default_account="test-acc")
    ensure_vault_structure(config)

    # Write a valid offloaded pointer to _index/offloaded.json
    offloaded_tid = "01M43A2W9JMVPX4Y9WGBYF0V99"
    pointer_data = {
        offloaded_tid: {
            "repo": "user/test-archive",
            "commit": "9c1f000000000000000000000000000000000001",
            "pages": {
                "2026/10/2026-10-02T1423_test-acc_yf0v99_test-thread_p01.md": "v1:3fa9000000000000000000000000000000000000000000000000000000000001"
            },
            "last_user_anchor": "bulk insertion of 50k rows",
            "recent_turn_keys": ["v1:11223344556677889900aabbccddeeff"],
            "max_n": 4,
            "delim": "k7Qx",
        }
    }
    config.offloaded_json_path.write_text(json.dumps(pointer_data, indent=2), encoding="utf-8")

    # 1. In pure offloaded state (no stray local files), fsck must return 0
    res = verify_vault(vault_root)
    assert res == 0

    # 2. Introduce a stray file on disk matching the offloaded thread ID
    stray_dir = vault_root / "test-acc" / "2026" / "10"
    stray_dir.mkdir(parents=True, exist_ok=True)
    stray_file = stray_dir / "2026-10-02T1423_test-acc_yf0v99_test-thread_p01.md"
    stray_file.write_text(
        "---\n"
        '"schema_version": 2\n'
        f'"thread_id": "{offloaded_tid}"\n'
        '"title": "Stray Thread"\n'
        '"slug": "test-thread"\n'
        '"account": "test-acc"\n'
        '"client": "claude-desktop"\n'
        '"created": "2026-10-02T14:23:00+00:00"\n'
        '"updated": "2026-10-02T14:23:00+00:00"\n'
        '"page": 1\n'
        '"turn_count": 1\n'
        '"turn_range": [1, 1]\n'
        '"bytes": 100\n'
        '"nonce": "k7Qx"\n'
        "---\n\n"
        "<!-- turn i=1 role=user nonce=k7Qx fidelity=verbatim chars=10 hash=38abf165 -->\n"
        "## User\n\nStray turn\n<!-- /turn i=1 nonce=k7Qx -->\n",
        encoding="utf-8",
    )

    # 3. fsck must detect the stray file and exit with non-zero code
    stray_res = verify_vault(vault_root)
    assert stray_res == 1
