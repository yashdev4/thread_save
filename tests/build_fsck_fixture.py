"""Build rich fsck fixture vault per Part A Item 4:
- At least one 3-page thread
- Stubs
- Recovered turns
- Truncated turn
- Redacted turn
- One schema_version 1 file
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import shutil

from thread_save.config import VaultConfig
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import format_turn, format_new_page, format_turn_separator
from thread_save.storage.path_resolver import ensure_vault_structure
from thread_save.storage.writer import FileStore
from thread_save.fsck import verify_vault


async def build_and_verify_fixture(target_dir: Path):
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    config = VaultConfig(
        vault_root=target_dir,
        default_account="test-account",
        page_max_turns=4,
        page_max_bytes=100_000,
    )
    ensure_vault_structure(config)
    store = FileStore(config)
    svc = TurnService(store, config=config)

    # ── 1. Create a 3-page thread with stubs, recovered, truncated, redacted ──
    # Turn 1: Redacted turn (AWS key)
    r1 = await svc.save_turn(
        user_query="Please connect with my aws key: AKIA1234567890ABCDEF",
        title_hint="Rich Fixture Thread",
    )
    tid = r1["thread_id"]

    # Turn 2: Normal
    await svc.save_turn(
        user_query="Explain quantum computing",
        prev_response="Connected securely to AWS.",
        prev_user_anchor="Please connect with my aws key: AKIA1234567890ABCDEF",
        thread_id=tid,
    )

    # Turn 3: Page rollover to page 2!
    # Also skip to turn 5 to create stubs for turn 4
    await svc.save_turn(
        user_query="Continue after skip",
        prev_response="Quantum computing uses qubits.",
        prev_user_anchor="Explain quantum computing",
        client_turn_number=5,
        thread_id=tid,
    )

    # Recover turn 3 & 4 (Recovered turns)
    await svc.backfill(
        thread_id=tid,
        turns=[
            {"n": 3, "user_query": "Missed question 3", "assistant_response": "Missed answer 3"},
            {"n": 4, "user_query": "Missed question 4", "assistant_response": "Missed answer 4"},
        ],
    )

    # Turn 6: Truncated turn (> 100,000 characters)
    oversized = "X" * 105_000
    await svc.save_turn(
        user_query=oversized,
        prev_response="Here is the response to turn 5.",
        prev_user_anchor="Continue after skip",
        thread_id=tid,
    )

    # Turn 7 & 8: Page rollover to page 3!
    await svc.save_turn(
        user_query="Page 3 opener",
        prev_response="Response to oversized turn.",
        prev_user_anchor="Continue after skip",
        thread_id=tid,
    )
    await svc.save_turn(
        user_query="Page 3 final query",
        prev_response="Page 3 answer.",
        prev_user_anchor="Page 3 opener",
        thread_id=tid,
    )

    # ── 2. Add a separate schema_version: 1 thread file ──
    legacy_file = target_dir / "test-account" / "2026" / "09" / "2026-09-15T1000_test-account_leg001_legacy-topic_p01.md"
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_content = (
        "---\n"
        '"schema_version": 1\n'
        '"thread_id": "01JLEGACYV1THREAD00000000001"\n'
        '"title": "Legacy V1 Thread"\n'
        '"slug": "legacy-topic"\n'
        '"account": "test-account"\n'
        '"client": "claude-desktop"\n'
        '"created": "2026-09-15T10:00:00+00:00"\n'
        '"updated": "2026-09-15T10:00:00+00:00"\n'
        '"page": 1\n'
        '"turn_count": 1\n'
        '"turn_range": [1, 1]\n'
        '"bytes": 120\n'
        "---\n\n"
        "# Legacy V1 Thread\n\n"
        "<!-- turn i=1 role=user fidelity=verbatim chars=20 hash=38abf165 -->\n"
        "## User\n\n"
        "Legacy turn 1 content\n"
        "<!-- /turn i=1 -->\n"
        "\n---\n\n"
        "<!-- turn i=1 role=assistant fidelity=verbatim chars=19 hash=26f5681a -->\n"
        "## Claude\n\n"
        "Legacy assistant 1\n"
        "<!-- /turn i=1 -->\n"
    )
    legacy_file.write_text(legacy_content, encoding="utf-8")

    print("\n--- Running fsck against rich fixture vault ---")
    ret = verify_vault(target_dir)
    return ret


if __name__ == "__main__":
    vault_path = Path("vault_test_fixture")  # vault_rich_fixture now holds the deployed mirror
    res = asyncio.run(build_and_verify_fixture(vault_path))
    print("fsck return code:", res)
