"""Local Vault Migration CLI (Milestone X10).

Imports an existing local Markdown vault (FileStore format: *_p[0-9][0-9].md)
into PostgreSQL (PgStore).

Supports:
  - --dry-run: Previews import actions without modifying the database.
  - --vault-dir: Path to directory containing Markdown thread files.
  - --account-slug: Override account partition slug.
  - --dsn: Target PostgreSQL connection string.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from typing import Optional

from thread_save.models import Fidelity, ThreadMeta, TurnData
from thread_save.security.sanitizer import sanitize_slug
from thread_save.storage.formatter import parse_front_matter, parse_turns
from thread_save.storage.pg_store import PgStore, _RANK_TO_FIDELITY

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@dataclass
class MigrationSummary:
    threads_scanned: int = 0
    files_scanned: int = 0
    turns_scanned: int = 0
    threads_imported: int = 0
    turns_imported: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


async def import_local_vault(
    vault_dir: Path | str,
    pg_store: Optional[PgStore] = None,
    dsn: Optional[str] = None,
    account_slug: Optional[str] = None,
    dry_run: Optional[bool] = None,
    apply: bool = False,
    verbose: bool = True,
) -> MigrationSummary:
    """Import local Markdown vault files into PgStore.

    Safety (§Milestone H8):
    - Defaults to dry-run (requires apply=True or dry_run=False to write).
    - Validates that target account_slug matches an existing OAuth account in database.
    """
    if dry_run is not None:
        is_dry_run = dry_run and not apply
    else:
        is_dry_run = not apply

    root = Path(vault_dir)
    summary = MigrationSummary(dry_run=is_dry_run)

    if not root.exists() or not root.is_dir():
        summary.errors.append(f"Vault directory does not exist: {root}")
        return summary

    all_pages = sorted(list(root.rglob("*_p[0-9][0-9].md")))
    if not all_pages:
        summary.errors.append(f"No *_p[0-9][0-9].md files found in {root}")
        return summary

    summary.files_scanned = len(all_pages)

    # Group files by thread_id
    thread_groups: dict[str, list[tuple[Path, ThreadMeta, list[TurnData]]]] = {}

    for page_path in all_pages:
        try:
            content = page_path.read_text(encoding="utf-8")
            meta, body_text = parse_front_matter(content)
            turns = parse_turns(body_text, nonce=meta.nonce)
            thread_groups.setdefault(meta.thread_id, []).append((page_path, meta, turns))
            summary.turns_scanned += len(turns)
        except Exception as e:
            err_msg = f"Failed to parse {page_path.name}: {e}"
            summary.errors.append(err_msg)
            if verbose:
                print(f"[ERROR] {err_msg}", file=sys.stderr)

    summary.threads_scanned = len(thread_groups)

    if verbose:
        print(f"Scanned {summary.files_scanned} files across {summary.threads_scanned} threads ({summary.turns_scanned} turns).")

    # Determine target account
    target_account = account_slug
    if not target_account and thread_groups:
        first_group = next(iter(thread_groups.values()))
        if first_group:
            target_account = getattr(first_group[0][1], "account", None)
    clean_account = sanitize_slug(target_account or "default")

    # Connect to PostgreSQL to validate account and perform migration
    owns_store = False
    store = pg_store
    if store is None:
        target_dsn = dsn or TEST_DSN
        store = PgStore(dsn=target_dsn)
        await store.connect()
        owns_store = True

    try:
        async with store.pool.acquire() as conn:
            # Milestone H8: --account-slug must match existing OAuth account in database
            acc_row = await conn.fetchrow(
                "SELECT id, slug FROM accounts WHERE slug = $1",
                clean_account,
            )
            if acc_row is None:
                err_msg = (
                    f"Account slug '{clean_account}' does not match any existing OAuth account in database. "
                    "An existing OAuth account in the database is required before migrating."
                )
                summary.errors.append(err_msg)
                if verbose:
                    print(f"[ERROR] {err_msg}", file=sys.stderr)
                return summary

            acc_uuid = acc_row["id"]

            if is_dry_run:
                if verbose:
                    print(f"[DRY RUN] Target account '{clean_account}' verified (UUID: {acc_uuid}).")
                    print("[DRY RUN] Preview completed. No database changes were made.")
                unique_slots = set()
                for tid, page_tuples in thread_groups.items():
                    for _, _, turns in page_tuples:
                        for turn in turns:
                            unique_slots.add((tid, turn.turn_index, turn.role))
                summary.threads_imported = summary.threads_scanned
                summary.turns_imported = len(unique_slots)
                return summary

            for thread_id, page_tuples in thread_groups.items():
                try:
                    # Sort pages in ascending order
                    page_tuples.sort(key=lambda item: item[1].page)
                    first_meta = page_tuples[0][1]
                    last_meta = page_tuples[-1][1]

                    created_ts = first_meta.created
                    if isinstance(created_ts, str):
                        created_ts = datetime.fromisoformat(created_ts)
                    if created_ts.tzinfo is None:
                        created_ts = created_ts.replace(tzinfo=timezone.utc)

                    updated_ts = last_meta.updated
                    if isinstance(updated_ts, str):
                        updated_ts = datetime.fromisoformat(updated_ts)
                    if updated_ts.tzinfo is None:
                        updated_ts = updated_ts.replace(tzinfo=timezone.utc)

                    max_page = max(meta.page for _, meta, _ in page_tuples)
                    total_bytes = sum(len(p.read_text(encoding="utf-8")) for p, _, _ in page_tuples)

                    delim = (getattr(first_meta, "nonce", None) or "0000")[:4].ljust(4, "0")

                    # 1. Upsert thread record
                    await conn.execute(
                        """INSERT INTO threads (
                               id, account_id, title, slug, created_at, updated_at,
                               open_turn, paused, max_n, current_page, current_page_turns,
                               current_page_bytes, delim
                           )
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                           ON CONFLICT (id) DO UPDATE SET
                               title = EXCLUDED.title,
                               updated_at = EXCLUDED.updated_at,
                               open_turn = EXCLUDED.open_turn,
                               paused = EXCLUDED.paused,
                               max_n = GREATEST(threads.max_n, EXCLUDED.max_n),
                               current_page = GREATEST(threads.current_page, EXCLUDED.current_page),
                               current_page_bytes = EXCLUDED.current_page_bytes""",
                        thread_id,
                        acc_uuid,
                        first_meta.title,
                        first_meta.slug,
                        created_ts,
                        updated_ts,
                        last_meta.open_turn,
                        last_meta.paused,
                        0,  # updated after turns inserted
                        max_page,
                        0,
                        total_bytes,
                        delim,
                    )

                    # 2. Upsert turns
                    thread_turns_count = 0
                    max_n = 0
                    for page_path, meta, turns in page_tuples:
                        for turn in turns:
                            if turn.turn_index > max_n:
                                max_n = turn.turn_index

                            turn_ts = turn.timestamp
                            if isinstance(turn_ts, str):
                                turn_ts = datetime.fromisoformat(turn_ts)
                            if turn_ts.tzinfo is None:
                                turn_ts = turn_ts.replace(tzinfo=timezone.utc)

                            fidelity_rank = turn.fidelity.rank

                            await conn.execute(
                                """INSERT INTO turns (
                                       thread_id, n, role, body, fidelity, chars,
                                       hash, recovered, created_at, updated_at, page,
                                       turn_key, anchor
                                   )
                                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                                   ON CONFLICT (thread_id, n, role) DO UPDATE SET
                                       body = EXCLUDED.body,
                                       fidelity = EXCLUDED.fidelity,
                                       chars = EXCLUDED.chars,
                                       hash = EXCLUDED.hash,
                                       recovered = turns.recovered OR EXCLUDED.recovered,
                                       turn_key = COALESCE(EXCLUDED.turn_key, turns.turn_key),
                                       anchor = COALESCE(EXCLUDED.anchor, turns.anchor),
                                       updated_at = EXCLUDED.updated_at
                                   WHERE turns.hash <> EXCLUDED.hash
                                      OR EXCLUDED.fidelity >= turns.fidelity""",
                                thread_id,
                                turn.turn_index,
                                turn.role,
                                turn.body,
                                fidelity_rank,
                                turn.char_count,
                                turn.content_hash,
                                turn.recovered,
                                turn_ts,
                                turn_ts,
                                meta.page,
                                turn.turn_key or None,
                                turn.anchor or None,
                            )

                            # Record stubs and gaps
                            if turn.fidelity == Fidelity.STUB:
                                await conn.execute(
                                    """INSERT INTO gaps (thread_id, n, state, requested, first_seen)
                                       VALUES ($1, $2, 'detected', false, now())
                                       ON CONFLICT (thread_id, n) DO NOTHING""",
                                    thread_id,
                                    turn.turn_index,
                                )
                            elif turn.recovered:
                                await conn.execute(
                                    """INSERT INTO gaps (thread_id, n, state, requested, first_seen)
                                       VALUES ($1, $2, 'recovered', true, now())
                                       ON CONFLICT (thread_id, n) DO UPDATE SET state = 'recovered'""",
                                    thread_id,
                                    turn.turn_index,
                                )

                            thread_turns_count += 1

                    # Update max_n on thread
                    await conn.execute(
                        "UPDATE threads SET max_n = $1 WHERE id = $2",
                        max_n,
                        thread_id,
                    )

                    actual_turns = await conn.fetchval(
                        "SELECT count(*) FROM turns WHERE thread_id = $1",
                        thread_id,
                    )
                    summary.threads_imported += 1
                    summary.turns_imported += actual_turns

                    if verbose:
                        print(f"  [IMPORTED] Thread {thread_id} ({first_meta.title[:30]}...): {actual_turns} turns across {len(page_tuples)} pages.")

                except Exception as ex:
                    err = f"Failed to import thread {thread_id}: {ex}"
                    summary.errors.append(err)
                    if verbose:
                        print(f"[ERROR] {err}", file=sys.stderr)

    finally:
        if owns_store and store is not None:
            await store.close()

    if verbose:
        print("-" * 56)
        print("Vault Migration Summary:")
        print(f"  Threads Scanned:   {summary.threads_scanned}")
        print(f"  Files Scanned:     {summary.files_scanned}")
        print(f"  Turns Scanned:     {summary.turns_scanned}")
        print(f"  Threads Imported:  {summary.threads_imported}")
        print(f"  Turns Imported:    {summary.turns_imported}")
        print(f"  Errors/Skipped:    {len(summary.errors)}")
        print(f"  Result:            {'SUCCESS' if summary.success else 'FAILED'}")
        print("-" * 56)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="ThreadVault Local Markdown Vault -> PostgreSQL Migration Tool"
    )
    parser.add_argument(
        "--vault-dir",
        required=True,
        help="Path to local Markdown vault directory to import",
    )
    parser.add_argument(
        "--dsn",
        default=TEST_DSN,
        help="Target PostgreSQL DSN",
    )
    parser.add_argument(
        "--account-slug",
        default=None,
        help="Target account slug. Must match an existing OAuth account in database.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute database writes (requires --apply; default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Inspect files and report without modifying the database (default: True)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress detailed output",
    )

    args = parser.parse_args()

    # Safety: default to --dry-run, require --apply to write (§Milestone H8)
    is_dry_run = True
    if args.apply and not args.dry_run:
        is_dry_run = False

    summary = asyncio.run(
        import_local_vault(
            vault_dir=args.vault_dir,
            dsn=args.dsn,
            account_slug=args.account_slug,
            dry_run=is_dry_run,
            apply=not is_dry_run,
            verbose=not args.quiet,
        )
    )

    if not summary.success:
        for err in summary.errors:
            print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
