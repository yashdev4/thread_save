"""Tests for GitHub Conflicts and Dead-Letter Management (§1 G5 & G6, Milestone GH4).

Verifies:
- Human edit detection via blob SHA: human edits on GitHub are never overwritten
- Push-protection rejection triggers dead-lettering and is never re-sent
- Active conflicts and dead letters are surfaced in exporter status and report CLI
"""

from datetime import datetime, timezone
import httpx
import pytest

from thread_save.export.github import GitHubDataApiTarget, PushProtectionError
from thread_save.export.layout import ExportThreadInfo
from thread_save.export.sync import GitHubBatchExporter, GitHubExportConfig
from thread_save.report import format_export_status
from tests.test_github_export import MockGitHubApi


@pytest.mark.asyncio
async def test_human_edit_conflict_detection_skips_overwrite():
    """Verify human edits made directly on GitHub are detected via blob SHA and never overwritten (G5)."""
    mock = MockGitHubApi(is_private=True)
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )
    config = GitHubExportConfig(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        settle_minutes=0,  # Immediate export
    )
    exporter = GitHubBatchExporter(target=target, config=config)

    t0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    tid = "01M444NQWT0R5QZB9KE26DH74A"
    p01_path = "2026/10/2026-10-02T1200_testuser_6dh74a_conflict-test_p01.md"
    original_content = "# Original Page Content\n\nTurn 1"

    thread = ExportThreadInfo(
        thread_id=tid,
        title="Conflict Test",
        slug="conflict-test",
        account="testuser",
        created_at=t0,
        updated_at=t0,
        turns=1,
        pages=[p01_path],
        page_contents={p01_path: original_content},
    )

    # 1. Initial export succeeds
    res1 = await exporter.export_settled_threads([thread], force_settle=True)
    assert res1 is not None
    assert p01_path in exporter.blob_shas
    original_blob_sha = exporter.blob_shas[p01_path]

    # 2. Simulate human modifying p01 directly on GitHub web UI
    mock.simulate_remote_edit(p01_path, "# Human edited this file on GitHub!\n\nManual change")

    # 3. Local update occurs (e.g. Turn 2 saved locally)
    thread.updated_at = datetime(2026, 10, 2, 12, 10, 0, tzinfo=timezone.utc)
    thread.turns = 2
    thread.page_contents[p01_path] = "# Local update attempting to overwrite\n\nTurn 1\n\nTurn 2"

    mock.write_calls.clear()

    # 4. Exporter runs next batch
    res2 = await exporter.export_settled_threads([thread], force_settle=True)

    # 5. Verify conflict is detected
    assert p01_path in exporter.conflicts
    status = exporter.get_status()
    assert p01_path in status["conflicts"]

    # 6. Verify p01 was NOT written or overwritten in the batch
    if res2 is not None:
        tree_payload = mock.write_calls[0]["payload"]["tree"]
        written_paths = {item["path"] for item in tree_payload}
        assert p01_path not in written_paths, "Conflicted file was overwritten on GitHub!"


@pytest.mark.asyncio
async def test_push_protection_rejection_dead_letter_never_resent():
    """Verify GitHub push protection rejection dead-letters thread and prevents re-sending (G6)."""
    mock = MockGitHubApi(is_private=True)
    mock.simulate_push_protection = True  # Reject commit with secret scanning push protection
    transport = httpx.MockTransport(mock.handle_request)
    client = httpx.AsyncClient(transport=transport)

    target = GitHubDataApiTarget(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        client=client,
    )
    config = GitHubExportConfig(
        repo="testowner/testrepo",
        token="ghp_test_token_12345",
        settle_minutes=0,
    )
    exporter = GitHubBatchExporter(target=target, config=config)

    t0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    tid = "01M444NQWT0R5QZB9KE26DH74A"
    p01_path = "2026/10/2026-10-02T1200_testuser_6dh74a_leak-test_p01.md"

    thread = ExportThreadInfo(
        thread_id=tid,
        title="Push Protection Test",
        slug="push-protection-test",
        account="testuser",
        created_at=t0,
        updated_at=t0,
        turns=1,
        pages=[p01_path],
        page_contents={p01_path: "# Thread with detected secret"},
    )

    # 1. First export attempt triggers PushProtectionError and dead-letters the thread
    with pytest.raises(PushProtectionError):
        await exporter.export_settled_threads([thread], force_settle=True)

    # Thread is recorded in dead-letters registry
    assert tid in exporter.dead_letters
    status = exporter.get_status()
    assert tid in status["dead_letters"]

    mock.write_calls.clear()

    # 2. Subsequent export cycle: thread is excluded from export and never re-sent
    res = await exporter.export_settled_threads([thread], force_settle=True)
    assert res is None
    assert len(mock.write_calls) == 0


def test_conflicts_and_dead_letters_visible_in_report():
    """Verify export status formatting surfaces active conflicts and dead letters."""
    status = {
        "conflicts": ["2026/10/2026-10-02T1200_user_6dh74a_test_p01.md"],
        "dead_letters": ["01M444NQWT0R5QZB9KE26DH74A"],
    }

    lines = format_export_status(status)
    formatted_text = "\n".join(lines)

    assert "GitHub Archive Export Status" in formatted_text
    assert "Active Conflicts (Human Edits Detected): 1" in formatted_text
    assert "2026/10/2026-10-02T1200_user_6dh74a_test_p01.md" in formatted_text
    assert "Dead-Lettered Threads (Push Protection): 1" in formatted_text
    assert "01M444NQWT0R5QZB9KE26DH74A" in formatted_text
