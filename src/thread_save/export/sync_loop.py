"""Background sync pushing FileStore markdown vaults to GitHub (§2).

P1-2: the loop used to rebuild and push the whole vault every 60 s, and the Git
Data API makes a commit even when nothing changed, so the archive repo got an
identical commit every minute the server was awake. Now GitHub is updated only
after a save:

1. FileStore sets `export_signal()` after every save that wrote a page (a
   vault_log_turn call that stored something).
2. The sync task sleeps on that signal; with no saves it does nothing at all.
3. After a signal it waits until saves have been quiet for
   THREADVAULT_SYNC_SETTLE_SECONDS (30), so both calls of a reply and quick
   follow-ups go into one commit, but never longer than
   THREADVAULT_SYNC_MAX_WAIT_SECONDS (300) during a long chat.
4. push_batch(skip_unchanged=True) uploads only files whose content differs from
   GitHub, and makes no commit when nothing differs.

At startup one check pushes anything GitHub is missing (e.g. a save just before
the server slept); when GitHub is up to date it makes no commit. A failed push
is tried again after THREADVAULT_SYNC_RETRY_SECONDS (300), and again only if
content still differs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from thread_save.export.github import (
    GitHubBatchResult,
    GitHubDataApiTarget,
    GitHubExportError,
    GitHubFileEntry,
    PublicRepoRefusedError,
)
from thread_save.export.layout import extract_filestore_export_tree

logger = logging.getLogger("thread_save.export.sync_loop")

# Outcomes of one sync attempt
PUSHED, UNCHANGED, EMPTY, FAILED = "pushed", "unchanged", "empty", "failed"


def normalize_repo_slug(repo: str) -> str:
    """Normalize full GitHub URLs or git remotes to owner/repo format."""
    r = repo.strip()
    if r.endswith(".git"):
        r = r[:-4]
    if "github.com/" in r:
        r = r.split("github.com/", 1)[1]
    return r.strip("/")


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _export_tree(vault_root: Path) -> dict[str, str]:
    accounts = [
        d.name for d in vault_root.iterdir()
        if d.is_dir() and not d.name.startswith(('.', '_'))
    ]
    if not accounts:
        accounts = ["default"]
    tree: dict[str, str] = {}
    multi_account = len(accounts) > 1
    for acc in accounts:
        tree.update(extract_filestore_export_tree(vault_root, account=acc, account_dir=multi_account))
    return tree


def _commit_message(saved: list[tuple[str, str]]) -> str:
    titles = [t for _, t in saved if t]
    if len(saved) == 1 and titles:
        return f"vault: update '{titles[0][:60]}'"
    if saved:
        return f"vault: update {len(saved)} chats"
    return "vault: sync missing pages"


async def _sync(
    vault_root: Path,
    repo: str,
    token: str,
    branch: str = "main",
    commit_message: str = "vault: sync",
) -> tuple[str, Optional[GitHubBatchResult]]:
    """One sync attempt: (outcome, push result)."""
    clean_repo = normalize_repo_slug(repo)
    if not clean_repo or "/" not in clean_repo:
        logger.warning("Invalid GitHub repository format: %r (expected 'owner/repo')", repo)
        return FAILED, None
    if not vault_root.exists():
        return EMPTY, None

    tree = _export_tree(vault_root)
    # Only push when there are real threads (not just README / empty index)
    has_threads = any(
        k.endswith(".md") and not k.startswith("README") and not k.startswith("index/")
        for k in tree
    )
    if not has_threads:
        logger.debug("No conversation threads found in %s to sync to GitHub", vault_root)
        return EMPTY, None

    entries = [GitHubFileEntry(path=p, content=c) for p, c in tree.items()]
    target = GitHubDataApiTarget(repo=clean_repo, token=token, branch=branch)
    try:
        allow_public = os.environ.get("THREADVAULT_ALLOW_PUBLIC_REPO", "true").strip().lower() in ("true", "1", "yes")
        result = await target.push_batch(
            files=entries,
            commit_message=commit_message,
            verify_private=(not allow_public),
            skip_unchanged=True,
        )
    except PublicRepoRefusedError as e:
        logger.error("GitHub export refused: %s", e)
        return FAILED, None
    except GitHubExportError as e:
        logger.error("GitHub export error for %s: %s", clean_repo, e)
        return FAILED, None
    except Exception as e:
        logger.error("Unexpected error syncing threads to GitHub: %s", e, exc_info=True)
        return FAILED, None

    if result.commit_sha:
        logger.info(
            "Exported %d changed file(s) to %s (commit %s)",
            result.files_written, clean_repo, result.commit_sha[:8],
        )
        return PUSHED, result
    if result.conflicts:
        logger.warning("GitHub export skipped: %d conflicting file(s)", len(result.conflicts))
        return FAILED, result
    logger.info("GitHub already has this vault content; no commit made")
    return UNCHANGED, result


async def run_sync_once(
    vault_root: Path,
    repo: str,
    token: str,
    branch: str = "main",
) -> Optional[GitHubBatchResult]:
    """Push the pages GitHub does not have yet; no commit when it has them all."""
    _, result = await _sync(vault_root, repo, token, branch)
    return result


def _token(token: Optional[str]) -> Optional[str]:
    return token or os.environ.get("GITHUB_TOKEN") or os.environ.get("THREADVAULT_GH_TOKEN")


async def _wait_until_quiet(signal: asyncio.Event, settle: float, max_wait: float) -> None:
    """Return once no save arrived for `settle` seconds, or after `max_wait`."""
    started = time.monotonic()
    while True:
        signal.clear()
        remaining = max_wait - (time.monotonic() - started)
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(signal.wait(), timeout=min(settle, remaining))
        except asyncio.TimeoutError:
            return


async def start_github_sync_loop(
    vault_root: Path,
    repo: str,
    token: Optional[str] = None,
    interval_seconds: int = 60,
    branch: str = "main",
    store: Any = None,
) -> None:
    """Push the vault to GitHub after saves, one commit per reply or burst (P1-2).

    `store` is the FileStore whose saves trigger a push. `interval_seconds` is
    kept for callers and no longer used: there is no timer.
    """
    clean_repo = normalize_repo_slug(repo)
    if store is None or not hasattr(store, "export_signal"):
        logger.error("GitHub sync needs the FileStore to know when chats are saved; sync disabled")
        return
    settle = _env_seconds("THREADVAULT_SYNC_SETTLE_SECONDS", 30)
    max_wait = max(settle, _env_seconds("THREADVAULT_SYNC_MAX_WAIT_SECONDS", 300))
    retry_after = _env_seconds("THREADVAULT_SYNC_RETRY_SECONDS", 300)
    signal = store.export_signal()
    logger.info(
        "Starting GitHub sync for %s: push after saves (settle %ss, max wait %ss), no timer",
        clean_repo, settle, max_wait,
    )

    async def push(message: str) -> None:
        active_token = _token(token)
        if not active_token:
            logger.warning(
                "[GitHub Sync] No GITHUB_TOKEN or THREADVAULT_GH_TOKEN set. "
                "Set GITHUB_TOKEN in your environment to enable automatic export to %s",
                clean_repo,
            )
            return
        outcome, _ = await _sync(vault_root, clean_repo, active_token, branch, message)
        if outcome == FAILED and retry_after:
            # One more try later; it commits only if content still differs
            asyncio.get_running_loop().call_later(retry_after, signal.set)

    try:
        # Anything saved before the last sleep/restart that GitHub does not have yet
        await push("vault: sync missing pages")
        while True:
            await signal.wait()
            await _wait_until_quiet(signal, settle, max_wait)
            await push(_commit_message(store.take_export_threads()))
    except asyncio.CancelledError:
        logger.info("GitHub sync loop cancelled")
        raise
