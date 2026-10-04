"""GitHub Data API Target for asynchronous thread vault export (§1 G1, Milestone GH1).

Implements:
- Direct Git Data API operations without local clones
- 3 write requests per batch regardless of file count (inline tree blobs)
- Atomic batch creation (tree -> commit -> ref update with force=false)
- Non-force ref update with automatic restart on 422 (moved-ref race)
- Private-repo guard: strict rejection if repository is public
- Rate-limit tracking and secondary rate-limit exponential backoff
- Conflict detection against remote modifications
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
import httpx

logger = logging.getLogger("thread_save.export.github")

# GitHub tree payload limit: 20 MB nominal
MAX_TREE_PAYLOAD_BYTES = 20 * 1024 * 1024


class GitHubExportError(Exception):
    """Base exception for GitHub export operations."""


class PublicRepoRefusedError(GitHubExportError):
    """Raised when the target repository is public, violating privacy requirements."""


class SecondaryRateLimitError(GitHubExportError):
    """Raised when GitHub secondary rate limit (abuse detection) is encountered."""


class RateLimitExceededError(GitHubExportError):
    """Raised when primary GitHub API rate limit is exhausted."""


class MovedRefMaxRestartsError(GitHubExportError):
    """Raised when concurrent branch updates cause 422 retries to exceed max limit."""


class GitHubConflictError(GitHubExportError):
    """Raised when remote contents conflict with local state."""


class PushProtectionError(GitHubExportError):
    """Raised when GitHub secret scanning push protection rejects a commit."""


@dataclass
class GitHubFileEntry:
    path: str
    content: Optional[str] = None  # None indicates deletion
    last_blob_sha: Optional[str] = None  # For conflict checking


@dataclass
class GitHubBatchResult:
    commit_sha: str
    tree_sha: str
    files_written: int
    files_deleted: int
    conflicts: list[str] = field(default_factory=list)
    restarts: int = 0
    blob_shas: dict[str, str] = field(default_factory=dict)



class GitHubDataApiTarget:
    """Exports thread vault files to a private GitHub repository via the Git Data API."""

    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = "main",
        base_url: str = "https://api.github.com",
        client: Optional[httpx.AsyncClient] = None,
        max_restarts: int = 3,
        max_secondary_retries: int = 3,
        base_backoff_seconds: float = 60.0,
        backoff_sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
    ):
        self.repo = repo.strip("/")
        self.token = token
        self.branch = branch
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.max_restarts = max_restarts
        self.max_secondary_retries = max_secondary_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.backoff_sleep_fn = backoff_sleep_fn or asyncio.sleep

        # Rate limit tracking
        self.ratelimit_limit: Optional[int] = None
        self.ratelimit_remaining: Optional[int] = None
        self.ratelimit_reset: Optional[int] = None
        self.ratelimit_used: Optional[int] = None

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ThreadVault-Export/1.0",
        }

    @contextlib.asynccontextmanager
    async def _get_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                yield client

    def _update_ratelimits(self, headers: httpx.Headers) -> None:
        if "x-ratelimit-limit" in headers:
            try:
                self.ratelimit_limit = int(headers["x-ratelimit-limit"])
            except ValueError:
                pass
        if "x-ratelimit-remaining" in headers:
            try:
                self.ratelimit_remaining = int(headers["x-ratelimit-remaining"])
            except ValueError:
                pass
        if "x-ratelimit-reset" in headers:
            try:
                self.ratelimit_reset = int(headers["x-ratelimit-reset"])
            except ValueError:
                pass
        if "x-ratelimit-used" in headers:
            try:
                self.ratelimit_used = int(headers["x-ratelimit-used"])
            except ValueError:
                pass

    async def _request(
        self,
        method: str,
        path: str,
        client: httpx.AsyncClient,
        json_data: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        retries = 0

        while True:
            resp = await client.request(
                method=method,
                url=url,
                headers=self._get_headers(),
                json=json_data,
            )
            self._update_ratelimits(resp.headers)

            if resp.status_code in (403, 429):
                text_lower = resp.text.lower()
                is_secondary = (
                    "secondary rate limit" in text_lower
                    or "abuse detection" in text_lower
                    or "retry-after" in resp.headers
                )
                is_push_protection = (
                    "push protection" in text_lower
                    or "secret scanning" in text_lower
                )
                if is_push_protection:
                    raise PushProtectionError(f"GitHub rejected push due to secret scanning: {resp.text}")

                if is_secondary:
                    if retries >= self.max_secondary_retries:
                        raise SecondaryRateLimitError(
                            f"GitHub secondary rate limit exceeded after {retries} retries: {resp.text}"
                        )
                    retry_after = resp.headers.get("retry-after")
                    if retry_after:
                        try:
                            sleep_dur = float(retry_after)
                        except ValueError:
                            sleep_dur = self.base_backoff_seconds * (2 ** retries)
                    else:
                        sleep_dur = self.base_backoff_seconds * (2 ** retries)

                    logger.warning(
                        "Secondary rate limit encountered on %s %s. Backing off for %.1fs (retry %d/%d)",
                        method,
                        path,
                        sleep_dur,
                        retries + 1,
                        self.max_secondary_retries,
                    )
                    await self.backoff_sleep_fn(sleep_dur)
                    retries += 1
                    continue

                if self.ratelimit_remaining == 0:
                    raise RateLimitExceededError(f"Primary rate limit exhausted: {resp.text}")

            return resp

    async def verify_private_repo(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """Guard: verify repo exists and is strictly private (G6)."""
        resp = await self._request("GET", f"/repos/{self.repo}", client)
        if resp.status_code == 404:
            raise GitHubExportError(f"Repository {self.repo} not found")
        if resp.status_code != 200:
            raise GitHubExportError(f"Failed to fetch repository metadata: {resp.status_code} {resp.text}")

        data = resp.json()
        if not data.get("private", False):
            raise PublicRepoRefusedError(
                f"Repository {self.repo} is public! ThreadVault requires private repositories for export."
            )
        return data

    async def push_batch(
        self,
        files: list[GitHubFileEntry],
        commit_message: str,
        verify_private: bool = True,
    ) -> GitHubBatchResult:
        """Execute atomic batch push using Git Data API (G1)."""
        if not files:
            return GitHubBatchResult(
                commit_sha="",
                tree_sha="",
                files_written=0,
                files_deleted=0,
            )

        async with self._get_client() as client:
            if verify_private:
                await self.verify_private_repo(client)

            restarts = 0
            for attempt in range(self.max_restarts + 1):
                # Step 1: Read head commit sha
                ref_resp = await self._request("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}", client)
                if ref_resp.status_code == 404:
                    ref_resp = await self._request("GET", f"/repos/{self.repo}/git/refs/heads/{self.branch}", client)
                if ref_resp.status_code != 200:
                    raise GitHubExportError(
                        f"Failed to read head ref for branch {self.branch}: {ref_resp.status_code} {ref_resp.text}"
                    )
                head_sha = ref_resp.json()["object"]["sha"]

                # Step 2: Read base tree sha
                commit_resp = await self._request("GET", f"/repos/{self.repo}/git/commits/{head_sha}", client)
                if commit_resp.status_code != 200:
                    raise GitHubExportError(
                        f"Failed to read commit {head_sha}: {commit_resp.status_code} {commit_resp.text}"
                    )
                base_tree_sha = commit_resp.json()["tree"]["sha"]

                # Conflict detection (G5)
                conflicts: list[str] = []
                files_to_check = [f for f in files if f.last_blob_sha is not None]
                if files_to_check:
                    tree_resp = await self._request(
                        "GET", f"/repos/{self.repo}/git/trees/{base_tree_sha}?recursive=1", client
                    )
                    if tree_resp.status_code == 200:
                        remote_tree = tree_resp.json().get("tree", [])
                        remote_blobs = {item["path"]: item["sha"] for item in remote_tree if item.get("type") == "blob"}
                        for f in files_to_check:
                            current_remote_sha = remote_blobs.get(f.path)
                            if current_remote_sha and current_remote_sha != f.last_blob_sha:
                                conflicts.append(f.path)
                                logger.warning(
                                    "Conflict detected on %s: remote blob %s != last exported %s",
                                    f.path,
                                    current_remote_sha,
                                    f.last_blob_sha,
                                )

                active_files = [f for f in files if f.path not in conflicts]
                if not active_files:
                    return GitHubBatchResult(
                        commit_sha="",
                        tree_sha="",
                        files_written=0,
                        files_deleted=0,
                        conflicts=conflicts,
                        restarts=restarts,
                    )

                # Step 3: Create tree with inline blobs (Write #1)
                tree_items = []
                written_count = 0
                deleted_count = 0
                for f in active_files:
                    if f.content is not None:
                        tree_items.append({
                            "path": f.path,
                            "mode": "100644",
                            "type": "blob",
                            "content": f.content,
                        })
                        written_count += 1
                    else:
                        tree_items.append({
                            "path": f.path,
                            "mode": "100644",
                            "type": "blob",
                            "sha": None,
                        })
                        deleted_count += 1

                tree_payload = {
                    "base_tree": base_tree_sha,
                    "tree": tree_items,
                }

                tree_resp = await self._request(
                    "POST", f"/repos/{self.repo}/git/trees", client, json_data=tree_payload
                )
                if tree_resp.status_code not in (200, 201):
                    raise GitHubExportError(
                        f"Failed to create tree: {tree_resp.status_code} {tree_resp.text}"
                    )
                tree_json = tree_resp.json()
                new_tree_sha = tree_json["sha"]
                returned_blob_shas = {
                    item["path"]: item["sha"]
                    for item in tree_json.get("tree", [])
                    if "sha" in item and item["sha"]
                }

                # Step 4: Create commit (Write #2)
                commit_payload = {
                    "message": commit_message,
                    "tree": new_tree_sha,
                    "parents": [head_sha],
                }
                new_commit_resp = await self._request(
                    "POST", f"/repos/{self.repo}/git/commits", client, json_data=commit_payload
                )
                if new_commit_resp.status_code not in (200, 201):
                    raise GitHubExportError(
                        f"Failed to create commit: {new_commit_resp.status_code} {new_commit_resp.text}"
                    )
                new_commit_sha = new_commit_resp.json()["sha"]

                # Step 5: Update ref with force=false (Write #3)
                ref_payload = {
                    "sha": new_commit_sha,
                    "force": False,
                }
                patch_resp = await self._request(
                    "PATCH", f"/repos/{self.repo}/git/refs/heads/{self.branch}", client, json_data=ref_payload
                )

                if patch_resp.status_code == 200:
                    return GitHubBatchResult(
                        commit_sha=new_commit_sha,
                        tree_sha=new_tree_sha,
                        files_written=written_count,
                        files_deleted=deleted_count,
                        conflicts=conflicts,
                        restarts=restarts,
                        blob_shas=returned_blob_shas,
                    )


                if patch_resp.status_code == 422:
                    # Non-fast-forward / ref moved concurrently
                    logger.warning(
                        "Ref update returned 422 (fast-forward race). Restarting batch attempt %d/%d",
                        attempt + 1,
                        self.max_restarts,
                    )
                    restarts += 1
                    if attempt < self.max_restarts:
                        continue
                    raise MovedRefMaxRestartsError(
                        f"Reference update rejected (422) and exceeded max restarts ({self.max_restarts})"
                    )

                raise GitHubExportError(
                    f"Failed to update ref {self.branch}: {patch_resp.status_code} {patch_resp.text}"
                )

            raise MovedRefMaxRestartsError("Max restarts exceeded")
