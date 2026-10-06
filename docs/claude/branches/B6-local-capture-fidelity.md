# B6: Local capture fidelity (PROPOSED)

- **Source:** no plan document. Split off on 2026-10-05 from a field report: "in a new Claude Desktop chat with the local connector on, the `.md` file is created but has no content".
- **Parent:** B1 (lagged logging, MODE, telemetry) × B3 (FileStore write path, W-4/W-11). The fix also covers B5, which runs the same `FileStore` on Render.
- **Status:** ◐ **L0, L1 (A1), L2, L4 committed `bad9e0c` and deployed 2026-10-06 (`ad9b812`).** L5 repair and L6 live check open. L3 → B7.
- **Goal:** with the connector on, each chat produces **one** file that contains **every user message and every Claude reply**, including the last one, and survives app restarts.

## Setup under test (verified 2026-10-05)
| Item | Value |
|---|---|
| Client | Claude Desktop, Microsoft Store build: `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| Server | stdio `C:\Python314\python.exe -m thread_save.server` (editable install, so it runs the repo code) |
| Env | `THREAD_SAVE_VAULT_ROOT=…\vault_local`, `THREAD_SAVE_ACCOUNT=dhanshree` |
| Not used by Desktop | `%APPDATA%\Claude\claude_desktop_config.json` (points at another project, `threadvault/claude_mcp_server.py`); `scripts/run_local.ps1` HTTP server (used by Claude Code `threadvault-local`) |
| Logs | none. Store Desktop's `logs/` folder is empty, so the server's stderr is lost |

## Findings: what actually happens
The user's guess was "the tool isn't called after Claude's reply". **Half right.** The tool *is* called, at the start of each reply: `vault_local/_index/events.jsonl` has both calls of the `dhanshree` chat, with `ok:true`. It is never called *after* the reply. That is by design, and the switch meant to change it does nothing. Five causes:

| # | Cause | Effect the user sees | Evidence | Issue |
|---|---|---|---|---|
| RC1 | FileStore state is memory-only; nothing reloads it after a restart | Resumed chat → a new "Untitled Thread" file holding only the user's line + "[response pending]"; the previous reply is **dropped**; the old file's last reply stays pending forever | reproduced (2 turns → restart → 1 turn = 2 files, reply lost); `writer.py:386-398,483,505-507`; `service.py:322` | P0-4 |
| RC2 | Turn-start lagged logging; `MODE=both` is a no-op | The latest reply is never in the file; a one-message chat = user line + placeholder | `vault_local/dhanshree/…new-chat-beginning_p01.md` turn 2; `server.py:192` is the only reader of `mode` | P1-9 |
| RC3 | `_create_thread` writes the page outside the txn | Any failure (or a chunk-buffered first call) leaves a file with only front matter + `# Title`, i.e. literally "no content" | reproduced with an injected `OSError` and with `chunk_index=0,is_final=false`; `writer.py:454-456` | P1-10 |
| RC4 | Index blanks title/account/path; front matter `turn_range` stale | `vault_find` returns `title: ""`; front matter shows `turn_range: [1, 0]` | `threads.sqlite` row; `server.py:415-423`; `writer.py:443` | P2-12 |
| RC6 | The capture method itself: the model is asked to re-emit its own prior reply verbatim (and to chunk it rather than shorten it) | Resumed chats stop with "Sonnet 5.5's safeguards flagged this message … [reasoning_extraction]"; the teammate's full-transcript saver hit the same | user report 2026-10-05; `server.py:124,129-130`; public reports anthropics/claude-code#97503, #93584. **Inferred, not A/B-tested** | P1-11 |
| RC5 | Failures not logged; binding logged from the argument; stderr invisible | RC1 and RC3 cannot be seen in `events.jsonl` or `report.py` | `server.py:193,204-206` | P2-13 |

Not yet verified: whether other "new chats" reached the tool at all. Only **one** `dhanshree` chat appears in `events.jsonl`. If more chats were tried, the model either didn't call the tool or Desktop's tool-permission prompt wasn't approved. L0 makes this visible; L6 measures it.

