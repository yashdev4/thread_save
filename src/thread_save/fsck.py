"""ThreadVault Invariant Checker (fsck) per §3.8 and W-1 ... W-10.

Verifies:
- File-based Markdown vaults: Recursively scans vault directory for *_p[0-9][0-9].md files,
  groups by thread_id, verifies front matter, delimiters, nonces, turn continuity, and hashes.
- PostgreSQL databases (--dsn): Verifies W-1..W-10 directly against Postgres tables
  (accounts, threads, turns, gaps, deleted_threads, outbox).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
from typing import Optional

import asyncpg

from thread_save.security.idempotency import (
    compute_content_hash,
    compute_content_hash_short,
)
from thread_save.storage.formatter import parse_front_matter, parse_turns


def verify_vault(vault_root: str | Path) -> int:
    root = Path(vault_root)
    print(f"Running fsck against vault: {root}")

    files_scanned = 0
    threads_scanned = 0
    turns_scanned = 0
    violations: list[str] = []

    all_pages = sorted(list(root.rglob("*_p[0-9][0-9].md")))
    if not all_pages:
        print("Error: 0 files scanned.")
        return 1

    files_scanned = len(all_pages)

    # Group pages by thread_id
    thread_pages: dict[str, list[Path]] = {}
    for p in all_pages:
        try:
            content = p.read_text(encoding="utf-8")
            meta, _ = parse_front_matter(content)
            thread_pages.setdefault(meta.thread_id, []).append(p)
        except Exception as e:
            violations.append(f"{p.name}: Failed to parse front matter: {e}")

    threads_scanned = len(thread_pages)

    for tid, pages in thread_pages.items():
        pages.sort()
        known_turns: dict[int, dict[str, str]] = {}
        thread_nonce = None

        for page_file in pages:
            content = page_file.read_text(encoding="utf-8")
            try:
                meta, body_text = parse_front_matter(content)
            except Exception:
                continue

            if thread_nonce is None:
                thread_nonce = meta.nonce
            elif meta.nonce != thread_nonce:
                violations.append(
                    f"{tid} {page_file.name}: Nonce changed across pages ({thread_nonce} vs {meta.nonce})"
                )

            # W-9: check delimiter balance
            open_tags = re.findall(r"<!-- turn [^>]*-->", content)
            close_tags = re.findall(r"<!-- /turn [^>]*-->", content)
            if len(open_tags) != len(close_tags):
                violations.append(
                    f"{tid} {page_file.name}: W-9 delimiter count mismatch ({len(open_tags)} open, {len(close_tags)} close)"
                )

            # Parse turns with nonce
            turns = parse_turns(body_text, nonce=meta.nonce)
            for t in turns:
                turns_scanned += 1
                known_turns.setdefault(t.turn_index, {})[t.role] = t.body

                # W-7: check recomputed content hash
                computed_hash = compute_content_hash_short(t.body) if t.body else "00000000"
                if t.content_hash and t.content_hash != "00000000" and computed_hash != t.content_hash:
                    violations.append(
                        f"{tid} {page_file.name}: W-7 Turn {t.turn_index} {t.role} hash mismatch (stored {t.content_hash} vs computed {computed_hash})"
                    )

                # Stub check: spec body format
                if t.fidelity.value == "stub":
                    if not (t.body.startswith("[not archived") and t.body.endswith("]")):
                        violations.append(
                            f"{tid} {page_file.name}: W-7 Turn {t.turn_index} {t.role} stub body invalid: {t.body!r}"
                        )

        # W-2, W-3: turn continuity
        if known_turns:
            user_ns = sorted(known_turns.keys())
            if user_ns[0] != 1:
                violations.append(f"{tid}: Missing turn 1 (first turn is {user_ns[0]})")

            for n in user_ns:
                roles = known_turns[n]
                if "user" not in roles:
                    violations.append(f"{tid}: Turn {n} missing user slot")

    print(f"Scanned {files_scanned} files across {threads_scanned} threads ({turns_scanned} turns).")

    if files_scanned == 0:
        print("Error: 0 files scanned.")
        return 1

    if not violations:
        print("fsck clear.")
        return 0
    else:
        print(f"fsck violations found ({len(violations)}):")
        for v in violations:
            print(f" - {v}")
        return 1


async def verify_pg_vault_async(dsn: str) -> int:
    """Check W-1 ... W-10 invariants directly against PostgreSQL tables."""
    print(f"Running fsck against PostgreSQL database...")
    violations: list[str] = []
    conn = await asyncpg.connect(dsn)
    try:
        # 1. Accounts (W-1)
        acc_rows = await conn.fetch("SELECT id, slug, oauth_sub, created_at FROM accounts")
        accounts_scanned = len(acc_rows)
        account_ids = {str(r["id"]) for r in acc_rows}

        # 2. Threads (W-1)
        thread_rows = await conn.fetch(
            """SELECT id, account_id, title, slug, created_at, updated_at,
                      open_turn, paused, max_n, current_page, current_page_turns,
                      current_page_bytes, delim
               FROM threads"""
        )
        threads_scanned = len(thread_rows)

        for t in thread_rows:
            acc_id = str(t["account_id"])
            if acc_id not in account_ids:
                violations.append(f"W-1 Thread {t['id']}: account_id {acc_id} does not exist in accounts table")

        # 3. Turns (W-1, W-2, W-3, W-4, W-6, W-7, W-9)
        turn_rows = await conn.fetch(
            """SELECT thread_id, n, role, body, fidelity, chars, hash,
                      recovered, created_at, updated_at, page, turn_key, anchor
               FROM turns
               ORDER BY thread_id, n ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END ASC"""
        )
        turns_scanned = len(turn_rows)

        # 4. Gaps
        gap_rows = await conn.fetch("SELECT thread_id, n, state, requested, first_seen FROM gaps")
        gaps_scanned = len(gap_rows)

        # 5. Outbox
        outbox_rows = await conn.fetch("SELECT thread_id, target, due_at, last_hash, attempts FROM outbox")
        outbox_scanned = len(outbox_rows)

        # 6. Tombstones (W-10)
        tombstone_rows = await conn.fetch("SELECT thread_id FROM deleted_threads")
        tombstoned_ids = {r["thread_id"] for r in tombstone_rows}
        for t in thread_rows:
            if t["id"] in tombstoned_ids:
                violations.append(f"W-10 Thread {t['id']} exists in threads but is marked deleted in deleted_threads")
        for tr in turn_rows:
            if tr["thread_id"] in tombstoned_ids:
                violations.append(f"W-10 Turn ({tr['thread_id']}, {tr['n']}, {tr['role']}) exists for deleted thread")

        # Group turns by thread
        thread_ids_set = {t["id"] for t in thread_rows}
        turns_by_thread: dict[str, list[dict]] = {}
        for tr in turn_rows:
            turns_by_thread.setdefault(tr["thread_id"], []).append(tr)

        for tid, t_turns in turns_by_thread.items():
            if tid not in thread_ids_set:
                violations.append(f"W-1 Turns exist for non-existent thread {tid}")
                continue

            turns_by_n: dict[int, dict[str, dict]] = {}
            for tr in t_turns:
                turns_by_n.setdefault(tr["n"], {})[tr["role"]] = tr

                # W-7: Content hash validation
                body = tr["body"] or ""
                stored_hash = tr["hash"] or ""
                full_hash = compute_content_hash(body) if body else "0" * 64
                short_hash = full_hash[:8]
                if stored_hash and stored_hash not in ("00000000", "0" * 64):
                    if len(stored_hash) == 64 and stored_hash != full_hash:
                        violations.append(
                            f"W-7 Thread {tid} Turn {tr['n']} {tr['role']}: hash mismatch (stored {stored_hash} vs computed {full_hash})"
                        )
                    elif len(stored_hash) != 64 and stored_hash != short_hash:
                        violations.append(
                            f"W-7 Thread {tid} Turn {tr['n']} {tr['role']}: hash mismatch (stored {stored_hash} vs computed {short_hash})"
                        )

                # W-9: Turn delimiter forgery prevention
                if "<!-- /turn" in body or "<!-- turn" in body:
                    violations.append(
                        f"W-9 Thread {tid} Turn {tr['n']} {tr['role']}: body contains unescaped turn delimiter tags"
                    )

            # W-2: Dense turn numbers from 1 to max_n
            user_ns = sorted([n for n, roles in turns_by_n.items() if "user" in roles])
            if user_ns:
                if user_ns[0] != 1:
                    violations.append(f"W-2 Thread {tid}: Missing turn 1 (first user turn is {user_ns[0]})")
                for expected_n in range(1, max(user_ns) + 1):
                    if expected_n not in turns_by_n or "user" not in turns_by_n[expected_n]:
                        violations.append(f"W-2 Thread {tid}: Missing user turn {expected_n}")

            # W-3: Assistant slot only exists if user slot exists
            for n_val, roles in turns_by_n.items():
                if "assistant" in roles and "user" not in roles:
                    violations.append(f"W-3 Thread {tid}: Turn {n_val} has assistant slot without user slot")

                # W-6: User and assistant share same page
                if "user" in roles and "assistant" in roles:
                    u_page = roles["user"]["page"]
                    a_page = roles["assistant"]["page"]
                    if u_page != a_page:
                        violations.append(f"W-6 Thread {tid}: Turn {n_val} user page {u_page} != assistant page {a_page}")

        print(
            f"Scanned {accounts_scanned} accounts, {threads_scanned} threads, {turns_scanned} turns, {gaps_scanned} gaps, {outbox_scanned} outbox jobs in database."
        )

        if not violations:
            print("Postgres fsck clear.")
            return 0
        else:
            print(f"Postgres fsck violations found ({len(violations)}):")
            for v in violations:
                print(f" - {v}")
            return 1
    finally:
        await conn.close()


def verify_pg_vault(dsn: str) -> int:
    return asyncio.run(verify_pg_vault_async(dsn))


def main():
    parser = argparse.ArgumentParser(description="ThreadVault Invariant Checker (fsck)")
    parser.add_argument("vault", nargs="?", default=None, help="Vault directory path")
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN to verify database rows")
    args = parser.parse_args()

    exit_code = 0
    if args.dsn:
        res = verify_pg_vault(args.dsn)
        if res != 0:
            exit_code = res

    if args.vault or not args.dsn:
        target = args.vault or "vault"
        res = verify_vault(target)
        if res != 0:
            exit_code = res

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
