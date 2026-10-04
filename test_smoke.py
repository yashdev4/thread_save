"""Smoke test — verify imports, thread creation, and turn saving."""

import asyncio
import sys
import os
import tempfile
import shutil

# Redirect stdout before importing server
_real_stdout = sys.stdout


async def test_full_flow():
    """Test thread_open → save_turn → save_turn → find_thread → thread_stats."""
    # Use a temp directory as vault root
    vault_root = tempfile.mkdtemp(prefix="thread_save_test_")
    os.environ["THREAD_SAVE_VAULT_ROOT"] = vault_root
    os.environ["THREAD_SAVE_ACCOUNT"] = "test-user"

    try:
        from thread_save.config import load_config
        from thread_save.storage.path_resolver import ensure_vault_structure
        from thread_save.storage.writer import WriteEngine
        from thread_save.index.sqlite_index import ThreadIndex
        from thread_save.security.sanitizer import sanitize_slug
        from thread_save.security.redactor import redact_text
        from thread_save.security.idempotency import compute_turn_hash, IdempotencyRing

        # ── Test sanitizer ──
        assert sanitize_slug("../../../etc/passwd") == "etcpasswd"
        assert sanitize_slug("CON") == "_con"
        assert sanitize_slug("Hello World! (2026)") == "hello-world-2026"
        assert sanitize_slug("") == "untitled"
        print("[PASS] Sanitizer")

        # ── Test redactor ──
        result = redact_text("my key is AKIAIOSFODNN7EXAMPLE and secret is abc123")
        assert result.was_redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in result.text
        assert "[REDACTED:aws_access_key:" in result.text
        print("[PASS] Redactor")

        # ── Test idempotency ──
        ring = IdempotencyRing(capacity=3)
        h = compute_turn_hash("hello", "world")
        assert ring.check_and_record("t1", h) is True  # First time: new
        assert ring.check_and_record("t1", h) is False  # Second time: duplicate
        print("[PASS] Idempotency")

        # ── Test config ──
        config = load_config()
        assert str(config.vault_root) == vault_root
        assert config.default_account == "test-user"
        print("[PASS] Config")

        # ── Test vault structure ──
        ensure_vault_structure(config)
        assert config.index_dir.exists()
        assert config.assets_dir.exists()
        assert (config.vault_root / ".gitignore").exists()
        print("[PASS] Vault structure")

        # ── Test write engine ──
        engine = WriteEngine(config)

        # thread_open
        result1 = await engine.thread_open(
            title_hint="Testing Postgres Bulk Inserts",
            tags=["postgres", "python"],
        )
        assert result1["thread_id"]
        assert result1["page"] == 1
        assert os.path.exists(result1["path"])
        thread_id = result1["thread_id"]
        print(f"[PASS] thread_open -> {thread_id}")

        # save_turn (turn 1)
        result2 = await engine.save_turn(
            user_query="How do I bulk insert 50k rows in asyncpg?",
            assistant_response="Use `copy_records_to_table` for maximum throughput:\n\n```python\nawait conn.copy_records_to_table('transactions', records=data)\n```\n\nThis bypasses the normal INSERT pipeline.",
            thread_id=thread_id,
            model="claude-opus-4",
        )
        assert result2["status"] == "success"
        assert result2["turn_index"] == 1
        print(f"[PASS] save_turn #1 -> page {result2['page']}, {result2['file_size_kb']} KB")

        # save_turn (turn 2)
        result3 = await engine.save_turn(
            user_query="What if one record violates a unique constraint?",
            assistant_response="With `copy_records_to_table`, the entire batch rolls back on any constraint violation. To handle this gracefully, use `ON CONFLICT` with a staging table pattern.",
            thread_id=thread_id,
            model="claude-opus-4",
        )
        assert result3["status"] == "success"
        assert result3["turn_index"] == 2
        print(f"[PASS] save_turn #2 -> page {result3['page']}, {result3['file_size_kb']} KB")

        # Duplicate detection
        result4 = await engine.save_turn(
            user_query="What if one record violates a unique constraint?",
            assistant_response="With `copy_records_to_table`, the entire batch rolls back on any constraint violation. To handle this gracefully, use `ON CONFLICT` with a staging table pattern.",
            thread_id=thread_id,
        )
        assert result4["status"] == "duplicate_skipped"
        print("[PASS] Duplicate detection")

        # Verify the markdown file content
        with open(result1["path"], "r", encoding="utf-8") as f:
            content = f.read()
        assert "schema_version: 1" in content
        assert f"thread_id: {thread_id}" in content
        assert "## User" in content
        assert "## Claude" in content
        assert "<!-- turn i=1 role=user" in content
        assert "<!-- /turn i=1 -->" in content
        assert "copy_records_to_table" in content
        print("[PASS] Markdown content verified")

        # ── Test SQLite index ──
        index = ThreadIndex(config)
        print(f"  Index DB: {config.index_db_path}")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        index.upsert_thread(
            thread_id=thread_id,
            title="Testing Postgres Bulk Inserts",
            slug="testing-postgres-bulk-inserts",
            account="test-user",
            path=result1["path"],
            created=now,
            updated=now,
            tags=["postgres"],
        )
        index.index_turn(thread_id, 1, "user", "How do I bulk insert 50k rows in postgres")
        index.index_turn(thread_id, 1, "assistant", "Use copy_records_to_table")

        search_results = index.search_threads(query="postgres")
        print(f"  Search results: {len(search_results)}")
        assert len(search_results) > 0, f"Expected results but got: {search_results}"
        assert search_results[0]["thread_id"] == thread_id
        print("[PASS] SQLite FTS5 search")

        # thread_stats
        stats = engine.get_thread_stats(thread_id)
        assert stats is not None
        assert stats["turn_count"] == 2
        assert stats["pages"] == 1
        print(f"[PASS] thread_stats -> {stats['turn_count']} turns, {stats['pages']} page(s)")

        index.close()

        print()
        print("=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)
        print(f"\nVault root: {vault_root}")
        print(f"Thread file: {result1['path']}")
        print()
        print("--- File contents preview (first 50 lines) ---")
        with open(result1["path"], "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 50:
                    print("... (truncated)")
                    break
                print(line, end="")

    finally:
        # Clean up
        shutil.rmtree(vault_root, ignore_errors=True)
        # Clean env
        os.environ.pop("THREAD_SAVE_VAULT_ROOT", None)
        os.environ.pop("THREAD_SAVE_ACCOUNT", None)


if __name__ == "__main__":
    asyncio.run(test_full_flow())
