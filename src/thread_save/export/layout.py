"""Repository Layout and Deterministic Index Generation (§1 G3, Milestone GH2).

Implements:
- Standard Plan v2 GitHub Archive repo structure:
    README.md
    index/threads.json
    index/YYYY-MM.md
    {YYYY}/{MM}/{filename}_p{NN}.md
- Deterministic README.md with automation notices, conflict policies, and deletion semantics
- Deterministic index/threads.json machine index
- Deterministic monthly indexes (index/YYYY-MM.md) with relative markdown links
- Tree extractor for FileStore
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional

from thread_save.storage.formatter import parse_page
from thread_save.storage.path_resolver import build_filename, validate_slug

README_TEMPLATE = """# ThreadVault Archive

> **Note:** This repository is an automated, private archive of conversations managed by [ThreadVault](https://github.com/eoxs/threadvault).

## Automated Maintenance & Guidelines

- **Do Not Edit Directly:** This repository is maintained by an automated export process. If files are edited directly on GitHub, ThreadVault detects the modification as a conflict and will skip updating those files to preserve your manual edits.
- **Privacy Notice:** This repository is configured to be strictly private. ThreadVault's private-repo guard refuses export if the repository is ever made public.
- **Deletion Semantics:** When a thread is deleted in ThreadVault, its files are deleted from the repository in the subsequent export commit. Prior commits in git history continue to contain the archived text unless explicitly purged using the squash command:
  ```bash
  python -m thread_save.cli.github squash
  ```

## Repository Structure

```
.
├── README.md              # Repository documentation and maintenance rules
├── index/
│   ├── threads.json       # Machine index mapping thread_id -> metadata and page paths
│   └── YYYY-MM.md         # Human-readable monthly indexes with relative links
└── YYYY/
    └── MM/
        └── {timestamp}_{account}_{short}_{slug}_p{NN}.md  # Canonical conversation pages
```
"""


@dataclass
class ExportThreadInfo:
    """Represents a thread ready for archive layout packaging."""
    thread_id: str
    title: str
    slug: str
    account: str
    created_at: datetime
    updated_at: datetime
    turns: int
    pages: list[str] = field(default_factory=list)  # repo relative paths
    page_contents: dict[str, str] = field(default_factory=dict)  # repo relative path -> content


def generate_readme() -> str:
    """Generate deterministic README.md."""
    return README_TEMPLATE


def generate_threads_json(threads: list[ExportThreadInfo]) -> str:
    """Generate deterministic machine-readable index/threads.json."""
    data: dict[str, Any] = {}
    # Sort deterministically by thread_id
    sorted_threads = sorted(threads, key=lambda t: t.thread_id)

    for t in sorted_threads:
        created_str = (
            t.created_at.isoformat()
            if isinstance(t.created_at, datetime)
            else str(t.created_at)
        )
        updated_str = (
            t.updated_at.isoformat()
            if isinstance(t.updated_at, datetime)
            else str(t.updated_at)
        )
        data[t.thread_id] = {
            "thread_id": t.thread_id,
            "title": t.title,
            "slug": t.slug,
            "account": t.account,
            "created_at": created_str,
            "updated_at": updated_str,
            "turns": t.turns,
            "pages": sorted(t.pages),
        }

    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def generate_monthly_index(
    year: int,
    month: int,
    threads: list[ExportThreadInfo],
    account_dir: bool = False,
) -> str:
    """Generate deterministic index/YYYY-MM.md with relative markdown links."""
    lines: list[str] = [
        f"# Threads — {year:04d}-{month:02d}",
        "",
    ]

    # Sort deterministically by created_at, then thread_id
    sorted_threads = sorted(
        threads,
        key=lambda t: (
            t.created_at.isoformat()
            if isinstance(t.created_at, datetime)
            else str(t.created_at),
            t.thread_id,
        ),
    )

    for t in sorted_threads:
        updated_str = (
            t.updated_at.isoformat()
            if isinstance(t.updated_at, datetime)
            else str(t.updated_at)
        )
        sorted_pages = sorted(t.pages)
        if not sorted_pages:
            continue

        first_page = sorted_pages[0]
        # From index/ directory, relative path to year/month is ../{page_path}
        primary_link = f"../{first_page}"

        lines.append(f"## [{t.title}]({primary_link})")
        lines.append(f"- **Thread ID:** `{t.thread_id}`")
        lines.append(f"- **Updated:** {updated_str}")
        lines.append(f"- **Turns:** {t.turns}")

        if len(sorted_pages) == 1:
            lines.append(f"- **Pages:** 1")
        else:
            page_links = [
                f"[Page {i+1}](../{p})" for i, p in enumerate(sorted_pages)
            ]
            lines.append(f"- **Pages:** {', '.join(page_links)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def generate_archive_tree(
    threads: list[ExportThreadInfo],
    account_dir: bool = False,
) -> dict[str, str]:
    """Generate the full dictionary of repo paths -> content for export."""
    tree: dict[str, str] = {}

    # 1. README.md
    tree["README.md"] = generate_readme()

    # 2. Machine index
    tree["index/threads.json"] = generate_threads_json(threads)

    # 3. Monthly indexes
    months_map: dict[tuple[int, int], list[ExportThreadInfo]] = {}
    for t in threads:
        dt = t.created_at if isinstance(t.created_at, datetime) else datetime.fromisoformat(str(t.created_at))
        months_map.setdefault((dt.year, dt.month), []).append(t)

    for (year, month), m_threads in sorted(months_map.items()):
        path = f"index/{year:04d}-{month:02d}.md"
        tree[path] = generate_monthly_index(year, month, m_threads, account_dir=account_dir)

    # 4. Page contents
    for t in threads:
        for page_path, content in t.page_contents.items():
            tree[page_path] = content

    return tree


def build_repo_path(
    dt: datetime,
    account: str,
    thread_id_short: str,
    slug: str,
    page: int,
    account_dir: bool = False,
) -> str:
    """Build canonical relative repository path for a thread page."""
    year = f"{dt.year:04d}"
    month = f"{dt.month:02d}"
    filename = build_filename(dt, account, thread_id_short, slug, page)
    if account_dir:
        return f"{account}/{year}/{month}/{filename}"
    return f"{year}/{month}/{filename}"


def extract_filestore_export_tree(
    vault_root: Path,
    account: str,
    account_dir: bool = False,
) -> dict[str, str]:
    """Extract full deterministic archive tree from a FileStore vault on disk."""
    account_root = vault_root / account
    if not account_root.exists():
        return generate_archive_tree([], account_dir=account_dir)

    # Find all *_p[0-9][0-9].md files
    page_files = sorted(account_root.rglob("*_p[0-9][0-9].md"))
    threads_map: dict[str, ExportThreadInfo] = {}

    for pf in page_files:
        content = pf.read_text(encoding="utf-8")
        meta, turns = parse_page(content)

        created_dt = (
            meta.created
            if isinstance(meta.created, datetime)
            else datetime.fromisoformat(str(meta.created))
        )
        updated_dt = (
            meta.updated
            if isinstance(meta.updated, datetime)
            else datetime.fromisoformat(str(meta.updated))
        )

        repo_path = build_repo_path(
            dt=created_dt,
            account=account,
            thread_id_short=meta.thread_id[-6:],
            slug=meta.slug,
            page=meta.page,
            account_dir=account_dir,
        )

        if meta.thread_id not in threads_map:
            threads_map[meta.thread_id] = ExportThreadInfo(
                thread_id=meta.thread_id,
                title=meta.title,
                slug=meta.slug,
                account=account,
                created_at=created_dt,
                updated_at=updated_dt,
                turns=meta.turn_count,
            )

        thread_info = threads_map[meta.thread_id]
        if repo_path not in thread_info.pages:
            thread_info.pages.append(repo_path)
        thread_info.page_contents[repo_path] = content
        if updated_dt > thread_info.updated_at:
            thread_info.updated_at = updated_dt
        if meta.turn_count > thread_info.turns:
            thread_info.turns = meta.turn_count

    return generate_archive_tree(list(threads_map.values()), account_dir=account_dir)

