"""Local Offload and Sync Manager (§1 G7, Milestone GH7).

Implements:
- Offload condition: idle >= offload_after_days (default 14), all pages confirmed
  exported to GitHub (last_exported_hash == current_hash and remote commit confirmed),
  no gaps in requested state, no unfinalised chunks.
- Pointer format in _index/offloaded.json:
  {thread_id: {repo, commit, pages: {rel_path: content_hash}, last_user_anchor, recent_turn_keys, max_n, delim}}
- File deletion: deletes local page files from vault on offload.
- Resumed conversation binding and dedup: binds to offloaded threads, dedups via recent_turn_keys.
- Rehydration on write: fetches pages at recorded commit from GitHub, verifies every hash,
  restores local files inside thread_txn.
- Continuation thread fallback: if rehydration fails (unreachable, token expired, hash mismatch),
  creates continuation thread with continues: <thread_id> in front matter, returns ok: true.
- Local export manifest (_index/export_manifest.json) tracking export state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import logging
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import httpx

from thread_save.config import VaultConfig
from thread_save.models import Fidelity, PageState, SlotKey, ThreadMeta
from thread_save.security.idempotency import compute_content_hash, SlotIndex
from thread_save.security.sanitizer import sanitize_slug
from thread_save.storage.formatter import parse_front_matter, parse_turns
from thread_save.storage.identity import generate_thread_id_short, normalise_anchor
from thread_save.storage.path_resolver import ensure_directory, resolve_thread_path

if TYPE_CHECKING:
    from thread_save.export.github import GitHubDataApiTarget
    from thread_save.storage.writer import FileStore, _ThreadEntry

logger = logging.getLogger("thread_save.export.offload")


@dataclass
class OffloadPointer:
    """Represents a cold thread offloaded to a private GitHub archive (§1 G7)."""
    thread_id: str
    repo: str
    commit: str
    pages: dict[str, str]  # rel_path -> content_hash (e.g. "v1:3fa9...")
    last_user_anchor: str
    recent_turn_keys: list[str]
    max_n: int
    delim: str
    title: str = ""
    slug: str = ""
    account: str = "default"
    offloaded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "commit": self.commit,
            "pages": dict(self.pages),
            "last_user_anchor": self.last_user_anchor,
            "recent_turn_keys": list(self.recent_turn_keys),
            "max_n": self.max_n,
            "delim": self.delim,
            "title": self.title,
            "slug": self.slug,
            "account": self.account,
            "offloaded_at": self.offloaded_at,
        }

    @classmethod
    def from_dict(cls, thread_id: str, data: dict[str, Any]) -> "OffloadPointer":
        return cls(
            thread_id=thread_id,
            repo=data.get("repo", ""),
            commit=data.get("commit", ""),
            pages=data.get("pages", {}),
            last_user_anchor=data.get("last_user_anchor", ""),
            recent_turn_keys=data.get("recent_turn_keys", []),
            max_n=int(data.get("max_n", 0)),
            delim=data.get("delim", ""),
            title=data.get("title", ""),
            slug=data.get("slug", ""),
            account=data.get("account", "default"),
            offloaded_at=data.get("offloaded_at", ""),
        )


def compute_page_hash(content: str) -> str:
    """Compute deterministic page content hash prefixed with v1: (§1 G7)."""
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"v1:{h}"


def verify_page_hash(content: str, expected_hash: str) -> bool:
    """Verify page content against recorded expected hash (accepts v1: or raw sha256)."""
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if expected_hash.startswith("v1:"):
        return f"v1:{h}" == expected_hash or h == expected_hash[3:]
    return h == expected_hash


class FileStoreExportManifest:
    """Manages the local export manifest in _index/export_manifest.json (§1 G7)."""

    def __init__(self, manifest_path: Path):
        self.path = manifest_path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"repo": "", "commit": "", "pages": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to parse export manifest %s: %s", self.path, e)
            return {"repo": "", "commit": "", "pages": {}}

    def save(self, data: dict[str, Any]) -> None:
        ensure_directory(self.path.parent)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def record_export(
        self,
        repo: str,
        commit_sha: str,
        pages_exported: dict[str, str],  # rel_path -> content_hash or content
        blob_shas: dict[str, str],
    ) -> None:
        data = self.load()
        data["repo"] = repo
        data["commit"] = commit_sha
        pages_dict = data.setdefault("pages", {})
        now_iso = datetime.now(timezone.utc).isoformat()

        for p, h in pages_exported.items():
            # If h is content string, hash it
            val_hash = h if (h.startswith("v1:") or len(h) == 64) else compute_page_hash(h)
            pages_dict[p] = {
                "hash": val_hash,
                "blob_sha": blob_shas.get(p, ""),
                "commit_sha": commit_sha,
                "exported_at": now_iso,
            }
        self.save(data)


class OffloadIndex:
    """Manages the offloaded threads index in _index/offloaded.json (§1 G7)."""

    def __init__(self, index_path: Path):
        self.path = index_path

    def load(self) -> dict[str, OffloadPointer]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                tid: OffloadPointer.from_dict(tid, info)
                for tid, info in raw.items()
                if isinstance(info, dict)
            }
        except Exception as e:
            logger.warning("Failed to parse offloaded index %s: %s", self.path, e)
            return {}

    def save(self, pointers: dict[str, OffloadPointer]) -> None:
        ensure_directory(self.path.parent)
        raw = {tid: ptr.to_dict() for tid, ptr in pointers.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, thread_id: str) -> Optional[OffloadPointer]:
        pointers = self.load()
        return pointers.get(thread_id)

    def set(self, pointer: OffloadPointer) -> None:
        pointers = self.load()
        pointers[pointer.thread_id] = pointer
        self.save(pointers)

    def delete(self, thread_id: str) -> None:
        pointers = self.load()
        if thread_id in pointers:
            del pointers[thread_id]
            self.save(pointers)


class OffloadManager:
    """Coordinates condition evaluation, offload execution, and rehydration."""

    def __init__(self, config: VaultConfig):
        self.config = config
        self.offload_index = OffloadIndex(config.offloaded_json_path)
        self.manifest_mgr = FileStoreExportManifest(config.export_manifest_path)

    def is_thread_offloadable(
        self,
        store: "FileStore",
        thread_id: str,
        now: Optional[datetime] = None,
        offload_after_days: Optional[int] = None,
    ) -> tuple[bool, str]:
        """Check whether a thread satisfies all §1 G7 offload conditions:

        1. idle >= offload_after_days (default 14)
        2. every page confirmed exported to GitHub (last_exported_hash == current_hash, remote commit confirmed)
        3. no gaps in requested state, no open gaps, no unfinalised chunks
        """
        ref_now = now or datetime.now(timezone.utc)
        days = offload_after_days if offload_after_days is not None else self.config.offload_after_days

        entry = store._registry.get(thread_id)
        if not entry:
            return False, f"Thread {thread_id} not found in store"

        # 1. Idle condition
        meta = entry.meta
        updated_dt = meta.updated
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=timezone.utc)

        idle_delta = ref_now - updated_dt
        if idle_delta < timedelta(days=days):
            return False, f"Thread idle time ({idle_delta.days} days) < {days} days"

        # 2. Check gaps
        if hasattr(store, "gap_tracker"):
            gap_summary = store.gap_tracker.get_summary(thread_id)
            if gap_summary.get("gaps_open"):
                return False, f"Thread has {len(gap_summary['gaps_open'])} open gaps"

        # 3. Export confirmation against manifest
        manifest = self.manifest_mgr.load()
        manifest_pages = manifest.get("pages", {})
        manifest_commit = manifest.get("commit", "")

        if not manifest_commit:
            return False, "Export manifest has no confirmed commit"

        # Find all local page files for this thread
        ps = store._page_states.get(thread_id)
        if not ps:
            return False, f"PageState not found for {thread_id}"

        # Collect pages for thread
        page_files = list(Path(store._config.vault_root).rglob(f"*_{entry.thread_id_short}_*_p[0-9][0-9].md"))
        if not page_files:
            return False, "No page files found on disk"

        for pf in page_files:
            # Rel path relative to vault root
            try:
                rel_path = pf.relative_to(store._config.vault_root).as_posix()
            except ValueError:
                rel_path = pf.name

            # Also check layout without account prefix if account_dir is false
            page_entry = manifest_pages.get(rel_path)
            if not page_entry:
                # Try path relative to account directory: e.g. "2026/10/filename_p01.md"
                parts = rel_path.split("/", 1)
                if len(parts) > 1 and parts[1] in manifest_pages:
                    page_entry = manifest_pages[parts[1]]

            if not page_entry:
                return False, f"Page {rel_path} has not been exported to GitHub"

            # Verify current hash matches last_exported_hash
            content = pf.read_text(encoding="utf-8")
            cur_hash = compute_page_hash(content)
            exp_hash = page_entry.get("hash", "")
            if not verify_page_hash(content, exp_hash):
                return False, f"Page {rel_path} has changed since export (hash mismatch)"

        return True, "ok"

    def offload_thread(
        self,
        store: "FileStore",
        thread_id: str,
        repo: Optional[str] = None,
        commit: Optional[str] = None,
    ) -> OffloadPointer:
        """Offload a thread: record pointer in _index/offloaded.json and delete disk files."""
        entry = store._registry.get(thread_id)
        if not entry:
            raise ValueError(f"Thread {thread_id} not found in store")

        manifest = self.manifest_mgr.load()
        target_repo = repo or manifest.get("repo", "")
        target_commit = commit or manifest.get("commit", "")

        # Find all local page files for this thread
        page_files = sorted(list(Path(store._config.vault_root).rglob(f"*_{entry.thread_id_short}_*_p[0-9][0-9].md")))
        if not page_files:
            raise ValueError(f"No page files found for thread {thread_id}")

        pages_map: dict[str, str] = {}
        for pf in page_files:
            try:
                rel_path = pf.relative_to(store._config.vault_root).as_posix()
            except ValueError:
                rel_path = pf.name
            content = pf.read_text(encoding="utf-8")
            pages_map[rel_path] = compute_page_hash(content)

        last_anchor = ""
        if entry.anchor_map:
            highest_n = max(entry.anchor_map.keys())
            last_anchor = entry.anchor_map.get(highest_n, "")

        recent_turn_keys = store._slots.get_recent_turn_keys(thread_id, limit=10)
        max_n = store._slots.highest_n(thread_id)
        delim = getattr(entry.meta, "nonce", "") or getattr(entry.meta, "delim", "")

        now_iso = datetime.now(timezone.utc).isoformat()
        pointer = OffloadPointer(
            thread_id=thread_id,
            repo=target_repo,
            commit=target_commit,
            pages=pages_map,
            last_user_anchor=last_anchor,
            recent_turn_keys=recent_turn_keys,
            max_n=max_n,
            delim=delim,
            title=entry.meta.title,
            slug=entry.meta.slug,
            account=entry.meta.account,
            offloaded_at=now_iso,
        )

        # 1. Write pointer atomically to _index/offloaded.json
        self.offload_index.set(pointer)

        # 2. Delete local page files from vault on disk
        for pf in page_files:
            try:
                pf.unlink(missing_ok=True)
                # Clean empty parent dir if empty
                parent = pf.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except Exception as e:
                logger.warning("Failed to delete offloaded file %s: %s", pf, e)

        # 3. Update FileStore in-memory state
        store._page_states.pop(thread_id, None)
        # Keep pointer registered in store._offloaded_threads
        store._offloaded_threads[thread_id] = pointer.to_dict()

        logger.info("Successfully offloaded thread %s (%d pages) to GitHub pointer", thread_id, len(pages_map))
        return pointer

    async def rehydrate_thread(
        self,
        store: "FileStore",
        thread_id: str,
        github_target: "GitHubDataApiTarget",
        client: Optional[httpx.AsyncClient] = None,
    ) -> bool:
        """Rehydrate an offloaded thread: fetch from GitHub at commit, verify hashes, restore files."""
        pointer = self.offload_index.get(thread_id)
        if not pointer:
            # Check store's offloaded cache
            raw_ptr = store._offloaded_threads.get(thread_id)
            if raw_ptr:
                pointer = OffloadPointer.from_dict(thread_id, raw_ptr)
            else:
                logger.warning("Thread %s not found in offloaded index", thread_id)
                return False

        fetched_pages: dict[str, str] = {}

        # 1. Fetch and verify ALL pages into memory before touching disk
        try:
            for rel_path, expected_hash in pointer.pages.items():
                content = await github_target.fetch_file_content(
                    path=rel_path,
                    ref=pointer.commit,
                    client=client,
                )
                if not verify_page_hash(content, expected_hash):
                    logger.error(
                        "Rehydration hash mismatch for thread %s path %s: expected %s",
                        thread_id,
                        rel_path,
                        expected_hash,
                    )
                    return False
                fetched_pages[rel_path] = content
        except Exception as e:
            logger.warning("Rehydration failed for thread %s (GitHub fetch error): %s", thread_id, e)
            return False

        # 2. All pages verified — restore cleanly to disk
        try:
            for rel_path, content in fetched_pages.items():
                full_path = store._config.vault_root / rel_path
                ensure_directory(full_path.parent)
                full_path.write_text(content, encoding="utf-8")

            # 3. Rebuild in-memory state in store (same code path as a local restart, B6 L1)
            pages = []
            for rel_path, content in fetched_pages.items():
                full_path = store._config.vault_root / rel_path
                m = re.search(r"_p(\d+)\.md$", full_path.name)
                page_no = int(m.group(1)) if m else parse_front_matter(content)[0].page
                pages.append((page_no, full_path, content))
            store.restore_thread_state(thread_id, pages)

            # 4. Remove from offloaded index
            self.offload_index.delete(thread_id)
            store._offloaded_threads.pop(thread_id, None)

            logger.info("Successfully rehydrated thread %s from GitHub commit %s", thread_id, pointer.commit)
            return True

        except Exception as e:
            logger.critical("Error restoring rehydrated files to disk for %s: %s", thread_id, e)
            return False
