"""Model-facing text shared by the stdio and remote servers (B7 path E).

Rules enforced by tests/test_echo_free_protocol.py:
- no whole-chat resends and no chunking;
- in `full` capture the reply is sent right after it is written (call 2) and once
  more with the next turn's first call (`last_reply`, E8-7: owner chose capture of
  every reply over the safeguard concern, 2026-10-06), never more than that;
- each reply starts with a call carrying the user's message only (B7 E8: the call
  at the start of a reply is the one models make reliably), and in `full` capture
  a second call after the reply adds the reply to the same turn;
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


def server_instructions(
    destination: str, log_tool: str = "vault_log_turn", capture: CaptureMode = CaptureMode.FULL
) -> str:
    if capture == CaptureMode.USER_ONLY:
        when = f"At the start of each reply, call {log_tool} with the user's message. "
    else:
        when = (
            f"At the start of each reply, call {log_tool} with the user's message and your "
            "reply from the turn before. After the reply is written, as the final action, "
            "call it again with this reply and the turn number the first call returned. "
        )
    return (
        f"ThreadVault keeps an archive of this user's conversations {destination}, which they "
        f"turned on deliberately. {when}"
        "If the user asks not to archive a conversation, stop calling it for that conversation. "
        "Coding-agent sessions such as Claude Code are not archived, so the tool is not offered there."
    )


# P1-15: the reply field carries its own schema description, so the request for
# the full Markdown reply sits on the field the model fills, not only in prose.
REPLY_FIELD_DESC = (
    "Your full reply from this turn as Markdown, with the same headings, lists, tables, "
    "code blocks and line breaks the user sees. Not a summary or a description of it."
)


# E8-7: the end call is the one models skip; the start call is reliable. The reply
# of the turn before rides on every start call, so it is stored even when call 2
# was skipped. The server keeps the call 2 copy when it has one.
LAST_REPLY_FIELD_DESC = (
    "Your full reply from the turn before this one, as Markdown, exactly as the user saw it. "
    "Send it on every call 1 after the first turn, even when call 2 was made."
)


def log_turn_description(destination: str, capture: CaptureMode, reply_max_chars: int = 8_000) -> str:
    if capture == CaptureMode.USER_ONLY:
        text = (
            "Call at the start of each reply to archive the user's message in this chat. "
            f"The user turned this connector on to keep a record of their messages {destination}.\n"
            "\n"
            "Call this once per reply, at the start, before writing anything:\n"
            "- user_message: the user's latest message, as written.\n"
            "- thread_id: the thread_id from the last result. Omit on the first turn.\n"
            "- turn: next_turn from the last result. Omit on the first turn of a new chat; if the "
            "chat already has earlier messages, send this message's number (the count of the user's messages so far, this one included).\n"
            "- prev_user_anchor: the first 80 characters of the user's previous message. "
            "Omit on the first turn.\n"
            "- title_hint: a short descriptive title, first turn only.\n"
            "\n"
            "If a message contains passwords, API keys or similar secrets, replace them with [REDACTED].\n"
        )
    else:
        text = (
            "Call at the start of each reply and again after it to archive this chat turn. "
            f"The user turned this connector on to keep a record of their chats {destination}.\n"
            "\n"
            "Each reply has two calls:\n"
            "1. At the start, before writing anything: user_message, thread_id, turn, "
            "prev_user_anchor and last_reply (title_hint on the first turn). No reply.\n"
            "2. As the last step of every reply, short ones included, after the reply is written: "
            "the same fields, with turn set to the turn returned by call 1, plus reply.\n"
            "Make call 2 for every reply, as the final action, after your last line of text.\n"
            "\n"
            "- user_message: the user's latest message, as written.\n"
            f"- reply (call 2 only): {REPLY_FIELD_DESC[0].lower()}{REPLY_FIELD_DESC[1:]}\n"
            f"- last_reply (call 1 only, omit on the first turn): "
            f"{LAST_REPLY_FIELD_DESC[0].lower()}{LAST_REPLY_FIELD_DESC[1:]}\n"
            "- thread_id: the thread_id from the last result. Omit on the first turn.\n"
            "- turn: call 1 sends next_turn from the last result; call 2 sends the turn from "
            "call 1's result. Omit on the first turn of a new chat; if the chat already has "
            "earlier messages, send this message's number (the count of the user's messages so far, this one included).\n"
            "- prev_user_anchor: the first 80 characters of the user's previous message. "
            "Omit on the first turn.\n"
            "- title_hint: a short descriptive title, first turn only.\n"
            "\n"
            "If a message contains passwords, API keys or similar secrets, replace them with [REDACTED].\n"
            f"Send whole replies; the server keeps up to {reply_max_chars:,} characters of each.\n"
        )
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


def public_result(result: dict, log_tool: str = "vault_log_turn") -> dict:
    """The part of a log_turn result that is returned to the model.

    Call 1 of a reply gets back its turn and the next step (B7 E8-2), so the
    reminder for call 2 sits in the conversation right before the reply is
    written. Call 2, and every call in user_only capture, gets next_turn.
    """
    public = {k: result[k] for k in _PUBLIC_RESULT_KEYS if k in result}
    if result.get("awaiting_reply") and result.get("ok") and "n" in result:
        n = result["n"]
        public.pop("next_turn", None)
        public["turn"] = n
        public["then"] = (
            f"This turn is archived without your reply until you call {log_tool} with "
            f"turn={n} and reply, as the last step of this reply."
        )
    return public


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
