"""Tests for GitHub Data API Exporter (§1 G1, Milestone GH1).

Verifies:
- Happy batch (creation and deletion)
- 50-file batch uses exactly 3 write calls (no individual blob creations)
- Non-force ref update recovers automatically from 422 moved-ref race
- Secondary rate limit 403 triggers exponential backoff and eventual retry
- Public repository is strictly refused by private-repo guard
"""

import json
from typing import Any, Optional
import httpx
import pytest

from thread_save.export.github import (
    GitHubBatchResult,
    GitHubDataApiTarget,
    GitHubExportError,
    GitHubFileEntry,
    MovedRefMaxRestartsError,
    PublicRepoRefusedError,
    PushProtectionError,
    SecondaryRateLimitError,
)


class MockGitHubApi:
    """Mock GitHub REST API simulating Git Data API and repo endpoints."""

    def __init__(self, repo: str = "testowner/testrepo", is_private: bool = True, branch: str = "main"):
        self.repo = repo
        self.is_private = is_private
        self.branch = branch

        self.head_commit_sha = "c001_initial_head"
        self.base_tree_sha = "t001_base_tree"

        self.commits: dict[str, dict[str, Any]] = {
            self.head_commit_sha: {
                "sha": self.head_commit_sha,
                "tree": {"sha": self.base_tree_sha},
                "message": "Initial commit",
                "parents": [],
            }
        }
        self.trees: dict[str, dict[str, Any]] = {
            self.base_tree_sha: {
                "sha": self.base_tree_sha,
                "tree": [],
            }
        }

        self.write_calls: list[dict[str, Any]] = []
        self.all_requests: list[httpx.Request] = []

        self.simulate_race_attempts = 0
        self.simulate_secondary_403_count = 0
        self.simulate_push_protection = False

        self.tree_counter = 1
        self.commit_counter = 1

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.all_requests.append(request)
        path = request.url.path
        method = request.method

        headers = {
            "Content-Type": "application/json",
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4995",
            "x-ratelimit-reset": "1700000000",
            "x-ratelimit-used": "5",
        }

        # Check secondary rate limit simulation
        if self.simulate_secondary_403_count > 0:
            self.simulate_secondary_403_count -= 1
            headers["retry-after"] = "1"
            return httpx.Response(
                403,
                headers=headers,
                json={"message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."},
            )

        # Check secret scanning push protection simulation
        if self.simulate_push_protection and method == "PATCH":
            return httpx.Response(
                403,
                headers=headers,
                json={"message": "Push rejected: push protection detected a secret in commit."},
            )

        # 1. Repo metadata: GET /repos/{owner}/{repo}
        if method == "GET" and path == f"/repos/{self.repo}":
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "name": self.repo.split("/")[1],
                    "full_name": self.repo,
                    "private": self.is_private,
                },
            )

        # 2. Get head ref: GET /repos/{owner}/{repo}/git/ref/heads/{branch} or /git/refs/heads/{branch}
        if method == "GET" and (
            path == f"/repos/{self.repo}/git/ref/heads/{self.branch}"
            or path == f"/repos/{self.repo}/git/refs/heads/{self.branch}"
        ):
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "ref": f"refs/heads/{self.branch}",
                    "object": {"sha": self.head_commit_sha, "type": "commit"},
                },
            )

        # 3. Get commit: GET /repos/{owner}/{repo}/git/commits/{sha}
        if method == "GET" and path.startswith(f"/repos/{self.repo}/git/commits/"):
            sha = path.split("/")[-1]
            if sha in self.commits:
                return httpx.Response(200, headers=headers, json=self.commits[sha])
            return httpx.Response(404, headers=headers, json={"message": "Commit not found"})

        # 4. Get tree: GET /repos/{owner}/{repo}/git/trees/{sha}
        if method == "GET" and path.startswith(f"/repos/{self.repo}/git/trees/"):
            sha = path.split("/")[-1]
            if sha in self.trees:
                return httpx.Response(200, headers=headers, json=self.trees[sha])
            return httpx.Response(404, headers=headers, json={"message": "Tree not found"})

        # 5. Create tree: POST /repos/{owner}/{repo}/git/trees (Write #1)
        if method == "POST" and path == f"/repos/{self.repo}/git/trees":
            payload = json.loads(request.content.decode("utf-8"))
            self.write_calls.append({"method": "POST", "endpoint": "/git/trees", "payload": payload})
            self.tree_counter += 1
            new_sha = f"t{self.tree_counter:03d}_new_tree"
            self.trees[new_sha] = {"sha": new_sha, "tree": payload.get("tree", [])}
            return httpx.Response(201, headers=headers, json={"sha": new_sha})

        # 6. Create commit: POST /repos/{owner}/{repo}/git/commits (Write #2)
        if method == "POST" and path == f"/repos/{self.repo}/git/commits":
            payload = json.loads(request.content.decode("utf-8"))
            self.write_calls.append({"method": "POST", "endpoint": "/git/commits", "payload": payload})
            self.commit_counter += 1
            new_sha = f"c{self.commit_counter:03d}_new_commit"
            self.commits[new_sha] = {
                "sha": new_sha,
                "tree": {"sha": payload["tree"]},
                "message": payload.get("message", ""),
                "parents": payload.get("parents", []),
            }
            return httpx.Response(201, headers=headers, json={"sha": new_sha})

        # 7. Update ref: PATCH /repos/{owner}/{repo}/git/refs/heads/{branch} (Write #3)
        if method == "PATCH" and path == f"/repos/{self.repo}/git/refs/heads/{self.branch}":
            payload = json.loads(request.content.decode("utf-8"))
            self.write_calls.append({"method": "PATCH", "endpoint": "/git/refs/heads", "payload": payload})

            # Check moved-ref race simulation
            if self.simulate_race_attempts > 0:
                self.simulate_race_attempts -= 1
                # Move the head commit concurrently!
                concurrent_sha = f"c999_concurrent_move_{self.simulate_race_attempts}"
                self.commits[concurrent_sha] = {
                    "sha": concurrent_sha,
                    "tree": {"sha": self.base_tree_sha},
                    "message": "Concurrent commit",
                    "parents": [self.head_commit_sha],
                }
                self.head_commit_sha = concurrent_sha
                return httpx.Response(
                    422,
                    headers=headers,
                    json={"message": "Reference cannot be updated", "documentation_url": "https://docs.github.com"},
                )

            # Success: update head commit
            self.head_commit_sha = payload["sha"]
            return httpx.Response(
                200,
                headers=headers,
                json={"ref": f"refs/heads/{self.branch}", "object": {"sha": self.head_commit_sha}},
            )

        return httpx.Response(404, headers=headers, json={"message": f"Endpoint not mocked: {method} {path}"})


