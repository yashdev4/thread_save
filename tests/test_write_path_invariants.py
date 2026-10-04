import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from thread_save.config import VaultConfig
from thread_save.models import Fidelity
from thread_save.service import TurnService
from thread_save.storage.formatter import parse_page
from thread_save.storage.orphan import reap_orphans
from thread_save.storage.path_resolver import ensure_vault_structure
from thread_save.storage.writer import FileStore


class TestWritePathInvariants(unittest.TestCase):
    def test_sticky_pages(self):
        """Sticky pagination (W-5 §3.5, W-6): turns never change page once assigned."""
        async def run():
            with tempfile.TemporaryDirectory() as td:
                # Set small page limits to trigger rollover
                config = VaultConfig(
                    vault_root=Path(td),
                    page_max_turns=4,
                    page_max_bytes=10_000,
                )
                ensure_vault_structure(config)
                store = FileStore(config)
                svc = TurnService(store, config=config)

                # Turn 1
                r1 = await svc.save_turn(user_query="Q1", title_hint="Sticky Test")
                tid = r1["thread_id"]

                # Turn 2
                await svc.save_turn(
                    user_query="Q2",
                    prev_response="R1",
                    prev_user_anchor="Q1",
                    thread_id=tid,
                )

                # Turn 3 triggers rollover to page 2
                await svc.save_turn(
                    user_query="Q3",
                    prev_response="R2",
                    prev_user_anchor="Q2",
                    thread_id=tid,
                )

                # Check page files
                all_pages = sorted(list(Path(td).rglob("*_p[0-9][0-9].md")))
                self.assertGreaterEqual(len(all_pages), 2)

                # Parse page 1 and page 2
                m1, turns_p1 = parse_page(all_pages[0].read_text(encoding="utf-8"))
                m2, turns_p2 = parse_page(all_pages[1].read_text(encoding="utf-8"))

                # In page 1, turn 1 and turn 2 are present
                p1_indices = {t.turn_index for t in turns_p1}
                self.assertIn(1, p1_indices)
                self.assertIn(2, p1_indices)

                # In page 2, turn 3 is present
                p2_indices = {t.turn_index for t in turns_p2}
                self.assertIn(3, p2_indices)

                # Now backfill a large payload into Turn 1 (Page 1)
                await svc.backfill(
                    thread_id=tid,
                    turns=[{"n": 1, "assistant_response": "Very large updated response" * 50}],
                )

                # Verify turn 2 did NOT get pushed into page 2
                m1_after, turns_p1_after = parse_page(all_pages[0].read_text(encoding="utf-8"))
                m2_after, turns_p2_after = parse_page(all_pages[1].read_text(encoding="utf-8"))

                self.assertIn(1, {t.turn_index for t in turns_p1_after})
                self.assertIn(2, {t.turn_index for t in turns_p1_after})
                self.assertNotIn(2, {t.turn_index for t in turns_p2_after})

        asyncio.run(run())

    def test_orphan_reaper(self):
        """Orphan reaper (W2, §3.1): reaps single-turn thread > 24h old with multi-turn twin."""
        async def run():
            with tempfile.TemporaryDirectory() as td:
                config = VaultConfig(vault_root=Path(td))
                ensure_vault_structure(config)
                store = FileStore(config)
                svc = TurnService(store, config=config)

                now = datetime.now(timezone.utc)
                old_time = now - timedelta(hours=25)

                # Create twin thread that continued (turn 1 + turn 2)
                r_twin = await svc.save_turn(user_query="Identical query", title_hint="Twin")
                twin_id = r_twin["thread_id"]
                await svc.save_turn(
                    user_query="Second query",
                    prev_response="Twin reply",
                    prev_user_anchor="Identical query",
                    thread_id=twin_id,
                )
                twin_entry = store._registry.get(twin_id)
                twin_entry.meta.created = old_time

                # Create orphan thread (turn 1 only, never continued)
                r_orphan = await svc.save_turn(user_query="Identical query", title_hint="Orphan")
                orphan_id = r_orphan["thread_id"]
                orphan_entry = store._registry.get(orphan_id)
                orphan_entry.meta.created = old_time + timedelta(seconds=15)  # within 2 minutes

                # Reap orphans
                reaped_count = await reap_orphans(store, "default")
                self.assertEqual(reaped_count, 1)

                # Assert orphan was deleted and twin remained
                self.assertIsNone(store._registry.get(orphan_id))
                self.assertIsNotNone(store._registry.get(twin_id))

        asyncio.run(run())

    def test_v1_legacy_file_handling(self):
        """v1 legacy file handling: parses schema_version 1 files without nonces."""
        legacy_content = (
            "---\n"
            '"schema_version": 1\n'
            '"thread_id": "01JLEGACYTHREAD0000000000001"\n'
            '"title": "Legacy Thread"\n'
            '"slug": "legacy-thread"\n'
            '"account": "default"\n'
            '"client": "claude-desktop"\n'
            '"created": "2026-09-01T12:00:00+00:00"\n'
            '"updated": "2026-09-01T12:00:00+00:00"\n'
            '"page": 1\n'
            '"turn_count": 1\n'
            '"turn_range": [1, 1]\n'
            '"bytes": 100\n'
            "---\n\n"
            "# Legacy Thread\n\n"
            "<!-- turn i=1 role=user fidelity=verbatim chars=20 hash=deadbeef -->\n"
            "## User\n\n"
            "Legacy user query\n"
            "<!-- /turn i=1 -->\n"
        )

        meta, turns = parse_page(legacy_content)
        self.assertEqual(meta.schema_version, 1)
        self.assertEqual(meta.thread_id, "01JLEGACYTHREAD0000000000001")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].turn_index, 1)
        self.assertEqual(turns[0].body, "Legacy user query")


if __name__ == "__main__":
    unittest.main()
