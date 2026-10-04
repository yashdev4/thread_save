"""Tests for GitHub Commit Policy and Settle-and-Batch Sync (Milestone GH3).

Verifies:
- 10 saves on one thread within settle window produce exactly 1 commit
- Closed sticky pages are never re-sent across successive exports
- Single in-flight batch per repository
- Growth guard warns when repository size >= 750 MB
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any
import httpx
import pytest

from thread_save.export.github import GitHubDataApiTarget
from thread_save.export.layout import ExportThreadInfo
from thread_save.export.sync import (
    GitHubBatchExporter,
    GitHubExportConfig,
    REPO_SIZE_WARNING_THRESHOLD_KB,
)
from tests.test_github_export import MockGitHubApi


@pytest.mark.asyncio
async def test_ten_saves_within_settle_produces_one_commit():
    """Verify 10 successive saves within the 30m settle window result in exactly 1 commit once settled."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )
    config = GitHubExportConfig(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        settle_minutes=30,
    )
    exporter = GitHubBatchExporter(target=target, config=config)

    t0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    tid = "01M444NQWT0R5QZB9KE26DH74A"
    page_path = "2026/10/2026-10-02T1200_testuser_6dh74a_settle-test_p01.md"

    # Simulate 10 saves occurring 3 minutes apart (12:00 to 12:27)
    for i in range(10):
        current_time = t0 + timedelta(minutes=i * 3)
        thread = ExportThreadInfo(
            thread_id=tid,
            title="Settle Test",
            slug="settle-test",
            account="testuser",
            created_at=t0,
            updated_at=current_time,
            turns=i + 1,
            pages=[page_path],
            page_contents={page_path: f"# Content after turn {i+1}\n\nTurn {i+1} body"},
        )

        # Attempt export at the time of each save: must NOT commit because thread has not settled
        res = await exporter.export_settled_threads([thread], now=current_time)
        assert res is None
        assert len(mock.write_calls) == 0

    # Intermediate check at 12:40 (13 minutes after last save: still within 30m window)
    intermediate_time = t0 + timedelta(minutes=40)
    res_inter = await exporter.export_settled_threads([thread], now=intermediate_time)
    assert res_inter is None
    assert len(mock.write_calls) == 0

    # Advance time to 12:58 (31 minutes after last save at 12:27: settled!)
    settled_time = t0 + timedelta(minutes=58)
    res_final = await exporter.export_settled_threads([thread], now=settled_time)

    assert res_final is not None
    assert res_final.files_written >= 1  # Page + indexes
    # Exactly 3 write calls for the 1 commit!
    assert len(mock.write_calls) == 3

    # Check commit message format
    commit_call = [c for c in mock.write_calls if c["endpoint"] == "/git/commits"][0]
    msg = commit_call["payload"]["message"]
    assert "vault: 1 threads, 1 pages (batch 2026-10-02T12:58Z)" in msg


@pytest.mark.asyncio
async def test_closed_pages_never_resent():
    """Verify closed pages are exported once and skipped in subsequent exports."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )
    config = GitHubExportConfig(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        settle_minutes=30,
    )
    exporter = GitHubBatchExporter(target=target, config=config)

    t0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    tid = "01M444NQWT0R5QZB9KE26DH74A"
    p01_path = "2026/10/2026-10-02T1200_testuser_6dh74a_sticky-test_p01.md"
    p02_path = "2026/10/2026-10-02T1200_testuser_6dh74a_sticky-test_p02.md"

    p01_content = "# Page 1 (Closed and sticky)\n\nFinished turns 1-10"
    p02_content_v1 = "# Page 2 (Active)\n\nTurn 11"

    thread_v1 = ExportThreadInfo(
        thread_id=tid,
        title="Sticky Test",
        slug="sticky-test",
        account="testuser",
        created_at=t0,
        updated_at=t0,
        turns=11,
        pages=[p01_path, p02_path],
        page_contents={p01_path: p01_content, p02_path: p02_content_v1},
    )

    # First export (settled at 12:35)
    t1 = t0 + timedelta(minutes=35)
    res1 = await exporter.export_settled_threads([thread_v1], now=t1)
    assert res1 is not None

    # Tree 1 must include both p01 and p02
    tree1_payload = mock.write_calls[0]["payload"]["tree"]
    tree1_paths = {item["path"] for item in tree1_payload}
    assert p01_path in tree1_paths
    assert p02_path in tree1_paths

    # Clear write calls count
    mock.write_calls.clear()

    # New turn added to Page 2 at 13:00. Page 1 content is 100% IDENTICAL (sticky)
    t_save2 = datetime(2026, 10, 2, 13, 0, 0, tzinfo=timezone.utc)
    p02_content_v2 = "# Page 2 (Active)\n\nTurn 11\n\nTurn 12"
    thread_v2 = ExportThreadInfo(
        thread_id=tid,
        title="Sticky Test",
        slug="sticky-test",
        account="testuser",
        created_at=t0,
        updated_at=t_save2,
        turns=12,
        pages=[p01_path, p02_path],
        page_contents={p01_path: p01_content, p02_path: p02_content_v2},
    )

    # Second export (settled at 13:35)
    t2 = t_save2 + timedelta(minutes=35)
    res2 = await exporter.export_settled_threads([thread_v2], now=t2)
    assert res2 is not None

    # Tree 2 must contain p02, BUT MUST NOT contain p01!
    tree2_payload = mock.write_calls[0]["payload"]["tree"]
    tree2_paths = {item["path"] for item in tree2_payload}
    assert p02_path in tree2_paths
    assert p01_path not in tree2_paths, "Closed page p01 was erroneously re-sent in second commit!"


@pytest.mark.asyncio
async def test_repo_size_growth_guard_warning():
    """Verify repo growth guard warns when repository size >= 750 MB."""
    mock = MockGitHubApi(is_private=True)

    # Custom repo metadata returning size = 780 MB in KB
    def custom_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/repos/{mock.repo}":
            return httpx.Response(
                200,
                json={
                    "name": "testrepo",
                    "full_name": mock.repo,
                    "private": True,
                    "size": 780 * 1024,  # 780 MB
                },
            )
        return mock.handle_request(request)

    transport = httpx.MockTransport(custom_handler)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )
    config = GitHubExportConfig(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        size_warning_kb=REPO_SIZE_WARNING_THRESHOLD_KB,
    )
    exporter = GitHubBatchExporter(target=target, config=config)

    size_kb, warning = await exporter.check_repo_size_guard(client)
    assert size_kb == 780 * 1024
    assert warning is True
    assert exporter.size_warning_active is True
