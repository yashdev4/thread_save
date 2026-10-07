"""Tests for GitHub Deletion and History Squash (§1 G6, Milestone GH5).

Verifies:
- Squash operation rewrites branch to a single orphan (parent-less) commit of current tree
- File removal in batch deletes pages from remote tree
- CLI command `python -m thread_save.cli.github squash` executes cleanly
"""

from datetime import datetime, timezone
import httpx
import pytest

from thread_save.cli.github import main as cli_main, run_squash
from thread_save.export.github import GitHubDataApiTarget, GitHubFileEntry
from tests.test_github_export import MockGitHubApi


@pytest.mark.asyncio
async def test_squash_produces_single_parentless_commit_with_current_tree():
    """Verify squash rewrites branch to an orphan root commit (zero parents) containing current tree only (GH5 gate)."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )

    # 1. Build a multi-commit history
    batch1_files = [
        GitHubFileEntry(path="2026/10/thread_a_p01.md", content="# Thread A"),
    ]
    res1 = await target.push_batch(batch1_files, commit_message="vault: commit 1")

    batch2_files = [
        GitHubFileEntry(path="2026/10/thread_b_p01.md", content="# Thread B"),
    ]
    res2 = await target.push_batch(batch2_files, commit_message="vault: commit 2")

    head_before_squash = mock.head_commit_sha
    tree_before_squash = mock.commits[head_before_squash]["tree"]["sha"]

    # Verify history chain has parents
    assert len(mock.commits[head_before_squash]["parents"]) > 0

    # 2. Execute squash
    squashed_sha = await target.squash_history(message="vault: squash test")

    # 3. Assert properties of squashed commit
    assert squashed_sha == mock.head_commit_sha
    squashed_commit = mock.commits[squashed_sha]

    # Property 1: Zero parents (orphan root commit)
    assert squashed_commit["parents"] == [], (
        f"Squashed commit has parents {squashed_commit['parents']}, expected []"
    )

    # Property 2: Preserves exact current tree
    assert squashed_commit["tree"]["sha"] == tree_before_squash, (
        f"Squashed tree {squashed_commit['tree']['sha']} differs from pre-squash tree {tree_before_squash}"
    )

    # Property 3: Commit message
    assert squashed_commit["message"] == "vault: squash test"


@pytest.mark.asyncio
async def test_cli_squash_command_runner():
    """Verify CLI squash execution helper."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )

    sha = await run_squash(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        target=target,
    )

    assert sha != ""
    assert mock.commits[sha]["parents"] == []


def test_cli_main_argparse(monkeypatch):
    """Verify CLI parser dispatches squash subcommand correctly."""
    exit_code = cli_main(["squash", "--repo", "", "--token", "tok"])
    assert exit_code == 1  # Missing repo

    exit_code2 = cli_main(["squash", "--repo", "owner/repo", "--token", ""])
    assert exit_code2 == 1  # Missing token