## Sub-goals (proposed order)
| ID | Sub-goal | Fixes | Done when |
|---|---|---|---|
| ~~S0~~ | 🔀 **Dropped 2026-10-05** (owner: no connector-toggling diagnostics; the fix must be deterministic). RC6 moved to **B7**; measurement is B7 E7 | RC6 | — |
| L0 | **Observability** | RC5 | ✅ stdio writes `_index/server.log` (rotating 1 MB × 3); failed calls logged to `events.jsonl` with `code`; `binding`, `bytes`, `chunk` and `action` come from the result. Remote server still has no `EventLogger` (B1 §7) |
| L1 | **Restart-safe FileStore** (A1 lazy rehydrate) | RC1 | ✅ `FileStore._rehydrate` / `restore_thread_state`: on a registry miss (by id, or by `active.json` anchor) parse the thread's pages from `<account>/*/*/*_{short}_*_pNN.md`; page number from the **filename**; slot hashes recomputed from bodies; anchors rebuilt from the body (the header keeps only 40 chars); stubs restored as requested gaps. Account-scoped. `find` loads an account's threads once per process. GitHub offload restore now uses the same code. `tests/test_filestore_restart.py` (9) incl. resume by id, by anchor only, retry no-op, multi-page, other account cannot bind |
| L2 | **Atomic thread creation** | RC3 | ✅ `_create_thread` no longer writes; first page written at commit; failed first txn discards the thread; failed later txn restores page state + meta snapshot; a no-change txn does not touch the file. Chunk buffering unchanged (legacy path only) |
| L3 | 🔀 **Superseded by B7** (`vault_log_turn` at the end of each reply, no open slot) | RC2 | see B7 E2/E7 |
| L4 | **Index + front matter** | RC4 | ✅ index uses the real title/account/path, never overwrites with `""`, indexes replies; `vault_find` returns one hit per thread; front matter now has the page's own `page`, `prev` and `turn_range` (was always `page: 1`, `[1, 0]`). `turn_count` semantics unchanged |
| L5 | **Repair existing data:** one-shot `cli` that merges restart-split "Untitled Thread" files back into their parent by anchor/time, dry-run by default | RC1 legacy | dry-run lists the pairs; `--apply` leaves fsck clean |
| L6 | **Live Desktop check** (= B1 R7, local): 10 chats × 5 turns, with a Desktop restart in the middle of 3 of them | all | coverage report from L0 data; numbers recorded here |

## Alternatives

### For RC1: where the FileStore state comes from after a restart
| Option | How | Cost | Risk | Verdict |
|---|---|---|---|---|
| **A1 Lazy rehydrate from the Markdown** | On a registry miss, find `*_{short_id}_p*.md` (the short id is in every filename), `parse_page` every page, rebuild entry/page state/slots/anchors/gaps/`open_turn`. Anchor fallback rehydrates the `active.json` hit. | ~150 LOC + tests | parse must round-trip (WF already tests `parse(render(rows)) == rows`); a hand-edited file may not parse → treat as a new thread (split, never merge) | **Recommended.** Markdown stays the single source of truth, as B0 intended. Verified that `parse_page` recovers id, nonce, `open_turn`, anchors, `turn_key`s, fidelity and bodies from the live file |
| A2 Sidecar state JSON per thread | Write `_index/state/<id>.json` in the same commit | small | two sources of truth; drifts after manual edits or partial writes | Fallback only |
| A3 SQLite-canonical local store | Implement `Store` on SQLite (same schema ideas as PgStore), render Markdown from rows (X5 renderer, sticky pages) | 1–2 days | larger change; Markdown becomes an output, not the record | Best long-term fit with B2/B3 (W-1…W-11 by construction). Consider if A1 is outgrown |
| A4 Local Postgres | PgStore + exporter to `vault_local` | Postgres on every user machine | — | Rejected for Desktop users |

