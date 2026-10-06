# B1: Invocation reliability

- **Source:** `invocation_reliability_impl_plan.md`
- **Parent:** B0 §8 and §11 (25%). Plan v2 called this "the open research problem".
- **Fixed decisions recorded in the plan:** Python, official `mcp` SDK (FastMCP), stdio, Claude Desktop as the first client. This replaced B0's TypeScript choice.
- **Strategy:** turn-start lagged logging, idempotent slot upsert, anchor-based binding, bounded backfill.
- **Later updates in this branch:** D0, D1, D2 (2026-10-05), and D3 for instrumentation.

## Decision ledger

### Isolation invariants (§1)
| ID | Invariant | Status | Evidence |
|---|---|---|---|
| I-1 | `save_turn` never raises; failures return `{ok:false, code, retryable}` | ✅ | `web/mcp_server.py:122-124` and the equivalent in `server.py`; `run_tests.py` Test 15 |
| I-2 | Hot-path p95 < 50 ms; index off the hot path | ◐ | `run_tests.py` Test 19 uses **50** concurrent calls, not the planned 200 across 10 threads. Remote budget is 300 ms (B2 S7) |
| I-3 | Split, never merge | ✅ | `storage/identity.py`; Tests 4, 16, 17 |
| I-4 | `vault_*` tool names | 🔀 2026-10-06 | Revised: names are unique **per transport**: stdio `vault_local_*`, remote `vault_*` (`tools/descriptions.py` `LOCAL_TOOL_NAMES` / `REMOTE_TOOL_NAMES`). Shared names let the local process take the deployed connector's calls in Claude Desktop (P1-13). Guarded by `test_local_and_deployed_tool_names_never_overlap` |
| I-5 | Tiny, data-only results; never echo user content | ◐ | `vault_find` notice echoes the query: `f"No threads matching '{query}' found."` (`web/mcp_server.py:197`). Minor |
| I-6 | `missing` reported once, capped at 10; backfill ≤ 10 per call | ✅ | Cap and lost-marking in `storage/gaps.py` (D2); `service.py:401` returns `too_many` |
| I-7 | No network, no reads outside the vault, no stdout | ✅ stdio only | `server.py:21-22`. In remote mode (B2/B5) the network is the transport by design |
| I-8 | Per-conversation opt-out phrases plus the global `.paused` file | ✅ | `server.py:76-78`, `service.py:34`, `config.py:126-133`; Tests 7, 8 |
| I-9 | `vault_find` returns metadata and ≤ 200-char snippets only | ✅ | `web/mcp_server.py:177`; Tests 9, 18. Remote adds signed viewer links by design (B2 S2) |
| I-10 | **Only chats are archived; coding-agent sessions are not** | 🆕 2026-10-06 | `tools/clients.py` `ArchiveServer` withholds write tools from clients matching `THREADVAULT_SKIP_CLIENTS` (default `claude-code`; handshake clientInfo, per-request `_meta` clientInfo, or User-Agent) and no-ops forced calls. Client side: Claude Code `deniedMcpServers`. Each `tools/list` logs the decision (`archived` / `write tools withheld`); the web app now routes `thread_save` INFO to the platform log, the stdio server opens `server.log` at startup. `tests/test_client_filter.py` (9: stdio + deployed HTTP on the file backend, incl. User-Agent-only). P1-14 |

