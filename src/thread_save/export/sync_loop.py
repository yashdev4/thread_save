"""Background periodic sync loop pushing FileStore markdown vaults to GitHub (§2)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from thread_save.export.github import (
    GitHubBatchResult,
    GitHubDataApiTarget,
    GitHubExportError,
    GitHubFileEntry,
    PublicRepoRefusedError,
)
from thread_save.export.layout import extract_filestore_export_tree

logger = logging.getLogger("thread_save.export.sync_loop")


def normalize_repo_slug(repo: str) -> str:
    """Normalize full GitHub URLs or git remotes to owner/repo format."""
    r = repo.strip()
    if r.endswith(".git"):
        r = r[:-4]
    if "github.com/" in r:
        r = r.split("github.com/", 1)[1]
    return r.strip("/")


async def run_sync_once(
    vault_root: Path,
    repo: str,
    token: str,
    branch: str = "main",
) -> Optional[GitHubBatchResult]:
    """Extract local FileStore archive tree and push atomic batch to GitHub."""
    clean_repo = normalize_repo_slug(repo)
    if not clean_repo or "/" not in clean_repo:
        logger.warning("Invalid GitHub repository format: %r (expected 'owner/repo')", repo)
        return None

    # 1. Extract markdown vault tree across accounts
    if not vault_root.exists():
        return None

    accounts = [
        d.name for d in vault_root.iterdir()
        if d.is_dir() and not d.name.startswith(('.', '_'))
    ]
    if not accounts:
        accounts = ["default"]

    tree: dict[str, str] = {}
    multi_account = len(accounts) > 1
    for acc in accounts:
        acc_tree = extract_filestore_export_tree(vault_root, account=acc, account_dir=multi_account)
        tree.update(acc_tree)

    # Check if we have actual threads (tree has more than just README / empty index)
    has_threads = any(
        k.endswith(".md") and not k.startswith("README") and not k.startswith("index/")
        for k in tree.keys()
    )
    if not has_threads:
        logger.debug("No conversation threads found in %s to sync to GitHub", vault_root)
        return None

    entries = [GitHubFileEntry(path=p, content=c) for p, c in tree.items()]
    target = GitHubDataApiTarget(repo=clean_repo, token=token, branch=branch)

    try:
        allow_public = os.environ.get("THREADVAULT_ALLOW_PUBLIC_REPO", "true").strip().lower() in ("true", "1", "yes")
        result = await target.push_batch(
            files=entries,
            commit_message=f"vault: automated sync ({len(entries)} files)",
            verify_private=(not allow_public),
        )
        logger.info(
            "Successfully exported %d files to %s (commit %s)",
            result.files_written,
            clean_repo,
            result.commit_sha[:8] if result.commit_sha else "none",
        )
        return result
    except PublicRepoRefusedError as e:
        logger.error("GitHub export refused: %s", e)
        return None
    except GitHubExportError as e:
        logger.error("GitHub export error for %s: %s", clean_repo, e)
        return None
    except Exception as e:
        logger.error("Unexpected error syncing threads to GitHub: %s", e, exc_info=True)
        return None


async def start_github_sync_loop(
    vault_root: Path,
    repo: str,
    token: Optional[str] = None,
    interval_seconds: int = 60,
    branch: str = "main",
) -> None:
    """Run continuous background loop syncing FileStore threads to GitHub repository."""
    clean_repo = normalize_repo_slug(repo)
    logger.info(
        "Starting background GitHub sync loop for %s (interval: %ds)",
        clean_repo,
        interval_seconds,
    )

    warned_token = False
    while True:
        try:
            active_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("THREADVAULT_GH_TOKEN")
            if not active_token:
                if not warned_token:
                    logger.warning(
                        "[GitHub Sync] No GITHUB_TOKEN or THREADVAULT_GH_TOKEN set. "
                        "Set GITHUB_TOKEN in your environment to enable automatic export to %s",
                        clean_repo,
                    )
                    warned_token = True
            else:
                warned_token = False
                await run_sync_once(
                    vault_root=vault_root,
                    repo=clean_repo,
                    token=active_token,
                    branch=branch,
                )
        except asyncio.CancelledError:
            logger.info("GitHub sync loop cancelled")
            break
        except Exception as e:
            logger.error("Exception in GitHub sync loop: %s", e)

        await asyncio.sleep(interval_seconds)
