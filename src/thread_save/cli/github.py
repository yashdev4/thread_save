"""GitHub Archive CLI commands (§1 G6, Milestone GH5).

Provides commands for managing remote GitHub archive repositories:
    python -m thread_save.cli.github squash [--repo <owner/repo>] [--token <token>] [--branch <branch>]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from thread_save.export.github import GitHubDataApiTarget, GitHubExportError


async def run_squash(
    repo: str,
    token: str,
    branch: str = "main",
    message: str = "vault: squashed archive history",
    base_url: str = "https://api.github.com",
    target: Optional[GitHubDataApiTarget] = None,
) -> str:
    """Execute history squash on the target repository."""
    if not repo or not token:
        raise ValueError("Both repo and token are required for squash operation.")

    gh_target = target or GitHubDataApiTarget(
        repo=repo,
        token=token,
        branch=branch,
        base_url=base_url,
    )

    print(f"Target repository: {repo} (branch: {branch})")
    print("Initiating history squash to orphan root commit...")

    squashed_sha = await gh_target.squash_history(message=message)

    print("=" * 70)
    print("GitHub Archive History Squashed Successfully")
    print("=" * 70)
    print(f"  New Root Commit SHA: {squashed_sha}")
    print(f"  Branch Reference:    refs/heads/{branch}")
    print("\nHistory Semantics Notice (§G6):")
    print("  - The branch has been rewritten to a single parent-less commit containing the current tree.")
    print("  - All previous commits are now unreferenced on GitHub and eligible for eventual cleanup.")
    print("  - Note: Existing local git clones, forks, or cached web views may retain prior versions.")
    print("=" * 70)
    return squashed_sha


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thread_save.cli.github",
        description="ThreadVault GitHub Archive CLI tools",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # squash command
    squash_parser = subparsers.add_parser(
        "squash",
        help="Rewrite archive branch to a single orphan commit of the current tree",
    )
    squash_parser.add_argument(
        "--repo",
        default=os.environ.get("THREADVAULT_GH_REPO") or os.environ.get("GITHUB_REPO", ""),
        help="GitHub repository in owner/repo format (or env THREADVAULT_GH_REPO)",
    )
    squash_parser.add_argument(
        "--token",
        default=os.environ.get("THREADVAULT_GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub personal access token (or env THREADVAULT_GH_TOKEN)",
    )
    squash_parser.add_argument(
        "--branch",
        default="main",
        help="Target branch (default: main)",
    )
    squash_parser.add_argument(
        "--message",
        default="vault: squashed archive history",
        help="Commit message for squashed commit",
    )
    squash_parser.add_argument(
        "--base-url",
        default="https://api.github.com",
        help="GitHub API base URL (default: https://api.github.com)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "squash":
        if not args.repo:
            print("Error: repository is required (--repo or THREADVAULT_GH_REPO env var)", file=sys.stderr)
            return 1
        if not args.token:
            print("Error: token is required (--token or THREADVAULT_GH_TOKEN env var)", file=sys.stderr)
            return 1

        try:
            asyncio.run(
                run_squash(
                    repo=args.repo,
                    token=args.token,
                    branch=args.branch,
                    message=args.message,
                    base_url=args.base_url,
                )
            )
            return 0
        except Exception as e:
            print(f"Error executing squash: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