### Mechanics
| § | Decision | Status | Evidence |
|---|---|---|---|
| 2 | Four tools; `thread_open` removed; ToolAnnotations | ✅ | `web/mcp_server.py:82-236`; parity tested by H4 (`tests/test_streamable_http.py`) |
| 2.1 | `SaveTurnInput` fields | ✅ | Remote also accepts `current_response` (for `both` mode) |
| 3.1–3.3 | Slot key `(thread_id, n, role)`, fidelity ranking, upsert rule table | ✅ → B3 | `security/idempotency.py` `SlotIndex.evaluate`; PG version is a single `ON CONFLICT` (B3) |
| 3.4 | In-place page rewrite under lock; backfill never moves turns | ✅ → B3 | `storage/writer.py`; formalised as W-6 sticky pages |
| 4.1 | Anchor binding; delete the `active.json` fallback | ✅ | `storage/identity.py:1-12` |
| 4.2 | Assign `n`: reuse on duplicate, stub on a missed previous turn | ✅ → B3 | Duplicate detection later changed to the `turn_key` triple (B3 WB) to fix the "continue ×2" bug (W-5) |
| 4.3 | Last assistant slot stored as `open` | ◐ ⚠ | `Fidelity.OPEN`; excluded from `total_turns` (ledger X2). **The open slot of the last reply is never closed**, so every chat's final reply is missing. After a FileStore restart, the open slot of a resumed chat is orphaned (the chat splits into a new file). `03-issues.md` P1-9, P0-4 → B6 L1/L3 |
| 5 | `gaps.py`: open → recovered/lost, reported once, 24 h → lost | ✅ | `storage/gaps.py`; `GapTracker(lost_hours=config.gap_lost_hours)` (`writer.py:391`) |
| 5 | **Late join** (first call arrives at turn k > 1) | ADDED D2 | Stubs 1..k-1, `missing` capped at 10, older marked lost; FileStore and PgStore (`tests/test_late_join.py`) |
| 6.1 | Wording rules: neutral, **state the destination**, sanctioned degraded path, opt-out, repeats are safe | ◐ ⚠ | Neutral wording is enforced by D0 (`tests/test_instruction_style.py`). **Destination is now wrong in both servers:** stdio says "ThreadVault account" (`server.py:119-120`), while the remote **server instructions** say "local markdown archive" (`web/mcp_server.py:34-37`). See `03-issues.md` P2-1, P2-10 |
| 6.2 | Exact description text | 🔀 D1 | Opening replaced: "Archive the conversation to the user's local ThreadVault (markdown files on this computer)." → "Call at the start of every reply to archive this chat turn for the user." (≤ 80-char rule). Remote body kept "ThreadVault account" (`a672217`) |
| 6.2 | `prev_response` "verbatim" + chunk-rather-than-shorten | ⚠ | `server.py:124,129-130`. Likely trigger for the model's `[reasoning_extraction]` safeguard in resumed chats (P1-11, inferred). → B6 S0/C6 |
| 6.2 | Backfill description | 🔀 D1 | Now opens "Send earlier chat turns that vault_save_turn reported as missing." |
| 6.2 | Late-join line | ADDED D2 | "If earlier turns of this conversation are listed as missing, …". It **duplicates** the existing "If the result lists missing turns…" line (`web/mcp_server.py:54-55`) |
| 6.3 | `THREADVAULT_MODE = turn_start \| both` | ⚠ | Parsed (`config.py:20-24,172`) but **only read for telemetry** (`server.py:192`). The description never mentions `current_response`, so `both` behaves exactly like `turn_start`. Test 11 calls the service with `current_response` directly and cannot catch this. P1-9 → B6 L3 |
| 6.4 | `THREADVAULT_NUDGE`, default off | ✅ | `service.py:388` |
| 6.5 | Opt-out by phrase or `.paused` | ✅ | as I-8 |
| 6.6 | Onboarding checklist | ✅ → B2 | `docs/ONBOARDING.md` rewritten for remote (X10) |
| 7 | `events.jsonl` telemetry on every call | ◐ ⚠ | Wired in **stdio only** (`server.py:66-67`). The web app builds `TurnService` without `events` (`web/app.py:68`), so remote and B5 record nothing. D3 added Postgres-derived reports instead. Stdio logs `binding` from the argument, not the result (`server.py:193`); failed calls are not logged (`server.py:204-206`); `bytes` is always 0. P2-13 → B6 L0 |
| 7 | `report.py`: coverage, recovery, split rate, dup no-op rate, p95 | ✅ | `report.py:8-82`; D3 added `--dsn/--thread/--latest` for PgStore |
| 8.1 | Deterministic protocol tests | ◐ ⚠ | `run_tests.py` (21) and `tests/test_protocol.py` (11). **Test 13 is hollow** (B0 §4.3). Test 19 is smaller than planned |
| 8.2 | Live 30-turn script and matrix | ❌ | No `tests/live_script.md`. `scripts/cross_surface_test.py` is a simulation (B2 X9) |
| R7 | Live baseline decides the `MODE`/`NUDGE` defaults | ❌ ⏸ | Not run. Defaults are still the untested assumptions `turn_start` and `off` |

