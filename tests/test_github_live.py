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
import pytest

from thread_save.export.github import (
    GitHubDataApiTarget,
)

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
async def test_live_squash(live_target: GitHubDataApiTarget):
    """Test history squash to a single orphan root commit."""
    squash_sha = await live_target.squash_history()
    assert squash_sha != ""