### For RC2: getting the reply Claude has just written
| Option | How | Cost per reply | Risk | Verdict |
|---|---|---|---|---|
| C1 **Make `both` real** | When `MODE=both`, the description adds: "after finishing the reply, call again with `current_response` = the reply". Start call unchanged. | +1 tool call; a trailing tool chip in the UI | model may skip the end call; then it degrades to today's behaviour (no loss vs now) | **Recommended if you accept the extra call.** Safe because the upsert is idempotent and `prev_response` on the next turn still repairs a skipped end call |
| C2 `turn_end` only | One call per reply, at the end, with `user_query` + `current_response` | 1 call (same as now) | if skipped, **both** halves of that turn are lost; late-join backfill partly repairs it | Cheaper, riskier. Decide from L6 data |
| C3 Keep turn-start; honest placeholder + "save this chat" | Placeholder says the reply is saved when the next message arrives; user can ask for a full save (backfill) | 0 | last reply missing by design | Minimum; no fidelity gain |
| C4 Client-side capture | Claude Code `Stop` hook reads the transcript and saves deterministically | 0 model calls | Claude Code only; Desktop has no hook | Future, for Claude Code users only |
| C5 **Capture outside the model** | Browser extension on claude.ai that reads the rendered chat and posts each finished turn to ThreadVault; or a scheduled import of the claude.ai data export | 0 model calls, no echo | new component; extension does not cover the Desktop app; export import is delayed (hours/days) | Removes RC2 **and** RC6 entirely. Larger direction change: would become its own branch (B7) |
| C6 **Low-echo model capture** | Keep the tool, but ask only for the user's message plus the **visible text of the reply just written**, sent once at the end; never earlier replies, never chunk-to-avoid-shortening, explicitly "no thinking or reasoning" | 1 call | still an echo; S0 decides if it is enough | Smallest change that may avoid RC6 |

**2026-10-05 update (MCP-only constraint):** C4 and C5 are rejected (outside MCP); C1 is rejected (doubles the echo); C6 became **B7 path A**; RC2 and RC6 are solved together in **B7 path E**.

RC6 changes the C1 recommendation: **C1 doubles the echo** (start call re-sends the previous reply, end call sends the current one). Do not ship C1 until S0 shows echoes are safe; C6 or C5 are the candidates if they are not.

## Decisions needed (owner)
1. RC1 approach: **A1** (recommended) / A2 / A3.
2. ~~RC2 approach~~ → decided in **B7** (path E recommended).
3. L5 repair of existing split files: yes / no (the only affected data today is in `vault_local`).

## Cross-branch effects
- **B1:** §4.3 open slot is now ⚠ (never closed for the last reply); §6.3 `MODE=both` is ⚠ (no-op); §7 telemetry is ⚠ (binding from the argument, failures unlogged).
- **B3:** W-4 and W-11 do not hold for FileStore across restarts or on failed creation.
- **B5:** Render runs FileStore. Every deploy or idle spin-down splits every active chat (RC1); L1 fixes production as well.

## Change log (newest first)
- 2026-10-06 · FIXED · L1 restore recomputed slot hashes from a lossy parse (first-line indent stripped), so a Retry after restart could rewrite an unchanged reply. Exact parse (B3 WD) fixes it; `test_turn_roundtrip.py::test_restart_keeps_exact_reply_hash`. Uncommitted.
- 2026-10-06 · REVISED · Status: L0–L4 shipped in `bad9e0c`, deployed with `ad9b812`.
- 2026-10-06 · ADDED · L0, L1 (A1), L2, L4 implemented (uncommitted); found and fixed on the way: front matter `page` was always 1 on later pages, offload restore used 8-char hashes and 40-char anchors, no-op retries rewrote the file.
- 2026-10-05 · REPLACED · S0 dropped and L3 superseded by B7 (MCP-only, deterministic constraint from the owner). C1/C4/C5 rejected; C6 → B7 path A.
- 2026-10-05 · FOUND · RC6: `[reasoning_extraction]` safeguard stops resumed chats; shared trait with the teammate's saver is verbatim re-emission of prior output. Added S0 A/B, options C5/C6; C1 no longer recommended until S0.
- 2026-10-05 · FOUND/BRANCHED · Field report investigated; RC1–RC5 found (RC1 and RC3 reproduced). B6 proposed with L0–L6 and alternatives A1–A4 / C1–C4.