### Proposed replacement (B7, 2026-10-05)
| § | Today | B7 proposal | Status |
|---|---|---|---|
| 2 | `vault_save_turn` at reply start | `vault_log_turn` **after** each reply; `vault_save_turn` becomes a deprecated alias | 🔀 deployed 2026-10-06 (`bad9e0c`) |
| 2.1 | `prev_response`, `chunk_index`, `is_final`, `current_response` | `user_message`, `reply` (capped), `thread_id`, `turn`, `prev_user_anchor`; no chunk fields | 🔀 deployed 2026-10-06 (`bad9e0c`) |
| 4.2–4.3 | lagged; last reply `open` | one txn writes both slots; no `open` slot | 🔀 deployed 2026-10-06 (`bad9e0c`) |
| 5 / backfill | resend missing turns incl. replies | server stubs `not_logged`; backfill user text only | 🔀 deployed 2026-10-06 (`bad9e0c`) |
| 6.2 | echo wording | echo-free wording, enforced by test (B7 E1) | 🔀 deployed 2026-10-06 (`bad9e0c`); `reply` field description added (P1-15, uncommitted) |
| 6.3 | `MODE=turn_start\|both` | `CAPTURE=full\|user_only` with schema shaping | 🔀 deployed 2026-10-06 (`bad9e0c`) |

### Storage evaluation: turn-start vs end-of-reply vs two-beat (2026-10-06)
Probe: the same 4-turn chat, run through `TurnService` on a FileStore under each protocol (scratch `probe_protocols.py`).

| | **Old: turn-start lagged** (§4, `vault_save_turn`) | **Current: end of reply** (B7 E, `vault_log_turn`, deployed) | **Proposed: two-beat** (B7 E8-1+2+3) |
|---|---|---|---|
| Calls per reply | 1, at the start | 1, after the reply | 2: start + end |
| Model sends | this user message + **previous reply** verbatim (+ chunks, + backfill of old replies) | this user message + **this reply** (≤ 8 000) | start: user message only; end: this reply (≤ 8 000) |
| Stored by the call | `(n,user)`, `(n-1,assistant)` = prev reply, `(n,assistant)` = `open` placeholder | `(n,user)` + `(n,assistant)` in one txn | start: `(n,user)` at once; end: fills `(n,assistant)` (E2b) |
| All calls made | t1–t3 complete; **t4 reply `open`** (last reply never stored, P1-9) | t1–t4 complete | t1–t4 complete |
| Model skips turn 3 | **Reply 3 stored under message 2**; message 4 stored as turn 3 | **Turn 3 gone, no stub**; message 4 stored as turn 3 (P1-17) | Without E8-3: message 4 stored **twice** (t3 no reply, t4 with reply). With E8-3: stub t3, message 4 at t4 |
| End beat missed (turn 3 / last turn) | n/a | n/a (same as a skipped turn) | user side kept; reply marked missing on that turn |
| Fires unprompted | ✅ every turn on 2026-10-05 | ❌ owner must ask (P1-16) | start beat sits where the old call sat; **unmeasured** |
| Model output re-sent | earlier output, 5k→14k per turn; flagged at turn 6 (P1-11) | this reply once, capped | same as current |
| Reply label | `verbatim`, also for summaries (P1-12) | `reported` / `abridged` / `truncated` | same as current |
| Score (E8 weights) | 65 | 59 | **83** (E8-1 alone 77) |

Score inputs, as Fires/Miss/Safety/Clients/Reuse: old 5/1/1/4/5, current 2/1/4/5/5, proposed 4/4/4/5/4, E8-1 alone 4/2/4/5/5.

The old design called reliably but stored wrongly. The current design stores correctly but is called unreliably, and loses a skipped turn without a trace. The proposal keeps the old trigger position and the current payload, and needs E8-3 so that a skipped turn becomes a stub, not a duplicate.

## Milestone map
| Plan | Landed in | Commit |
|---|---|---|
| R1–R6 | Before the first commit; first committed as "Part A" | `33163b5` (2026-10-04 17:18) |
| R7 | — | not done |
| D0 neutral-style test | B1 update | `ed7f0ad` |
| D1 ≤ 80-char openings, "every reply" | B1 update (replaced the §6.2 text) | `f967b97` |
| D2 late-join backfill | B1 update (extends §5) | `68ab2a7` |
| D3 PG coverage report | B1 §7 on B2's store | `ff87b11` |
| H4 stdio/remote description parity | B1 × B2 | `1e9d90e` |
| keep "ThreadVault account" in the remote body | B1 × B2 | `a672217` |

## Open items owned by B1
1. ✅ 2026-10-06 (B7 E3). Fix the destination wording: "local markdown files on this computer" for stdio; "the user's ThreadVault account on <service>" for remote, in both the server instructions and the description. Add a test that checks each transport's wording.
2. ✅ 2026-10-06: the old text is no longer listed (B7 E3).
3. Wire `EventLogger` (or a PG `events` table) into the web app so remote coverage can be measured.
4. Make Test 13 a real test once the stale-chunk flush exists.
5. Run R7: a live baseline on the deployed connector to choose the `MODE`/`NUDGE` defaults from data.
6. Make `MODE=both` real, or replace it (B6 L3, choice C1/C2/C3).
7. Telemetry: binding/bytes from the result, failures logged, file log for stdio (B6 L0).
8. ◐ Invocation rate fell after the move to end-of-reply (P1-16). B7 E8 built 2026-10-06 (uncommitted): start call + end call, results name the next step, skipped turns stubbed (P1-17). Needs the E8-5 live run.

