"""ThreadVault Invariant Checker (fsck) per §3.8 and W-1 ... W-10.

Recursively scans vault directory for *_p[0-9][0-9].md files, groups by
thread_id, verifies front matter, delimiters, nonces, turn continuity,
and hashes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from thread_save.security.idempotency import compute_content_hash_short
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


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "vault"
    sys.exit(verify_vault(target))