@pytest.mark.asyncio
async def test_happy_batch():
    """Verify standard batch with files and deletions."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )

    files = [
        GitHubFileEntry(path="2026/10/thread_p01.md", content="# Thread Content Page 1"),
        GitHubFileEntry(path="2026/10/thread_p02.md", content="# Thread Content Page 2"),
        GitHubFileEntry(path="2026/10/old_page_p03.md", content=None),  # Deletion
    ]

    result = await target.push_batch(files, commit_message="vault: test batch")

    assert result.files_written == 2
    assert result.files_deleted == 1
    assert result.conflicts == []
    assert result.restarts == 0
    assert result.commit_sha != ""
    assert result.tree_sha != ""

    # Verify write sequence: exactly 3 write calls
    assert len(mock.write_calls) == 3
    assert mock.write_calls[0]["endpoint"] == "/git/trees"
    assert mock.write_calls[1]["endpoint"] == "/git/commits"
    assert mock.write_calls[2]["endpoint"] == "/git/refs/heads"

    # Verify inline tree entries
    tree_items = mock.write_calls[0]["payload"]["tree"]
    assert len(tree_items) == 3
    assert tree_items[0]["path"] == "2026/10/thread_p01.md"
    assert tree_items[0]["content"] == "# Thread Content Page 1"
    assert tree_items[2]["path"] == "2026/10/old_page_p03.md"
    assert tree_items[2]["sha"] is None


@pytest.mark.asyncio
async def test_50_file_batch_three_write_calls():
    """Verify 50-file batch results in exactly 3 write requests (no separate blob calls)."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )

    files = [
        GitHubFileEntry(path=f"2026/10/thread_{i:02d}_p01.md", content=f"# Content for thread {i}")
        for i in range(50)
    ]

    result = await target.push_batch(files, commit_message="vault: 50 threads batch")

    assert result.files_written == 50
    assert result.files_deleted == 0
    assert result.conflicts == []

    # Count write requests
    write_methods = [call["method"] for call in mock.write_calls]
    assert len(write_methods) == 3
    assert write_methods == ["POST", "POST", "PATCH"]

    # Verify no individual blob endpoints were called
    blob_requests = [req for req in mock.all_requests if "/git/blobs" in req.url.path]
    assert len(blob_requests) == 0

    # Verify all 50 items were packaged inline in the single tree call
    tree_items = mock.write_calls[0]["payload"]["tree"]
    assert len(tree_items) == 50