## Change log (newest first)
- 2026-10-06 · REVISED · §6.2: every start call carries the turn-before reply (`last_reply`, B7 E8-7); the end call is still asked for as the final action. Owner dropped the safeguard constraint. Uncommitted, untested.
- 2026-10-06 · ADDED · §6.2: the end call is still skipped live, so call 1 now carries `missed_reply` for a turn whose call 2 never came (B7 E8-6). Uncommitted, untested.
- 2026-10-06 · REPLACED · §6.2: `vault_log_turn` is asked for at the start of each reply, plus a second call after the reply in `full` capture (B7 E8). The first sentence is now "Call at the start of each reply and again after it to archive this chat turn." The D1 test was changed to match. Uncommitted.
- 2026-10-06 · ADDED · Storage evaluation of turn-start vs end-of-reply vs two-beat (probe on FileStore). Old 65, current 59, two-beat 83. Found P1-17: a skipped `vault_log_turn` call loses the turn silently.
- 2026-10-06 · FOUND · P1-16: since `vault_log_turn` moved the call to after the reply, it no longer fires every turn; start-of-reply calls had run unbroken. Fix proposed in B7 E8; §6.2 wording will change if E8-1 is approved.
- 2026-10-06 · REVISED · §6.2: `vault_log_turn` `reply` has its own schema description asking for the full Markdown reply; "server shortens" line replaced (B7 P1-15). Uncommitted.
- 2026-10-06 · REVISED · I-10: deployed HTTP path tested (Claude Code by clientInfo and by User-Agent only → no write tools, no file; chat client archived). Decision now visible: web app had no logging setup, so Render would have dropped the guard's INFO lines; stdio `server.log` attached at startup so a Desktop connect is logged before any save (P1-14).
- 2026-10-06 · ADDED · I-10: coding-agent sessions (Claude Code) are not archived. Server guard + Claude Code `deniedMcpServers` (P1-14).
- 2026-10-06 · REVISED · I-4: stdio tools renamed `vault_local_*` so they never collide with the deployed connector's `vault_*` (P1-13).
- 2026-10-06 · REPLACED · B7 path E implemented: `vault_log_turn` after each reply replaces turn-start `vault_save_turn` (unlisted unless `THREADVAULT_LEGACY_SAVE_TURN=on`); destination wording fixed per transport; `MODE` now applies to the legacy tool only, `CAPTURE` is the new switch.
- 2026-10-05 · BRANCHED · B7 echo-free capture proposes replacing §2, §2.1, §4.2–4.3, §5 backfill, §6.2, §6.3 (see table above).
- 2026-10-05 · BRANCHED · B7 proposed: replace model-driven capture (`vault_save_turn`, lagged logging, MODE/NUDGE, backfill) with deterministic source capture; MCP would become read-only. Rows stay until B7-3 is live.
- 2026-10-05 · FOUND · §6.2 echo instructions (`prev_response` verbatim, chunking) likely trip `[reasoning_extraction]` (P1-11 → B6).
- 2026-10-05 · FOUND · Local Desktop field report (B6): the tool *is* called at turn start, but `MODE=both` is a no-op, so the last reply is never saved (§4.3, §6.3 → ⚠); telemetry hides restart splits (§7 → ⚠). Work proposed in B6.
- 2026-10-05 · FOUND · Audit: destination wording wrong in both transports; duplicate description line; remote has no telemetry; Test 13 hollow; R7 not run.
- 2026-10-05 17:49 · REVISED · `a672217` remote description body keeps "ThreadVault account".
- 2026-10-05 17:34 · ADDED · `68ab2a7` D2 late-join backfill.
- 2026-10-05 17:31 · REPLACED · `f967b97` D1 replaced the §6.2 description openings.
- 2026-10-05 17:28 · ADDED · `ed7f0ad` D0 forbidden-words test.
- 2026-10-04 23:01 · REVISED · `1e9d90e` H4 aligned stdio/remote descriptions and added remote `vault_stats`.
- 2026-10-04 17:18 · ADDED · `33163b5` R1–R6 committed as part of "Part A".
