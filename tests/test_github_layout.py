"""Tests for GitHub Archive Layout and Deterministic Indexes (Milestone GH2).

Verifies:
- Golden files for README.md, index/threads.json, and monthly indexes
- Relative markdown links in monthly indexes correctly target ../YYYY/MM/...
"""

from datetime import datetime, timezone
import json

from thread_save.export.layout import (
    ExportThreadInfo,
    generate_monthly_index,
    generate_readme,
    generate_threads_json,
)


def test_readme_golden():
    """Verify README.md contains required guidelines, conflict notices, and squash CLI commands."""
    readme = generate_readme()

    assert "# ThreadVault Archive" in readme
    assert "Do Not Edit Directly:" in readme
    assert "Privacy Notice:" in readme
    assert "Deletion Semantics:" in readme
    assert "python -m thread_save.cli.github squash" in readme
    assert "threads.json" in readme
    assert "YYYY-MM.md" in readme


def test_threads_json_golden():
    """Verify index/threads.json format is deterministically structured and sorted."""
    dt1 = datetime(2026, 10, 2, 14, 23, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 10, 4, 9, 15, 0, tzinfo=timezone.utc)

    threads = [
        ExportThreadInfo(
            thread_id="01M444NQWT0R5QZB9KE26DH74B",
            title="Thread B",
            slug="thread-b",
            account="testuser",
            created_at=dt2,
            updated_at=dt2,
            turns=2,
            pages=["2026/10/2026-10-04T0915_testuser_6dh74b_thread-b_p01.md"],
        ),
        ExportThreadInfo(
            thread_id="01M444NQWT0R5QZB9KE26DH74A",
            title="Thread A",
            slug="thread-a",
            account="testuser",
            created_at=dt1,
            updated_at=dt1,
            turns=4,
            pages=["2026/10/2026-10-02T1423_testuser_6dh74a_thread-a_p01.md"],
        ),
    ]

    out = generate_threads_json(threads)
    parsed = json.loads(out)

    # Deterministic keys sorted by thread_id
    keys = list(parsed.keys())
    assert keys == ["01M444NQWT0R5QZB9KE26DH74A", "01M444NQWT0R5QZB9KE26DH74B"]

    t_a = parsed["01M444NQWT0R5QZB9KE26DH74A"]
    assert t_a["title"] == "Thread A"
    assert t_a["slug"] == "thread-a"
    assert t_a["account"] == "testuser"
    assert t_a["turns"] == 4
    assert t_a["pages"] == ["2026/10/2026-10-02T1423_testuser_6dh74a_thread-a_p01.md"]


def test_monthly_index_golden():
    """Verify generated index/YYYY-MM.md produces relative markdown links."""
    dt = datetime(2026, 10, 2, 14, 23, 0, tzinfo=timezone.utc)

    threads = [
        ExportThreadInfo(
            thread_id="01M444NQWT0R5QZB9KE26DH74A",
            title="Postgres Bulk Inserts",
            slug="postgres-bulk-inserts",
            account="testuser",
            created_at=dt,
            updated_at=dt,
            turns=6,
            pages=[
                "2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p01.md",
                "2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p02.md",
            ],
        )
    ]

    index_md = generate_monthly_index(2026, 10, threads)

    assert "# Threads — 2026-10" in index_md
    assert "## [Postgres Bulk Inserts](../2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p01.md)" in index_md
    assert "- **Thread ID:** `01M444NQWT0R5QZB9KE26DH74A`" in index_md
    assert "- **Turns:** 6" in index_md
    assert "[Page 1](../2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p01.md)" in index_md
    assert "[Page 2](../2026/10/2026-10-02T1423_testuser_6dh74a_postgres-bulk-inserts_p02.md)" in index_md

