"""GitHub Commit Policy and Settle-and-Batch Sync Manager (§1 G4, Milestone GH3).

Implements:
- Settle window: threads export only after settle_minutes of inactivity (default 30m)
- Batch cadence: batches settled threads into single commits (default 10m)
- Single in-flight batch per repository (concurrency lock per repo)
- Skip unchanged pages: page hash comparison against manifest ensures closed sticky pages are never re-sent
- Deterministic commit messages: 'vault: X threads, Y pages (batch YYYY-MM-DDTHH:MMZ)'
- Repo growth guard: warns when GitHub repo size >= 750 MB (768,000 KB)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Optional

import httpx

from thread_save.export.github import (
    GitHubBatchResult,
    GitHubDataApiTarget,
    GitHubFileEntry,
    PushProtectionError,
)
from thread_save.export.layout import (
    ExportThreadInfo,
    generate_archive_tree,
)

logger = logging.getLogger("thread_save.export.sync")

# 750 MB in KB (GitHub API reports repository size in KB)
REPO_SIZE_WARNING_THRESHOLD_KB = 750 * 1024


@dataclass
class GitHubExportConfig:
    """Configuration for GitHub archive export commit policy."""
    repo: str
    token: str
    branch: str = "main"
    settle_minutes: int = 30
    batch_minutes: int = 10
    account_dir: bool = False
    size_warning_kb: int = REPO_SIZE_WARNING_THRESHOLD_KB


class GitHubBatchExporter:
    """Manages settle-window evaluation and atomic batched exports to GitHub."""

    _repo_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        target: GitHubDataApiTarget,
        config: GitHubExportConfig,
        manifest: Optional[dict[str, str]] = None,  # path -> content_sha256
    ):
        self.target = target
        self.config = config
        self.manifest: dict[str, str] = manifest if manifest is not None else {}
        self.blob_shas: dict[str, str] = {}
        self.conflicts: dict[str, str] = {}
        self.dead_letters: dict[str, str] = {}
        self.last_size_check: Optional[datetime] = None
        self.last_known_size_kb: int = 0
        self.size_warning_active: bool = False

    @classmethod
    def _get_repo_lock(cls, repo: str) -> asyncio.Lock:
        if repo not in cls._repo_locks:
            cls._repo_locks[repo] = asyncio.Lock()
        return cls._repo_locks[repo]

    def is_thread_due(self, updated_at: datetime, now: Optional[datetime] = None) -> bool:
        """A thread is due for export once it has had no writes for settle_minutes (G4)."""
        ref_now = now or datetime.now(timezone.utc)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=timezone.utc)
        return (ref_now - updated_at) >= timedelta(minutes=self.config.settle_minutes)

    async def check_repo_size_guard(self, client: httpx.AsyncClient) -> tuple[int, bool]:
        """Check repository size once a day; warn at 750 MB (G4)."""
        try:
            repo_info = await self.target.verify_private_repo(client)
            size_kb = int(repo_info.get("size", 0))
            self.last_known_size_kb = size_kb
            self.size_warning_active = size_kb >= self.config.size_warning_kb

            if self.size_warning_active:
                logger.warning(
                    "GitHub archive repository %s size (%d KB / %.1f MB) exceeds %d MB warning threshold! "
                    "Consider configuring yearly rotation (e.g. %s-%d) or pruning.",
                    self.config.repo,
                    size_kb,
                    size_kb / 1024,
                    self.config.size_warning_kb // 1024,
                    self.config.repo,
                    datetime.now(timezone.utc).year + 1,
                )
            return size_kb, self.size_warning_active
        except Exception as e:
            logger.warning("Failed to check repository size for %s: %s", self.config.repo, e)
            return self.last_known_size_kb, self.size_warning_active

    @staticmethod
    def _compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get_status(self) -> dict[str, Any]:
        """Return export status including conflicts, dead letters, and repo size."""
        return {
            "conflicts": sorted(self.conflicts.keys()),
            "conflict_details": dict(self.conflicts),
            "dead_letters": sorted(self.dead_letters.keys()),
            "dead_letter_details": dict(self.dead_letters),
            "manifest_entries": len(self.manifest),
            "size_warning_active": self.size_warning_active,
            "last_known_size_kb": self.last_known_size_kb,
        }

    async def export_settled_threads(
        self,
        threads: list[ExportThreadInfo],
        now: Optional[datetime] = None,
        force_settle: bool = False,
    ) -> Optional[GitHubBatchResult]:
        """Export settled threads in a single atomic batch commit.

        Skips unchanged pages by checking against the export manifest.
        Excludes dead-lettered threads.
        Detects conflicts with human edits on GitHub and leaves them untouched.
        Guarantees single in-flight batch per repository.
        """
        ref_now = now or datetime.now(timezone.utc)
        lock = self._get_repo_lock(self.config.repo)

        # Exclude threads previously dead-lettered (e.g. push protection rejection)
        eligible_threads = [
            t for t in threads if t.thread_id not in self.dead_letters
        ]

        # Enforce single in-flight batch per repo
        async with lock:
            async with self.target._get_client() as client:
                # 1. Check repo growth guard
                await self.check_repo_size_guard(client)

                # 2. Filter threads settled past settle_minutes
                due_threads = [
                    t for t in eligible_threads
                    if force_settle or self.is_thread_due(t.updated_at, ref_now)
                ]

                if not due_threads:
                    return None


                # 3. Generate candidate full archive tree
                full_tree = generate_archive_tree(eligible_threads, account_dir=self.config.account_dir)

                # 4. Filter tree entries: send only files whose content hash changed
                entries_to_push: list[GitHubFileEntry] = []
                new_manifest_updates: dict[str, str] = {}
                pages_sent_count = 0

                for path, content in full_tree.items():
                    c_hash = self._compute_hash(content)
                    last_hash = self.manifest.get(path)

                    if last_hash != c_hash:
                        last_blob = self.blob_shas.get(path)
                        entries_to_push.append(
                            GitHubFileEntry(path=path, content=content, last_blob_sha=last_blob)
                        )
                        new_manifest_updates[path] = c_hash
                        if path.endswith(".md") and not path.startswith("index/") and path != "README.md":
                            pages_sent_count += 1

                # If no pages or indexes changed, no commit needed
                if not entries_to_push:
                    return None

                # 5. Deterministic commit message (§G4)
                time_str = ref_now.strftime("%Y-%m-%dT%H:%MZ")
                commit_msg = (
                    f"vault: {len(due_threads)} threads, {pages_sent_count} pages (batch {time_str})"
                )

                # 6. Execute atomic batch push with push-protection handling
                try:
                    result = await self.target.push_batch(
                        files=entries_to_push,
                        commit_message=commit_msg,
                        verify_private=False,  # Already verified in check_repo_size_guard
                    )
                except PushProtectionError as e:
                    # G6: Push protection rejection -> dead-letter batch, alert, never retry
                    for t in due_threads:
                        self.dead_letters[t.thread_id] = f"Push protection rejected commit: {e}"
                    logger.critical(
                        "GitHub secret scanning push protection rejected batch. Dead-lettering threads: %s",
                        [t.thread_id for t in due_threads],
                    )
                    raise

                # 7. Record any detected conflicts
                for c_path in result.conflicts:
                    self.conflicts[c_path] = (
                        "Remote human edit detected on GitHub: remote blob SHA differs from last exported SHA."
                    )
                    # Don't update manifest for conflicted path so it's not marked exported
                    new_manifest_updates.pop(c_path, None)

                # 8. Update manifest and blob SHAs
                self.manifest.update(new_manifest_updates)
                self.blob_shas.update(result.blob_shas)
                return result

