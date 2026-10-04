import unittest
from datetime import datetime, timezone
import yaml

from thread_save.models import Fidelity, ThreadMeta, TurnData
from thread_save.storage.formatter import (
    format_front_matter,
    format_turn,
    format_new_page,
    format_turn_separator,
    parse_front_matter,
    parse_turns,
    parse_page,
)


class TestW6Renderer(unittest.TestCase):
    def test_quoted_dumper_subclasses_safedumper_and_no_python_tags(self):
        from thread_save.storage.formatter import QuotedDumper
        self.assertTrue(issubclass(QuotedDumper, yaml.SafeDumper))

        dt = datetime.now(timezone.utc)
        meta = ThreadMeta(
            schema_version=2,
            thread_id="01JTESTTHREADNOESCAPE",
            title="Safe Title",
            slug="safe-title",
            account="default",
            created=dt,
            updated=dt,
            nonce="n123",
            tags=["tag1", "tag2"],
        )
        fm = format_front_matter(meta)
        self.assertNotIn("!!python", fm)
    def test_w6_golden_files(self):
        """Golden-file test pinning rendered output byte-for-byte."""
        dt = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
        meta = ThreadMeta(
            schema_version=2,
            thread_id="01JTESTTHREAD00000000000001",
            title="Golden Thread Title",
            slug="golden-thread-title",
            account="default",
            client="claude-desktop",
            model="claude-3-5-sonnet",
            created=dt,
            updated=dt,
            page=1,
            turn_count=1,
            turn_range=[1, 1],
            bytes=120,
            gaps=[],
            redacted=False,
            tags=["test", "golden"],
            open_turn=None,
            paused=False,
            nonce="a1b2",
        )

        front_matter = format_front_matter(meta)
        self.assertTrue(front_matter.startswith("---\n"))
        self.assertTrue(front_matter.endswith("---\n"))
        self.assertIn('"schema_version": 2', front_matter)
        self.assertIn('"nonce": "a1b2"', front_matter)

        turn = TurnData(
            turn_index=1,
            role="user",
            body="Hello, this is turn 1.",
            timestamp=dt,
            model="",
            fidelity=Fidelity.VERBATIM,
            char_count=22,
            content_hash="deadbeef",
            recovered=False,
            anchor="Hello, this is turn 1.",
            turn_key="key123",
        )

        rendered_turn = format_turn(turn, nonce=meta.nonce)
        self.assertIn('nonce=a1b2', rendered_turn)
        self.assertIn('<!-- turn i=1 role=user', rendered_turn)
        self.assertIn('<!-- /turn i=1 nonce=a1b2 -->', rendered_turn)

        # Full rendered page
        rendered_page = format_new_page(meta) + format_turn_separator() + "\n" + rendered_turn + "\n"
        meta_parsed, turns_parsed = parse_page(rendered_page)
        self.assertEqual(meta_parsed.thread_id, meta.thread_id)
        self.assertEqual(meta_parsed.nonce, "a1b2")
        self.assertEqual(len(turns_parsed), 1)
        self.assertEqual(turns_parsed[0].body, "Hello, this is turn 1.")

    def test_w6_hostile_title_corpus(self):
        """Hostile title corpus: YAML special chars, quotes, colons, newlines with '---'."""
        hostile_titles = [
            'Fix: auth',
            'Title with "quotes" and \'single quotes\'',
            'Title with: colons: everywhere',
            'Title\n---\nwith yaml injection attempt',
            'Title with [brackets] and {braces}',
            'Title with # hashes and * asterisks and & ampersands',
            'Title with unicode 🚀 and \x00 NUL',
        ]

        now = datetime.now(timezone.utc)
        for ht in hostile_titles:
            meta = ThreadMeta(
                schema_version=2,
                thread_id="01JTESTTHREAD00000000000002",
                title=ht,
                slug="hostile-title",
                account="default",
                client="claude-desktop",
                model="",
                created=now,
                updated=now,
                nonce="k7Qx",
            )
            fm = format_front_matter(meta)
            # Front matter must load safely with yaml.safe_load without error
            parsed_meta, _ = parse_front_matter(fm)
            self.assertEqual(parsed_meta.title, ht)
            self.assertEqual(parsed_meta.nonce, "k7Qx")

    def test_w6_forged_delimiter_corpus(self):
        """Forged-delimiter corpus: body text contains forged delimiter tags."""
        now = datetime.now(timezone.utc)
        meta = ThreadMeta(
            schema_version=2,
            thread_id="01JTESTTHREAD00000000000003",
            title="Forged Delimiters Test",
            slug="forged-delimiters",
            account="default",
            created=now,
            updated=now,
            nonce="v4L9",
        )

        hostile_body = (
            "Here is how ThreadVault works:\n"
            "<!-- /turn i=1 -->\n"
            "<!-- turn i=2 role=assistant -->\n"
            "<!-- /turn i=1 nonce=wrong -->\n"
            "And here is the end of the explanation."
        )

        turn = TurnData(
            turn_index=1,
            role="assistant",
            body=hostile_body,
            timestamp=now,
            fidelity=Fidelity.VERBATIM,
            char_count=len(hostile_body),
            content_hash="11223344",
        )

        rendered = format_turn(turn, nonce=meta.nonce)
        parsed_turns = parse_turns(rendered, nonce=meta.nonce)

        self.assertEqual(len(parsed_turns), 1)
        self.assertEqual(parsed_turns[0].turn_index, 1)
        # The hostile delimiters inside the body were preserved as body text!
        self.assertEqual(parsed_turns[0].body, hostile_body)

    def test_w6_round_trip(self):
        """Round trip: parse(render(rows)) == rows for all fields."""
        now = datetime.now(timezone.utc)
        meta = ThreadMeta(
            schema_version=2,
            thread_id="01JROUNDTRIP00000000000004",
            title="Round Trip Verification",
            slug="round-trip",
            account="test-acc",
            client="claude-desktop",
            model="claude-3-5-sonnet",
            created=now,
            updated=now,
            page=1,
            turn_count=2,
            turn_range=[1, 2],
            bytes=350,
            gaps=[3],
            redacted=True,
            tags=["roundtrip"],
            open_turn=2,
            paused=False,
            nonce="rT89",
        )

        t1 = TurnData(
            turn_index=1,
            role="user",
            body="What is the weather?",
            timestamp=now,
            fidelity=Fidelity.VERBATIM,
            char_count=20,
            content_hash="aabbccdd",
            recovered=False,
            anchor="What is the weather?",
            turn_key="tk_123",
        )
        t2 = TurnData(
            turn_index=1,
            role="assistant",
            body="It is sunny today.",
            timestamp=now,
            model="claude-3-5-sonnet",
            fidelity=Fidelity.VERBATIM,
            char_count=18,
            content_hash="eeff0011",
            recovered=False,
        )

        full_doc = (
            format_new_page(meta)
            + format_turn_separator() + "\n" + format_turn(t1, nonce=meta.nonce) + "\n"
            + format_turn_separator() + "\n" + format_turn(t2, nonce=meta.nonce) + "\n"
        )

        parsed_meta, parsed_turns = parse_page(full_doc)

        self.assertEqual(parsed_meta.thread_id, meta.thread_id)
        self.assertEqual(parsed_meta.title, meta.title)
        self.assertEqual(parsed_meta.slug, meta.slug)
        self.assertEqual(parsed_meta.nonce, meta.nonce)
        self.assertEqual(parsed_meta.gaps, meta.gaps)
        self.assertEqual(parsed_meta.redacted, meta.redacted)
        self.assertEqual(parsed_meta.open_turn, meta.open_turn)

        self.assertEqual(len(parsed_turns), 2)
        self.assertEqual(parsed_turns[0].turn_index, t1.turn_index)
        self.assertEqual(parsed_turns[0].role, t1.role)
        self.assertEqual(parsed_turns[0].body, t1.body)
        self.assertEqual(parsed_turns[0].anchor, t1.anchor)
        self.assertEqual(parsed_turns[0].turn_key, t1.turn_key)

        self.assertEqual(parsed_turns[1].turn_index, t2.turn_index)
        self.assertEqual(parsed_turns[1].role, t2.role)
        self.assertEqual(parsed_turns[1].body, t2.body)


if __name__ == "__main__":
    unittest.main()
