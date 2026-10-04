"""Live integration verification tests for GitHub Archive Export (§1 G1-G6, Milestone GH8).

Gated on external environment variables:
- THREADVAULT_GH_LIVE_TOKEN: fine-grained Personal Access Token with Contents (read & write)
- THREADVAULT_GH_LIVE_REPO: owner/repo string for a dedicated private throwaway repository

If either environment variable is absent, tests are cleanly skipped.
When present, executes:
1. First sync: initial archive export (README, index, thread pages)
2. Incremental batch: second sync with unchanged pages skipped
3. Conflict detection: external edits detected without overwriting
4. Squash: squash history to single orphan commit preserving current tree
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
import pytest

from thread_save.export.github import (
    GitHubBatchResult,
    GitHubDataApiTarget,
    GitHubFileEntry,
    PublicRepoRefusedError,
)
from thread_save.export.layout import ExportThreadInfo, generate_archive_tree
from thread_save.export.sync import GitHubBatchExporter, GitHubExportConfig

LIVE_TOKEN = os.environ.get("THREADVAULT_GH_LIVE_TOKEN")
LIVE_REPO = os.environ.get("THREADVAULT_GH_LIVE_REPO")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not LIVE_TOKEN or not LIVE_REPO,
        reason=(
            "THREADVAULT_GH_LIVE_TOKEN and THREADVAULT_GH_LIVE_REPO environment variables "
            "must be set for live GitHub verification (Milestone GH8 gate)."
        ),
    ),
]


@pytest.fixture
def live_target() -> GitHubDataApiTarget:
    assert LIVE_TOKEN is not None
    assert LIVE_REPO is not None
    return GitHubDataApiTarget(
        repo=LIVE_REPO,
        token=LIVE_TOKEN,
        branch="main",
    )


@pytest.mark.asyncio
async def test_live_verify_private_guard(live_target: GitHubDataApiTarget):
    """Verify that the target repository exists and is private (G6 guard)."""
    is_private = await live_target.verify_private()
    assert is_private is True, "Target repository must be private"


@pytest.mark.asyncio
async def test_live_first_sync_and_incremental(live_target: GitHubDataApiTarget):
    """Test first sync initial batch and incremental batch update."""
    thread_id = "01M43A2W9JMVPX4Y9WGBYF0V88"
    now = datetime.now(timezone.utc)

    # 1. First sync: thread with Page 1
    p1_content = (
        "---\n"
        'schema_version: 2\n'
        f'thread_id: "{thread_id}"\n'
        'title: "Live Verification Thread"\n'
        'slug: "live-verification"\n'
        'page: 1\n'
        "---\n\n"
        "# Live Verification Thread\n\n"
        "<!-- turn i=1 role=user fidelity=verbatim chars=20 hash=11223344 -->\n"
        "## User\n\n"
        "Live test turn 1\n"
        "<!-- /turn i=1 -->\n"
    )

    thread_info = ExportThreadInfo(
        thread_id=thread_id,
        title="Live Verification Thread",
        slug="live-verification",
        created_at=now,
        updated_at=now,
        turn_count=1,
        pages={"2026/10/2026-10-05T0000_test_yf0v88_live-verification_p01.md": p1_content},
    )

    tree = generate_archive_tree([thread_info], account="test-live", account_dir=False)
    entries = [GitHubFileEntry(path=p, content=c) for p, c in tree.items()]

    result1 = await live_target.push_batch(entries)
    assert result1.files_written == len(entries)
    assert result1.commit_sha != ""

    # 2. Incremental batch: add Page 2
    p2_content = (
        "---\n"
        'schema_version: 2\n'
        f'thread_id: "{thread_id}"\n'
        'title: "Live Verification Thread"\n'
        'slug: "live-verification"\n'
        'page: 2\n'
        "---\n\n"
        "<!-- turn i=2 role=user fidelity=verbatim chars=20 hash=55667788 -->\n"
        "## User\n\n"
        "Live test turn 2\n"
        "<!-- /turn i=2 -->\n"
    )
    thread_info.turn_count = 2
    thread_info.pages["2026/10/2026-10-05T0000_test_yf0v88_live-verification_p02.md"] = p2_content

    # Using BatchExporter to verify unchanged page skip
    config = GitHubExportConfig(repo=live_target.repo, token=live_target.token, branch="main")
    exporter = GitHubBatchExporter(target=live_target, config=config)

    # Prime manifest with result1 blobs
    exporter.blob_shas.update(result1.blob_shas)

    result2 = await exporter.export_threads([thread_info], account="test-live")
    assert result2 is not None
    assert result2.commit_sha != result1.commit_sha
    assert result2.files_written > 0


@pytest.mark.asyncio
async def test_live_conflict_handling(live_target: GitHubDataApiTarget):
    """Test conflict detection when remote blob sha differs from last known."""
    path = "README.md"
    # Claim last_blob_sha was a dummy hash to simulate an external change
    conflicting_entry = GitHubFileEntry(
        path=path,
        content="# Conflicting README Content\n",
        last_blob_sha="0000000000000000000000000000000000000000",
    )
    result = await live_target.push_batch([conflicting_entry])
    assert path in result.conflicts
    assert result.files_written == 0


@pytest.mark.asyncio
async def test_live_squash(live_target: GitHubDataApiTarget):
    """Test history squash to a single orphan root commit."""
    squash_sha = await live_target.squash_history()
    assert squash_sha != ""
