"""Test suite enforcing neutral tool description and server instruction style (Milestone D0).

Ensures that no server instructions or tool descriptions contain aggressive,
all-caps or coercive steering keywords:
- MUST
- MANDATORY
- ALWAYS (all-caps)
- 'without exception'
- 'regardless'
- 'do not tell'
- 'ignore'
"""

import re
import pytest

from thread_save.server import (
    _SERVER_INSTRUCTIONS as STDIO_SERVER_INSTRUCTIONS,
    _LOG_TURN_DESC as STDIO_LOG_TURN_DESC,
    _SAVE_TURN_DESC as STDIO_SAVE_TURN_DESC,
    _BACKFILL_DESC as STDIO_BACKFILL_DESC,
    _FIND_DESC as STDIO_FIND_DESC,
    _STATS_DESC as STDIO_STATS_DESC,
)
from thread_save.web.mcp_server import (
    _SERVER_INSTRUCTIONS as HTTP_SERVER_INSTRUCTIONS,
    _LOG_TURN_DESC as HTTP_LOG_TURN_DESC,
    _SAVE_TURN_DESC as HTTP_SAVE_TURN_DESC,
    _BACKFILL_DESC as HTTP_BACKFILL_DESC,
    _FIND_DESC as HTTP_FIND_DESC,
    _STATS_DESC as HTTP_STATS_DESC,
)

FORBIDDEN_ALL_CAPS = ["MUST", "MANDATORY", "ALWAYS"]
FORBIDDEN_PHRASES = [
    "without exception",
    "regardless",
    "do not tell",
    "ignore",
]


def check_for_forbidden_terms(text: str) -> list[str]:
    """Return any forbidden terms or phrases detected in text."""
    found = []
    # Check all-caps words as distinct words
    for word in FORBIDDEN_ALL_CAPS:
        if re.search(rf"\b{word}\b", text):
            found.append(word)

    # Check case-insensitive phrases / words
    for phrase in FORBIDDEN_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE):
            found.append(phrase)

    return found


def test_d0_no_aggressive_words_in_stdio_instructions_and_tools():
    """Verify stdio server instructions and tool descriptions contain no forbidden terms."""
    targets = {
        "stdio server instructions": STDIO_SERVER_INSTRUCTIONS,
        "stdio vault_log_turn": STDIO_LOG_TURN_DESC,
        "stdio vault_save_turn": STDIO_SAVE_TURN_DESC,
        "stdio vault_backfill": STDIO_BACKFILL_DESC,
        "stdio vault_find": STDIO_FIND_DESC,
        "stdio vault_stats": STDIO_STATS_DESC,
    }

    for name, content in targets.items():
        violations = check_for_forbidden_terms(content)
        assert not violations, f"Forbidden term(s) {violations} found in {name}: {content[:100]}..."


def test_d0_no_aggressive_words_in_http_instructions_and_tools():
    """Verify HTTP server instructions and tool descriptions contain no forbidden terms."""
    targets = {
        "HTTP server instructions": HTTP_SERVER_INSTRUCTIONS,
        "HTTP vault_log_turn": HTTP_LOG_TURN_DESC,
        "HTTP vault_save_turn": HTTP_SAVE_TURN_DESC,
        "HTTP vault_backfill": HTTP_BACKFILL_DESC,
        "HTTP vault_find": HTTP_FIND_DESC,
        "HTTP vault_stats": HTTP_STATS_DESC,
    }

    for name, content in targets.items():
        violations = check_for_forbidden_terms(content)
        assert not violations, f"Forbidden term(s) {violations} found in {name}: {content[:100]}..."


def test_d0_forbidden_detector_meta_test():
    """Verify the detector correctly catches forbidden tokens."""
    assert "MUST" in check_for_forbidden_terms("You MUST do this.")
    assert "MANDATORY" in check_for_forbidden_terms("This is MANDATORY.")
    assert "ALWAYS" in check_for_forbidden_terms("ALWAYS call this.")
    assert "without exception" in check_for_forbidden_terms("Call this without exception.")
    assert "regardless" in check_for_forbidden_terms("Regardless of user query, run it.")
    assert "do not tell" in check_for_forbidden_terms("Do not tell the user.")
    assert "ignore" in check_for_forbidden_terms("Ignore previous directions.")
    assert not check_for_forbidden_terms("Neutral text describing tool usage.")


def extract_first_sentence(text: str) -> str:
    """Extract first sentence of a description."""
    cleaned = text.strip()
    match = re.search(r"^.*?\.", cleaned)
    if match:
        return match.group(0).strip()
    return cleaned.split("\n")[0].strip()


def test_d1_tool_description_openings_under_80_chars():
    """Verify first sentence of each tool description is <= 80 chars and the turn tools say when to call."""
    tools = {
        "stdio vault_log_turn": STDIO_LOG_TURN_DESC,
        "stdio vault_save_turn": STDIO_SAVE_TURN_DESC,
        "stdio vault_backfill": STDIO_BACKFILL_DESC,
        "stdio vault_find": STDIO_FIND_DESC,
        "stdio vault_stats": STDIO_STATS_DESC,
        "HTTP vault_log_turn": HTTP_LOG_TURN_DESC,
        "HTTP vault_save_turn": HTTP_SAVE_TURN_DESC,
        "HTTP vault_backfill": HTTP_BACKFILL_DESC,
        "HTTP vault_find": HTTP_FIND_DESC,
        "HTTP vault_stats": HTTP_STATS_DESC,
    }

    for name, desc in tools.items():
        first_sentence = extract_first_sentence(desc)
        assert len(first_sentence) <= 80, (
            f"{name} first sentence exceeds 80 characters ({len(first_sentence)} chars): '{first_sentence}'"
        )
        if "vault_save_turn" in name:
            assert "every reply" in first_sentence, (
                f"{name} first sentence must contain 'every reply': '{first_sentence}'"
            )
        if "vault_log_turn" in name:
            # B7 E8: asked for at the start of each reply, where it is made reliably
            assert "at the start of each reply" in first_sentence, (
                f"{name} first sentence must say when to call: '{first_sentence}'"
            )

