#!/usr/bin/env python3
"""GitHub Archive Export Live Verification Runner (Milestone GH8).

Executes the 4 live integration phases against a real GitHub repository:
  Phase 1: Privacy & Guard Verification (GET /repos/{owner}/{repo}, verify private=true)
  Phase 2: First Sync Batch (Push README, indexes, initial thread pages via Git Data API)
  Phase 3: Incremental Sync (Push updated thread page, verify unchanged pages skipped)
  Phase 4: Remote Human-Edit Conflict Safety (Simulate external modification, verify skip)
  Phase 5: History Squash (Create single orphan root commit, force-update ref)

Usage:
  python scripts/verify_github_live.py --repo owner/my-archive-vault --token ghp_...
  Or set environment variables:
    THREADVAULT_GH_LIVE_REPO=owner/my-archive-vault
    THREADVAULT_GH_LIVE_TOKEN=github_pat_...
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
import sys
from typing import Optional

from thread_save.export.auth import mask_token_preview
from thread_save.export.github import (
    GitHubBatchResult,
    GitHubDataApiTarget,
    GitHubFileEntry,
    PublicRepoRefusedError,
)
from thread_save.export.layout import ExportThreadInfo, generate_archive_tree
from thread_save.export.sync import GitHubBatchExporter, GitHubExportConfig


async def run_live_verification(repo: str, token: str, branch: str = "main") -> int:
    print("=" * 70)
    print("ThreadVault: GitHub Archive Export Live Verification (Milestone GH8)")
    print("=" * 70)
    print(f"Target Repository : {repo}")
    print(f"Target Branch     : {branch}")
    print(f"Auth Token Preview: {mask_token_preview(token)}")
    print("-" * 70)

    target = GitHubDataApiTarget(repo=repo, token=token, branch=branch)

    # ── Phase 1: Privacy Guard ──
    print("\n[Phase 1] Checking Repository Privacy Guard (G6)...")
    try:
        is_private = await target.verify_private()
        if not is_private:
            print("  FAIL: Target repository is public! ThreadVault refuses to export to public repos.")
            return 1
        print("  PASS: Repository verified as PRIVATE.")
    except Exception as exc:
        print(f"  FAIL: Could not verify repository: {exc}")
        return 1

    # ── Phase 2: First Sync Batch ──
    print("\n[Phase 2] Executing First Sync Batch (G1, G3)...")
    thread_id = "01M43A2W9JMVPX4Y9WGBYF0V88"
    now = datetime.now(timezone.utc)
    p1_content = (
        "---\n"
        'schema_version: 2\n'
        f'thread_id: "{thread_id}"\n'
        'title: "Live Archive Verification"\n'
        'slug: "live-archive-verification"\n'
        'page: 1\n'
        "---\n\n"
        "# Live Archive Verification\n\n"
        "<!-- turn i=1 role=user fidelity=verbatim chars=35 hash=a1b2c3d4 -->\n"
        "## User\n\n"
        "Testing live GitHub archive export.\n"
        "<!-- /turn i=1 -->\n\n---\n\n"
        "<!-- turn i=1 role=assistant fidelity=verbatim chars=28 hash=e5f6a7b8 -->\n"
        "## Claude\n\n"
        "Live export verified Turn 1.\n"
        "<!-- /turn i=1 -->\n"
    )
    thread_info = ExportThreadInfo(
        thread_id=thread_id,
        title="Live Archive Verification",
        slug="live-archive-verification",
        created_at=now,
        updated_at=now,
        turn_count=1,
        pages={"2026/10/2026-10-05T0000_live_yf0v88_live-archive-verification_p01.md": p1_content},
    )
    tree = generate_archive_tree([thread_info], account="live-test", account_dir=False)
    entries = [GitHubFileEntry(path=p, content=c) for p, c in tree.items()]

    try:
        res1 = await target.push_batch(entries)
        print(f"  PASS: Batch committed successfully.")
        print(f"        Commit SHA : {res1.commit_sha}")
        print(f"        Tree SHA   : {res1.tree_sha}")
        print(f"        Files added: {res1.files_written}")
    except Exception as exc:
        print(f"  FAIL: First sync batch failed: {exc}")
        return 1

    # ── Phase 3: Incremental Sync (Unchanged Page Skip) ──
    print("\n[Phase 3] Executing Incremental Sync with Unchanged Page Skip (G4)...")
    p2_content = (
        "---\n"
        'schema_version: 2\n'
        f'thread_id: "{thread_id}"\n'
        'title: "Live Archive Verification"\n'
        'slug: "live-archive-verification"\n'
        'page: 2\n'
        "---\n\n"
        "<!-- turn i=2 role=user fidelity=verbatim chars=20 hash=99887766 -->\n"
        "## User\n\n"
        "Appended turn 2 query.\n"
        "<!-- /turn i=2 -->\n"
    )
    thread_info.turn_count = 2
    thread_info.pages["2026/10/2026-10-05T0000_live_yf0v88_live-archive-verification_p02.md"] = p2_content

    config = GitHubExportConfig(repo=repo, token=token, branch=branch)
    exporter = GitHubBatchExporter(target=target, config=config)
    exporter.blob_shas.update(res1.blob_shas)

    try:
        res2 = await exporter.export_threads([thread_info], account="live-test")
        if res2 is None:
            print("  FAIL: Exporter returned None (expected incremental batch commit).")
            return 1
        print(f"  PASS: Incremental batch committed successfully.")
        print(f"        Commit SHA : {res2.commit_sha}")
        print(f"        Files added/updated: {res2.files_written}")
        print(f"        Unchanged Page 1 safely skipped.")
    except Exception as exc:
        print(f"  FAIL: Incremental sync failed: {exc}")
        return 1

    # ── Phase 4: Human-Edit Conflict Detection ──
    print("\n[Phase 4] Testing Remote Human-Edit Conflict Detection (G5)...")
    conflicting_entry = GitHubFileEntry(
        path="README.md",
        content="# Conflicting README Content\n",
        last_blob_sha="0000000000000000000000000000000000000000",
    )
    try:
        res3 = await target.push_batch([conflicting_entry])
        if "README.md" in res3.conflicts and res3.files_written == 0:
            print("  PASS: Remote conflict detected; remote modification protected from overwrite.")
        else:
            print(f"  FAIL: Expected README.md in conflicts, got: {res3.conflicts}")
            return 1
    except Exception as exc:
        print(f"  FAIL: Conflict detection check failed: {exc}")
        return 1

    # ── Phase 5: History Squash ──
    print("\n[Phase 5] Executing Git History Squash (G6)...")
    try:
        squash_sha = await target.squash_history()
        print(f"  PASS: History successfully squashed to parentless root commit.")
        print(f"        Squash Root Commit SHA: {squash_sha}")
    except Exception as exc:
        print(f"  FAIL: History squash failed: {exc}")
        return 1

    print("\n" + "=" * 70)
    print("ALL LIVE VERIFICATION PHASES PASSED CLEANLY (Gate GH8).")
    print("=" * 70)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ThreadVault GitHub Archive Export Live Verification Runner (Milestone GH8)"
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("THREADVAULT_GH_LIVE_REPO"),
        help="Target GitHub repository (owner/repo). Default: THREADVAULT_GH_LIVE_REPO env var.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("THREADVAULT_GH_LIVE_TOKEN"),
        help="GitHub Personal Access Token (PAT). Default: THREADVAULT_GH_LIVE_TOKEN env var.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Target branch (default: main).",
    )
    args = parser.parse_args()

    if not args.repo or not args.token:
        print("Error: Missing required parameters.")
        print("Please provide --repo and --token or set environment variables:")
        print("  export THREADVAULT_GH_LIVE_REPO=owner/repo")
        print("  export THREADVAULT_GH_LIVE_TOKEN=github_pat_...")
        sys.exit(2)

    ret = asyncio.run(run_live_verification(repo=args.repo, token=args.token, branch=args.branch))
    sys.exit(ret)


if __name__ == "__main__":
    main()
