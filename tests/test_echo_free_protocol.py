"""B7 E1: everything the model is shown must be echo-free.

The model-side `reasoning_extraction` safeguard was seen to stop long chats in
which the connector asked the model to re-send its own earlier output
(03-issues.md P1-11). This test reads the *advertised* surface of both servers,
in both capture modes (server instructions, tool descriptions and every
parameter description in the input schemas) and fails on any wording that asks
for earlier model output, whole-chat resends, chunk loops or reasoning.
"""

import asyncio
import json
import re
import subprocess
import sys
import textwrap

import pytest

from thread_save.server import _SAVE_TURN_DESC as LEGACY_STDIO_DESC
from thread_save.tools.descriptions import LOCAL_TOOL_NAMES, REMOTE_TOOL_NAMES
from thread_save.web.mcp_server import _SAVE_TURN_DESC as LEGACY_HTTP_DESC

BANNED = [
    r"previous reply",
    r"prev_response",
    r"verbatim",
    r"transcript",
    r"whole (chat|conversation)",
    r"all turns",
    r"instead of shortening",
    r"in parts",
    r"chunk",
    r"reasoning",
    r"thinking",
    r"chain of thought",
    r"silently",
]

# Collect the advertised surface in a fresh interpreter per (transport, capture),
# because the capture mode is read when the server module is imported.
_COLLECT = textwrap.dedent('''
    import asyncio, json, os, sys, tempfile
    os.environ["THREAD_SAVE_VAULT_ROOT"] = tempfile.mkdtemp(prefix="tv_echo_")
    transport = sys.argv[1]
    if transport == "stdio":
        import thread_save.server as mod
        server, instructions = mod.mcp, mod._SERVER_INSTRUCTIONS
    else:
        from thread_save.config import load_config
        from thread_save.service import TurnService
        from thread_save.storage.writer import FileStore
        from thread_save.web.mcp_server import create_http_mcp_server, _server_instructions
        cfg = load_config()
        server = create_http_mcp_server(TurnService(FileStore(cfg), config=cfg))
        instructions = _server_instructions(cfg.capture)
    async def main():
        tools = []
        for t in await server.list_tools():
            tools.append({"name": t.name, "description": t.description or "",
                          "schema": t.input_schema})
        # the stdio server points sys.stdout at stderr when imported
        print(json.dumps({"instructions": instructions, "tools": tools}), file=sys.__stdout__, flush=True)
    asyncio.run(main())
''')


def _surface(transport: str, capture: str, legacy: str = "") -> dict:
    env = {**__import__("os").environ, "THREADVAULT_CAPTURE": capture,
           "THREADVAULT_LEGACY_SAVE_TURN": legacy}
    out = subprocess.run(
        [sys.executable, "-c", _COLLECT, transport],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def _texts(surface: dict) -> list[tuple[str, str]]:
    texts = [("server instructions", surface["instructions"])]
    for tool in surface["tools"]:
        texts.append((f"{tool['name']} description", tool["description"]))
        texts.append((f"{tool['name']} schema", json.dumps(tool["schema"])))
    return texts


def _banned_hits(text: str) -> list[str]:
    return [b for b in BANNED if re.search(b, text, re.IGNORECASE)]


CASES = [(t, c) for t in ("stdio", "http") for c in ("full", "user_only")]
NAMES = {"stdio": LOCAL_TOOL_NAMES, "http": REMOTE_TOOL_NAMES}


@pytest.mark.parametrize("transport,capture", CASES)
def test_advertised_surface_is_echo_free(transport, capture):
    surface = _surface(transport, capture)
    for where, text in _texts(surface):
        assert not _banned_hits(text), f"{transport}/{capture} {where}: {_banned_hits(text)}"


@pytest.mark.parametrize("transport,capture", CASES)
def test_tool_set_and_schema_shape(transport, capture):
    surface = _surface(transport, capture)
    names = NAMES[transport]
    tools = {t["name"]: t for t in surface["tools"]}
    assert set(tools) == {names["log_turn"], names["backfill"], names["find"], names["stats"]}
    props = set(tools[names["log_turn"]]["schema"]["properties"])
    expected = {"user_message", "thread_id", "turn", "prev_user_anchor", "title_hint"}
    if capture == "full":
        expected |= {"reply", "last_reply"}
    assert props == expected  # E-floor: user_only cannot even carry a reply


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_reply_field_asks_for_the_formatted_reply(transport):
    """P1-15: the field the model fills says what to put in it, on both transports."""
    surface = _surface(transport, "full")
    tool = next(t for t in surface["tools"] if t["name"] == NAMES[transport]["log_turn"])
    field = tool["schema"]["properties"]["reply"].get("description", "")
    for text in (field, tool["description"]):
        assert "full reply" in text and "Markdown" in text and "Not a summary" in text
    assert "shortens" not in tool["description"]
    assert "8,000 characters" in tool["description"]


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_destination_is_stated_truthfully(transport):
    surface = _surface(transport, "full")
    log_name = NAMES[transport]["log_turn"]
    desc = next(t["description"] for t in surface["tools"] if t["name"] == log_name)
    assert log_name in surface["instructions"]
    for text in (surface["instructions"], desc):
        if transport == "stdio":
            assert "markdown files on this computer" in text
            assert "ThreadVault account" not in text
        else:
            assert "ThreadVault account" in text
            assert "on this computer" not in text


@pytest.mark.parametrize("transport,capture", CASES)
def test_call_at_the_start_of_each_reply(transport, capture):
    """B7 E8 (P1-16): the call is asked for at the start of the reply, where models
    make it reliably. In full capture a second call after the reply adds the reply."""
    surface = _surface(transport, capture)
    log_name = NAMES[transport]["log_turn"]
    desc = next(t["description"] for t in surface["tools"] if t["name"] == log_name)
    instr = surface["instructions"]
    assert f"At the start of each reply, call {log_name} with the user's message" in instr
    assert desc.startswith("Call at the start of each reply")
    if capture == "full":
        # E8-7: call 1 also carries the reply from the turn before (last_reply)
        assert "your reply from the turn before" in instr
        assert "as the final action, call it again with this reply" in instr
        assert "Each reply has two calls" in desc and "turn returned by call 1" in desc
    else:
        assert "again" not in instr and "again" not in desc
        assert "reply (call 2" not in desc
    assert "after finishing each reply" not in (instr + desc).lower()


def test_legacy_tool_is_listed_only_when_enabled():
    surface = _surface("stdio", "full", legacy="on")
    assert LOCAL_TOOL_NAMES["save_turn"] in {t["name"] for t in surface["tools"]}


def test_local_and_deployed_tool_names_never_overlap():
    """P1-13: Claude Desktop runs both servers side by side. A shared name let the
    local process receive calls meant for the deployed connector."""
    local = {t["name"] for t in _surface("stdio", "full", legacy="on")["tools"]}
    remote = {t["name"] for t in _surface("http", "full", legacy="on")["tools"]}
    assert local and remote
    assert local.isdisjoint(remote), local & remote


def test_detector_catches_the_old_wording():
    """The pre-B7 descriptions are exactly what this guard exists to stop."""
    for legacy in (LEGACY_STDIO_DESC, LEGACY_HTTP_DESC):
        hits = _banned_hits(legacy)
        assert "prev_response" in hits
        assert "instead of shortening" in hits
        assert r"whole (chat|conversation)" in hits