@pytest.mark.asyncio
async def test_moved_ref_race():
    """Verify non-force ref update automatically restarts and succeeds when branch moves (422)."""
    mock = MockGitHubApi(is_private=True)
    mock.simulate_race_attempts = 1  # 1 race conflict on ref update
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
        max_restarts=3,
    )

    files = [
        GitHubFileEntry(path="2026/10/thread_p01.md", content="# Thread Content"),
    ]

    result = await target.push_batch(files, commit_message="vault: test moved ref")

    # Target detected 422, restarted on new head commit, and succeeded
    assert result.restarts == 1
    assert result.files_written == 1

    # Verify commit parents on the final commit point to the concurrent head commit
    final_commit = mock.commits[result.commit_sha]
    assert "c999_concurrent_move_0" in final_commit["parents"]


@pytest.mark.asyncio
async def test_secondary_limit_403():
    """Verify secondary rate limit 403 triggers backoff and eventual success."""
    mock = MockGitHubApi(is_private=True)
    mock.simulate_secondary_403_count = 2  # Fail twice with secondary 403
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    slept_durations: list[float] = []

    async def fake_sleep(duration: float):
        slept_durations.append(duration)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
        max_secondary_retries=3,
        backoff_sleep_fn=fake_sleep,
    )

    files = [
        GitHubFileEntry(path="2026/10/thread_p01.md", content="# Thread Content"),
    ]

    result = await target.push_batch(files, commit_message="vault: rate limit test")

    assert result.files_written == 1
    # Slept twice due to 2 secondary 403 responses
    assert len(slept_durations) == 2
    assert slept_durations[0] == 1.0
    assert slept_durations[1] == 1.0


@pytest.mark.asyncio
async def test_secondary_limit_exhaustion_raises():
    """Verify exhausting secondary rate limit retries raises SecondaryRateLimitError."""
    mock = MockGitHubApi(is_private=True)
    mock.simulate_secondary_403_count = 5  # More than max retries
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    async def fake_sleep(duration: float):
        pass

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
        max_secondary_retries=2,
        backoff_sleep_fn=fake_sleep,
    )

    files = [
        GitHubFileEntry(path="2026/10/thread_p01.md", content="# Thread Content"),
    ]

    with pytest.raises(SecondaryRateLimitError):
        await target.push_batch(files, commit_message="vault: will fail")


@pytest.mark.asyncio
async def test_public_repo_refused():
    """Verify public repositories are strictly refused for export (G6)."""
    mock = MockGitHubApi(is_private=False)  # Public repository!
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )

    files = [
        GitHubFileEntry(path="2026/10/thread_p01.md", content="# Private Thread"),
    ]

    with pytest.raises(PublicRepoRefusedError):
        await target.push_batch(files, commit_message="vault: public repo attempt")

    # Zero write calls made
    assert len(mock.write_calls) == 0
