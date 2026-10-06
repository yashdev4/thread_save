"""B3 WD / P1-15: a turn body survives write -> parse unchanged.

format_turn wraps a body in a fixed frame (role heading, blank line, attachment
markers). Parsing must remove only that frame: the body's first-line
indentation, blank lines, Markdown and bracketed lines of its own must come back
as written, because restore (B6 L1), fsck and the Postgres migration all read
pages through the parser.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile

from hypothesis import assume, given, settings, strategies as st
import pytest

from thread_save.config import load_config
from thread_save.models import Attachment, Fidelity, TurnData
from thread_save.security.idempotency import canonical_v1
from thread_save.service import TurnService
from thread_save.storage.formatter import format_turn, parse_page, parse_turns
from thread_save.storage.writer import FileStore

NONCE = "ab12"

MARKDOWN_REPLY = (
    "## Summary\n"
    "\n"
    "**Key points:**\n"
    "\n"
    "1. First item\n"
    "   - nested bullet\n"
    "2. Second item\n"
    "\n"
    "```python\n"
    "def f():\n"
    "    return 1\n"
    "```\n"
    "\n"
    "| a | b |\n"
    "|---|---|\n"
    "| 1 | 2 |\n"
    "\n"
    "> quoted line"
)

BODIES = {
    "markdown": MARKDOWN_REPLY,
    "indented first line": "    indented code block\nnext line",
    "body starts with a heading": "## Heading in the reply\n\ntext",
    "bracketed lines": '[image: "chart" — not archived]\n[x] done\n[1]: https://example.com',
    "inner blank lines": "a\n\n\n\nb",
    "trailing spaces": "line with trailing spaces   ",
    "single line": "It is sunny today.",
}


def _turn(body: str, attachments: list | None = None) -> TurnData:
    return TurnData(
        turn_index=1,
        role="assistant",
        body=body,
        timestamp=datetime.now(timezone.utc),
        fidelity=Fidelity.REPORTED,
        char_count=len(body),
        content_hash="00000000",
        attachments=attachments or [],
    )


def _round_trip(turn: TurnData) -> str:
    text = format_turn(turn, nonce=NONCE) + "\n"
    (parsed,) = parse_turns(text, nonce=NONCE)
    return parsed.body


@pytest.mark.parametrize("body", BODIES.values(), ids=BODIES.keys())
def test_body_round_trips_exactly(body):
    assert _round_trip(_turn(body)) == body


def test_attachment_markers_are_frame_not_body():
    att = Attachment(type="image", title="chart")
    body = '[file: "notes" — not archived]\nreal body line'
    assert _round_trip(_turn(body, [att])) == body


def test_crlf_page_parses_like_lf():
    text = (format_turn(_turn("    a\n\nb"), nonce=NONCE) + "\n").replace("\n", "\r\n")
    (parsed,) = parse_turns(text, nonce=NONCE)
    assert parsed.body == "    a\n\nb"


_line = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="<>"),
    max_size=40,
)


@settings(max_examples=300, deadline=None)
@given(st.lists(_line, min_size=1, max_size=12))
def test_any_body_round_trips_canonically(lines):
    body = "\n".join(lines)
    assume(body.strip())  # canonical_v1 maps "" and "\n" differently; blank bodies are never stored
    assert canonical_v1(_round_trip(_turn(body))) == canonical_v1(body)


# ── Through the store: what the archive keeps and restores ────────────────

@pytest.fixture
def vault():
    path = Path(tempfile.mkdtemp(prefix="tv_roundtrip_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _svc(vault: Path) -> TurnService:
    cfg = replace(load_config(), vault_root=vault, default_account="tester")
    return TurnService(FileStore(cfg), config=cfg)


@pytest.mark.asyncio
async def test_logged_markdown_reply_is_stored_as_sent(vault):
    svc = _svc(vault)
    r = await svc.log_turn(user_message="summarise it", reply=MARKDOWN_REPLY)
    assert r["reply_fidelity"] == "reported" and r["reply_shape"] == "markdown"
    page = next(vault.rglob("*_p01.md")).read_text(encoding="utf-8")
    _, turns = parse_page(page)
    assert turns[1].body == MARKDOWN_REPLY


@pytest.mark.asyncio
async def test_restart_keeps_exact_reply_hash(vault):
    """After a restart the restored hash matches, so a Retry with the same reply is a no-op."""
    reply = "    indented first line\n\n- item"
    first = await _svc(vault).log_turn(user_message="q1", reply="a1")
    ctx = dict(thread_id=first["thread_id"], prev_user_anchor="q1")
    r2 = await _svc(vault).log_turn(user_message="q2", reply=reply, turn=2, **ctx)
    restarted = _svc(vault)  # fresh FileStore restores the thread from Markdown
    # No `turn`: a different turn_key, so only the restored body hash can make this a no-op
    again = await restarted.log_turn(user_message="q2", reply=reply, **ctx)
    assert (r2["n"], again["n"], again["action"]) == (2, 2, "no_op")
