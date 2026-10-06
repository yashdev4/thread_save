"""Model-facing text shared by the stdio and remote servers (B7 path E).

Rules enforced by tests/test_echo_free_protocol.py:
- the model is never asked for earlier output of its own (no previous replies,
  no whole-chat resends, no chunking to avoid shortening);
- the reply is sent once, right after it is written, and only in `full` capture;
- each transport states its real destination (B1 §6.1).
"""

from __future__ import annotations

from thread_save.config import CaptureMode

DEST_LOCAL = "as markdown files on this computer"
DEST_REMOTE = "in their ThreadVault account"

# Tool names per transport. Claude Desktop can run the local stdio server and the
# deployed connector side by side; identical names let one server receive the other's
# calls (P1-13), so the local server uses its own names.
LOCAL_TOOL_NAMES = {
    "log_turn": "vault_local_log_turn",
    "save_turn": "vault_local_save_turn",
    "backfill": "vault_local_backfill",
    "find": "vault_local_find",
    "stats": "vault_local_stats",
}
REMOTE_TOOL_NAMES = {
    "log_turn": "vault_log_turn",
    "save_turn": "vault_save_turn",
    "backfill": "vault_backfill",
    "find": "vault_find",
    "stats": "vault_stats",
}

# Keys of a vault_log_turn result that the model sees (I-5: tiny, data-only)
_PUBLIC_RESULT_KEYS = ("ok", "thread_id", "next_turn", "paused", "tombstoned", "code", "retryable")


def server_instructions(destination: str, log_tool: str = "vault_log_turn") -> str:
    return (
        f"ThreadVault keeps an archive of this user's conversations {destination}, which they "
        f"turned on deliberately. After finishing each reply, call {log_tool} as described "
        "in its tool description. If the user asks not to archive a conversation, stop calling "
        "it for that conversation. Coding-agent sessions such as Claude Code are not archived, "
        "so the tool is not offered there."
    )


def log_turn_description(destination: str, capture: CaptureMode) -> str:
    if capture == CaptureMode.USER_ONLY:
        record = "a record of their messages"
        reply_line = ""
    else:
        record = "a record of their chats"
        reply_line = "- reply: the reply you just gave in this turn, as shown to the user.\n"
    text = (
        "Call after finishing each reply to archive this chat turn for the user. "
        f"The user turned this connector on to keep {record} {destination}.\n"
        "\n"
        "Call this once per reply, as the last step:\n"
        "- user_message: the user's latest message, as written.\n"
        f"{reply_line}"
        "- thread_id: the thread_id from the last result. Omit on the first turn.\n"
        "- turn: next_turn from the last result. Omit on the first turn.\n"
        "- prev_user_anchor: the first 80 characters of the user's previous message. "
        "Omit on the first turn.\n"
        "- title_hint: a short descriptive title, first turn only.\n"
        "\n"
        "If a message contains passwords, API keys or similar secrets, replace them with [REDACTED].\n"
    )
    if capture != CaptureMode.USER_ONLY:
        text += "The server shortens long replies, so send the reply once as it is.\n"
    text += (
        "If the user asks not to archive this conversation, stop calling this tool for the rest of it.\n"
        "Repeated calls are safe and never create duplicate entries."
    )
    return text


BACKFILL_DESC = (
    "Add the user's own messages for earlier turns missing from the archive. "
    "Use only when the user asks for it. Up to 10 turns per call; turns already archived "
    "are left unchanged, so repeating a call is safe."
)


def public_result(result: dict) -> dict:
    """The part of a log_turn result that is returned to the model."""
    return {k: result[k] for k in _PUBLIC_RESULT_KEYS if k in result}


def user_messages_only(turns: list[dict]) -> list[dict]:
    """vault_backfill takes user text only; model output is never accepted (E-retire)."""
    cleaned = []
    for t in turns:
        if not isinstance(t, dict) or "n" not in t:
            continue
        msg = t.get("user_message") or t.get("user_query")
        if msg:
            cleaned.append({"n": t["n"], "user_query": msg})
    return cleaned
