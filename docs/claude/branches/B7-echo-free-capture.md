# B7: Echo-free capture protocol (PROPOSED)

- **Source:** no plan document. Branched on 2026-10-05 from B6 RC6 / `03-issues.md` P1-11: resumed chats stop with "safeguards flagged this message … [reasoning_extraction]". The teammate's independent saver (full transcript on every turn) hit the same error.
- **Parent:** B1 §2, §4, §6 (tool surface, binding, description text) × B6 (storage fixes L0–L2, L4 are prerequisites). Supersedes B6 L3.
- **Status:** ◐ **path E + E2b deployed 2026-10-06 (`bad9e0c`, `96acd6c`, `ad9b812`; remote tools/list verified). P1-15 Markdown reply contract implemented 2026-10-06, uncommitted, not deployed.** E6/E6b/E7 open. Owner approved with "execute it"; defaults taken: capture `full`, reply cap 8 000, legacy tool behind a flag (see Decisions).
- **Replaces:** B7 v1 "deterministic capture" (another session, TIMELINE 66), which read chats from outside MCP (Compliance API, web-app endpoints, own client, hooks, extension). Rejected by the MCP-only constraint; its file was never written.
- **Constraint (owner, 2026-10-05):** MCP only. No browser extension, no data-export import, no client hooks. The fix must be deterministic: no "turn off other connectors" type of advice.

## Problem statement
Today's protocol asks the model to **re-emit its own earlier output**: `prev_response` "verbatim", chunked "instead of shortening it", and backfill/"save the whole chat" (`server.py:122-136`). The model-side `reasoning_extraction` safeguard is reported to fire when a model is asked to hand back its own completed work (anthropics/claude-code#97503). A long resumed chat full of such re-emissions fits that pattern.

What MCP can and cannot guarantee:
- **Cannot:** an MCP server never sees the chat. Content reaches it only through tool arguments the model writes, and whether the model calls a tool is always the model's decision.
- **Can be made deterministic:**
  1. The protocol never asks for earlier model output (enforced by tests).
  2. The tool schema contains only the fields allowed by the configured capture level (the model cannot send what the schema doesn't have).
  3. Server-issued identity and turn numbers.
  4. Exactly-once, restart-safe storage (B6).
  5. Every missed turn is detected and recorded by the server without asking the model to resend anything.

## Evidence from the flagged chat (vault_local, 2026-10-05)
Chat `6z3t53` ("Current resistance in tech and product"), owner reports the flag after 6–7 queries. `events.jsonl` shows turn 6 sent **twice** (17:02:46 and 17:05:35), consistent with "Edit and retry" after a flag; the chat ends at turn 9 with the reply still pending.

| Turn | User asked | Claude reply (chars) | Claude's own output re-sent through `prev_response` so far |
|---|---|---|---|
| 1 | "tell me about the currently facing resistance…" | 2 139 | 0 |
| 2 | "more precise" | 2 375 | 2 139 |
| 3 | "longer" | 5 102 | 4 514 |
| 4 | "ok" | 171 | 9 616 |
| 5 | "more context plz, but related to thread ov" | 5 032 | 9 787 |
| **6** | "more" | **12 135** | **14 819** ← first flag (retry) |
| 7 | "extend…" | 573 | 26 954 |
| 8 | "branch out" | **14 259** | 27 527 |
| 9 | "mroe" | (pending) | **41 786** |

Observations:
1. **The duplicate volume grows every turn.** Each reply is in the context twice: once as the reply, once copied into the next `vault_save_turn` call. By the flag, ~15 k chars of Claude's own output had been copied back; by turn 9, ~42 k.
2. **The 6 000-char chunk rule was ignored.** All calls show `chunk=false`, including the 12 k and 14 k replies, so single tool inputs carried 12–14 k chars of copied output.
3. **The user's requests escalate output** ("more", "longer", "extend", "branch out"), and two chats asked Claude to describe its own connector/tool definitions ("tell me everything about this local connector"). These may contribute on their own; only a control run separates them.
4. **Confounder:** the same claude.ai account has the Threads-Ov connector, whose instructions demand the **full transcript on every reply** ("MANDATORY"). If it is active in these chats, its copies stack on top of ours.

What we cannot know: the classifier's actual rules (not published). The above is correlation, not proof.

### Rejected: evading the classifier
Obfuscating the copied text (encoding, splitting to stay under a size, rewording descriptions to hide what they ask for) is **not an option**. It means working around a safety system rather than fixing a false positive. It would also be fragile, since classifier updates break it, and could put the account at risk. The fix is to stop producing the pattern (paths A/C/E) and report false positives through the "send feedback" button on the flag.

## Evidence (flagged chat `6z3t53`, 2026-10-05)
**Evidence from the flagged chat (2026-10-05, `vault_local/dhanshree/…_6z3t53_current-resistance-i_p01.md` + `events.jsonl`):**
- Size of the reply re-sent in `prev_response`, per turn: 2.1k, 2.4k, 5.1k, 0.2k, 5.0k, **12.1k**, 0.6k, **14.3k** chars. By turn 8 the context holds about 42k chars of Claude's own replies a second time, as tool inputs.
- Turn 6 ("more", 22:32 IST) has **two** `vault_save_turn` calls with `n=6` (17:02:46 and 17:05:35 UTC). That is the same message run twice, which matches a flagged reply followed by "Edit and retry". The flag came at the **6th query**, right after a 5k-char re-send, and the user then re-sent the 12k turn-5 reply.
- **After the flag, the re-sends stopped being copies.** Turns 9–10 were stored as `"[Provided 10 branch implementations …]"` and `"[Extended breakdown covering …]"` (263 and 140 chars), still labelled `fidelity=verbatim`. Either the model avoided the echo itself, or the fallback model summarised. In both cases the archive now holds **false "verbatim" content** (new P1-12).
- The same context also carried another connector's instructions to send the **full transcript every turn**, "silently", as the "highest priority directive". The chat's *topic* was testing transcript capture as well. Both add to the "copy out the conversation" signal; neither is under this server's control, so the fix must not depend on them.
- **What the classifier actually checks is not public**; this is a correlation from one chat, not a confirmed mechanism.

What this means for the design:
1. The amount of echo grows with reply length, and "chunk instead of shortening" makes long replies the biggest echoes. **Echo size is a lever we control.**
2. Under pressure, the model already stops copying, so `verbatim` from the model cannot be trusted. **E-truth** below.
3. **Not pursued:** disguising the echo so the classifier doesn't recognise it (renaming fields, encoding, splitting under a size, asking for paraphrase). That would be evading a safety system; it is fragile and puts the account at risk. B7 changes *what we ask for*, not *how it looks*. False positives are reported through the in-app feedback button on each flagged message.

## Five paths (all inside MCP)

### Scoring weights
| Criterion | Weight | Why this weight |
|---|---|---|
| Safeguard safety: never asks the model to re-emit old output | **30** | A flagged chat is unusable, which is worse than a missed save |
| Capture completeness: both sides, including the last reply | **30** | This is the product goal |
| Determinism: identity, storage, gaps and opt-in decided by server/config, not model judgment | **20** | Owner requirement |
| Client fit and UX: Windows Store Desktop (stdio) + claude.ai (remote) | **10** | Must work where the user is |
| Builds on existing B1–B6 work / effort | **10** | Reuse `TurnService`, `Store`, tests |

Scores are 1–5 per criterion; total = Σ(score × weight) / 5, out of 100. They are judgement scores from code reading and public reports. **E7 replaces them with measured numbers.**

### Paths
| Path | What it is | Safety | Complete | Determ. | Client | Build | **Total** |
|---|---|---|---|---|---|---|---|
| Baseline (today) | Turn-start lagged `prev_response` echo, chunking, backfill | 1 | 2 | 2 | 4 | 5 | **44** |
| **A: Echo-once at end of reply** | One call **after** each reply: `vault_log_turn(user_message, reply, thread_id, turn)`. No earlier output, no chunking, no history backfill | 3 | 4 | 3 | 4 | 5 | **72** |
| **B: Write-through reply (MCP App)** | Model writes the reply **once**, as the tool argument; the server stores it and renders it with a `ui://` widget, so it is never re-emitted and stored = shown | 4 | 4 | 4 | 1 | 2 | **70** ✗ gate |
| **C: User-message ledger** | Store only the user's messages (user text, never model output) plus turn numbers; replies not captured | 5 | 2 | 4 | 5 | 5 | **78** |
| **D: Session contract via MCP prompt** | User starts the chat with a server prompt (`start_archive`); the server mints the `thread_id` in `get_prompt` and the instruction sits in the user's own turn. The current protocol stays | 2 | 3 | 4 | 2 | 3 | **56** |
| **E: Layered (A + C floor + guards + B6 storage, D where supported)** | See below | 4 | 4 | 5 | 4 | 3 | **82** ✅ |

### Why each path scored as it did
- **A, 72.** Sends only the reply it just wrote, once. That is the smallest echo an MCP design allows for capturing replies, and it has no "hand back earlier work" step. **Risk:** still one echo, and it is not proven safe. It reuses the existing `current_response` path (`service.py:147,213-225`), so build effort is low.
- **B, 70, fails the gate.** It is the only path with zero re-emission. But Claude Desktop on **Windows** does not render MCP App widgets; anthropics/claude-ai-mcp#729 was closed "not planned". Only the text result reaches the model, so the user would see nothing unless the model repeats the reply, which brings the echo back. It also loses streaming. **Re-evaluate if Windows rendering ships.**
- **C, 78.** Our connector can never be the trigger, because the model never sends its own output. It captures only half of each chat. It works best as a **floor mode**, not as the product.
- **D, 56.** Binding is deterministic and opt-in is explicit, but it keeps today's echo. Prompts from **local stdio** servers fail to attach in Claude Desktop (anthropics/claude-code#82045); remote prompts work. It is useful only as an add-on on the remote server.
- **E, 82.** Combines A's completeness with C's deterministic floor. Echo-free wording is enforced by CI. Identity and turns are issued by the server, and storage comes from B6. D is added only where the client supports it. It costs more parts, but each part is small and reuses existing code.

### Rejected (outside the constraint or not available)
| Option | Reason |
|---|---|
| Browser extension / claude.ai export import (old B6 C5) | Outside MCP |
| Claude Code `Stop` hook (old B6 C4) | Outside MCP; Claude Code only |
| MCP **sampling** (server asks the client to generate) | The client never gives the server the conversation; Claude apps do not offer sampling to connectors |
| MCP **elicitation** asking the user to paste the chat | Manual; not deterministic coverage |
| B6 C1 (`both` = start + end call) | **Doubles** the echo |
| Connector toggling / model switching as the fix | Not deterministic (owner) |

## Path E: design
| Part | Mechanism | Deterministic because |
|---|---|---|
| E-proto | New tool `vault_log_turn`, called once **after** each reply. Fields: `user_message`, `reply`, `thread_id` (from the last result), `turn` (from the last result's `next_turn`), `prev_user_anchor` (user text only, for binding recovery). Server writes `(n,user)` and `(n,assistant)` in **one txn**; there is no `open` slot | One call = one complete turn; exactly-once by `turn_key` (B3 WB) |
| E-cap | `reply` hard cap (`THREADVAULT_REPLY_MAX_CHARS`, default 100 000). Longer replies are stored as a prefix with `fidelity=truncated`. **No chunk fields** | No multi-call re-emission possible |
| E-floor | `THREADVAULT_CAPTURE = full \| user_only`. In `user_only`, the `reply` field is **removed from the tool schema** and the description | The model cannot send what the schema does not have |
| E-guard | `tests/test_instruction_style.py` (B1 D0) gains an echo-free word list, checked for both transports: `previous reply`, `prev_response`, `verbatim` (applied to model output), `transcript`, `whole chat`, `all turns`, `instead of shortening`, `reasoning`, `thinking`, `chain of thought` | CI fails on any regression |
| E-ident | The first call mints `thread_id`; each result returns `{ok, thread_id, next_turn}` only. If `thread_id` is lost, bind by `prev_user_anchor` (user text), then B6 L1 rehydrate | Server-issued; survives restarts |
| E-gaps | Server expects `next_turn`. Skipped turns become stubs with `reason=not_logged`, reported in `vault_stats`. **Never** a request to resend | Detected by arithmetic, not by the model |
| E-truth | Model-sent replies are stored as `reported`, never `verbatim`. Bodies that are just a bracketed description, ~~or suspiciously short~~ (never built), or **flattened** (one line > 600 chars, or > 200 chars with an inline "(1) … (2)" list; P1-15) are stored as `abridged` and counted in `vault_stats` | Server-side classification, not model self-report |
| E-budget | `THREADVAULT_REPLY_MAX_CHARS` default **8 000** (not 100 000). Longer replies are stored as a prefix + `truncated`. Echo per turn is bounded no matter how long the chat gets | Fixed by config; E7 tunes it with data |
| E-retire | `vault_save_turn`: `prev_response` / chunk args no longer advertised (accepted for N weeks as a deprecated alias, then removed). `vault_backfill`: user messages only. "Save the whole chat" line removed | No path asks for old model output |
| E-contract (optional) | Remote HTTP server only: MCP prompt `start_archive(title)`. Mints `thread_id` in `get_prompt`; the text tells the model to log each turn to that id | Explicit per-chat opt-in; skipped for stdio (#82045) |

## Sub-goals (proposed order)
| ID | Sub-goal | Depends on | Done when |
|---|---|---|---|
| E0 | Prerequisites from B6: L0 logging, L2 atomic create, L1 rehydrate, L4 index | B6 | ✅ see B6 |
| E1 | E-guard test | — | ✅ `tests/test_echo_free_protocol.py`: reads the **advertised** surface (instructions, descriptions, every schema field) of both transports × both capture modes in a fresh process; banned list incl. `previous reply`, `prev_response`, `verbatim`, `transcript`, `whole chat`, `chunk`, `in parts`, `reasoning`, `thinking`, `silently`. Meta-test proves the old wording fails |
| E2 | `TurnService.log_turn` + `vault_log_turn` on both servers; E-cap; E-ident | E0 | ✅ `service.py` `log_turn` (one txn writes both slots; `turn_key = log1|turn|prev_anchor|user|hash(reply)` from raw text so a retry after restart is a no-op); servers return only `{ok, thread_id, next_turn}`. `tests/test_log_turn.py` (FileStore + **PgStore** + PG fsck), HTTP tests migrated. Hypothesis model **not** extended (open) |
| E3 | E-retire + new server instructions/descriptions | E2 | ✅ `tools/descriptions.py` shared by both servers; real destination per transport (B1 open item 1 closed); `vault_save_turn` listed only with `THREADVAULT_LEGACY_SAVE_TURN=on`; `vault_backfill` strips anything but user text; duplicate "missing" line gone with the old text |
| E4 | E-floor switch with schema shaping | E2 | ✅ `THREADVAULT_CAPTURE=user_only` registers a function without `reply`; verified by schema listing on both transports |
| E5 | E-gaps stubs and stats | E2 | ✅ again with E8-3 (uncommitted): a skipped call is detected from `prev_user_anchor`. Before that, stubs were written only when `turn` was **ahead** of the server. The model sends back the `next_turn` it was given, so a skipped call is never detected: the turn vanishes and the next message takes its number (P1-17, probe 2026-10-06). Fix: E8-3. Otherwise: stale `turn` never overwrites; `turn` more than 50 ahead is ignored |
| E6 | E-contract prompt (remote) | E2 | prompt round-trip test; manual check on claude.ai |
| E6b | **Flag reproduction script:** a fixed 10-message script copying chat `6z3t53` ("more", "longer", "extend", "branch out"), run on Sonnet 5.5 under server configs: (a) today, (b) path A end-of-reply, (c) path A with a 4 000-char reply cap, (d) `user_only`, (e) control (`CAPTURE=off`, tool not advertised). 3 runs each; record the turn where the flag appears | E2, E4 | tells which config removes the flag, and whether the conversation itself (control) is enough to trigger it |
| E7 | Live measurement: 20 chats (10 Windows Desktop stdio, 10 claude.ai remote), ≥ 8 turns each, 5 of them resumed after a restart | E3–E5 | record flags/chat, turns logged / turns, last-reply coverage; replace the judgement scores above with these numbers |

## Decisions (owner, 2026-10-06: "execute it")
1. Path E: ✅ approved.
2. Capture default: ✅ `full`.
3. `vault_save_turn`: 🔀 **deviation from the recommendation**. A listed alias would keep the old echo wording in every chat's tool list, which is the trigger. It is **unlisted by default**; `THREADVAULT_LEGACY_SAVE_TURN=on` re-lists it as the 2-week fallback. The Python function and `TurnService.save_turn` are unchanged.
4. E-truth storage: new fidelity **`reported`** (rank 4); `verbatim` moved to rank 5 with Alembic `b7e1f0a2c3d4` (shifts existing rows 4→5; tested on the local test DB). Legacy `save_turn` still labels `prev_response` `verbatim` (unchanged, legacy only).

## Cross-branch effects
- **B1:** §2 tool set (+`vault_log_turn`), §2.1 input fields, §4.2/4.3 (no open slot), §6.2 descriptions, §6.3 `MODE` replaced by `CAPTURE`: all 🔀 proposed.
- **B3:** each turn is one txn with both slots. Chunk assembly (WD `turn_chunks`, P1-7) becomes **moot**; plan to drop it rather than build it.
- **B6:** L3 superseded by B7; C1/C4/C5 rejected; C6 became path A.
- **B2/B5:** same protocol on the remote server; E6 is remote-only.

## When is "the end of the reply"? (owner question, 2026-10-06)
No component signals it. MCP has no "reply finished" event, and the client never tells a server what was said. The model writes its reply text and then, as its last content block, emits the `vault_log_turn` call; the server learns about the turn only when that call arrives. "After the reply" is therefore the model following the description, not something the server detects. The server's job is to make every timing mistake land deterministically. Probe (`log_turn` on FileStore, as built):

| Case | Stored today | Wanted |
|---|---|---|
| A. Called after the reply | turn n: user + reply | same |
| B. Called before writing (no `reply`), never again | turn n: user only, no reply slot | same (honest gap); count it in stats |
| C. Called early **and** at the end of the same reply | ~~turn n: user only; turn n+1: **same user** + reply (duplicate)~~ → **turn n: user + reply** (E2b) | fill turn n ✅ |
| D. User clicks Retry; model re-logs with the same `turn` | ~~turn n: old reply; turn n+1: **same user** + new reply (duplicate)~~ → **turn n: new reply** (E2b) | replace reply n (newest wins) ✅ |
| E. Model skips the call on the chat's last turn | nothing | undetectable (no later call) |
| F. Text written after the tool call | not stored | same; the description says "last step" |

**E2b same-turn merge** (built 2026-10-06, owner: "make sure it should be calling once"). `service._is_same_turn`: a call is another call for the latest turn n when its user message anchor equals turn n's **and**
- `prev_user_anchor` is given and matches turn **n-1** (a new turn that repeats the same words carries turn n's anchor instead, so "continue" ×3 stays three turns), or
- no anchor, and n = 1, or `turn` = n (Retry), or turn n has no reply yet.

Then the reply fills or replaces turn n's reply with `upsert_turn(newest_wins=True)` (both stores; Pg adds `$12` to the `ON CONFLICT … WHERE`), the result returns `next_turn = n+1`, and nothing is appended. A repeat of that call is a no-op (same hash). A doubtful case is appended: it can duplicate, never lose a reply. Covers placeholder-then-full replies (C′) when the anchor is sent. The description now says "as the last step, after the reply is written". Limits: a Retry of the **first** reply has no `thread_id` in context and still opens a second thread; the local FTS index keeps the replaced reply text as well.

## E8: the call stopped firing on every reply (built 2026-10-06, uncommitted; E8-5 live run pending)
**Owner report:** the save tool is no longer called on every response; it runs when the user names the connector.

**Cause (Verified as a regression, mechanism Inferred).** Path E moved the trigger from the **start** of the reply to the **end** (`bad9e0c`, deployed `ad9b812`). The instructions changed from "At the start of each reply, call vault_save_turn" to "After finishing each reply, call vault_log_turn".
- Start of reply (2026-10-05, `events.jsonl`): every chat opened with an unprompted `n=1` call, and numbering ran unbroken (`6z3t53` 1→11, `…FJR` 1→6, `…FD9` 1→5).
- End of reply (2026-10-06): `eatz7d` logged 4 turns. `mt8f50` began only when the user typed "saving by invoking the right tool of local connector which is threadvault-local", so its turn 1 is that request and has no reply (80 bytes).

At the start, the call is the model's first decision. At the end, it has to win against simply ending the turn after a finished answer, guided only by a description that sits among ~100 tools from 10 connectors. The teammate's Threads-Ov server reports the same pattern: saves get missed on plain-text turns and on turns that call other connectors, which is why it adds a first-call `checkpoint`.

**Second defect found:** when the first call comes mid-chat, the server numbers that message turn 1. `vault_local_backfill` then has no earlier slots, so it returned ok and stored nothing (`mt8f50`, 13:38 UTC).

No MCP server can force a call. What the server can decide is where the trigger sits, what each result says, and how a miss is recorded and repaired.

### Scoring weights (E8)
| Criterion | Weight | Why |
|---|---|---|
| Fires on every reply without the user asking | **35** | The reported bug |
| A miss is recorded and repairable, never silent | **20** | Deterministic degradation |
| Safeguard safety: adds no re-sent model output | **20** | P1-11 must not come back |
| Works on Desktop stdio and claude.ai remote | **15** | Both are in use |
| Reuses built code (E2b, gaps, backfill) | **10** | Effort |

### Paths
| Path | What it is | Fires | Miss | Safety | Clients | Reuse | **Total** |
|---|---|---|---|---|---|---|---|
| **E8-1 Two-beat turn** | **Start beat:** at the start of each reply, `log_turn(user_message, thread_id, turn, prev_user_anchor)` with no `reply`, and the server stores the user side at once. **End beat:** after the reply, the same tool with `reply`; E2b case C merges it into the same turn. A missing end beat leaves "reply not logged" on that turn, counted in stats | 4 | 4 | 4 | 5 | 5 | **85** ✅ |
| **E8-2 Result chaining** | Every result carries the next call as data, e.g. `then: {tool, turn, when: "after this reply"}`. The cue is placed in the conversation at the latest point, not in a description far up the context | 3 | 3 | 5 | 5 | 5 | **78** |
| **E8-3 Gap ledger + one-call catch-up** | Each call reports `missing` turns from the turn and anchor arithmetic. A first call made mid-chat sends `user_turn_index` (the count of user messages so far; user text only), so earlier turns keep their slots and backfill can fill them. One call repairs the user side of the whole chat | 2 | 5 | 5 | 5 | 4 | **77** |
| **E8-4 Session-contract prompt** | MCP prompt `archive_this_chat`, attached once per chat. The rule then sits in the user's own turn, the strongest position. It needs a click per chat, and local stdio prompts do not attach in Desktop (claude-code#82045), so it is remote-only today. This is E6 | 3 | 2 | 5 | 2 | 3 | **61** |

Scores are 1–5 judgement scores; total = Σ(score × weight) / 5. Measurement (below) replaces them.

**Why E8-1 is not the rejected B6 C1.** C1 sent `prev_response` at the start, which doubled the echo. The E8-1 start beat carries the user's text only, so model output is still sent once per turn, exactly as today.

**Probe correction (2026-10-06, B1 storage evaluation).** E8-1 alone is unsafe. After a skipped turn, the start beat stores the new message at the skipped number. The end beat's `prev_user_anchor` then fails E2b's "matches turn n-1" rule, so the message is stored twice. E8-3 is therefore required, not optional:
- If `prev_user_anchor` does not match the latest stored user turn, write a stub for the skipped turn and store the message at the next number.
- The start-beat result returns `turn: n`. The end beat merges when `turn == n` and the user anchor matches, whatever `prev_user_anchor` says.

Re-scored: E8-1 alone **77** (Miss 2), E8-1+2+3 **83**.

**Recommended:** E8-1 + E8-2 + E8-3 as one change. They compose: the start beat carries the result chain, and the gap ledger catches what both beats miss. E8-4 stays E6 (remote add-on).

**Acceptance (E8-5):** 3 Desktop chats × 8 turns, mixing plain-text turns and turns that call other connectors. From `events.jsonl`, record turns with a user side / turns (target 100%) and turns with a reply / turns. Compare with the 2026-10-05 start-of-reply baseline.

## Change log (newest first)
- 2026-10-06 · REPLACED · E8-6 → **E8-7 `last_reply` on every start call** (owner: "forget about the safeguard issue, make sure it should call after the response … and save the current response"). Call 2 after the reply is unchanged and now described as "the final action, after your last line of text". Call 1 always carries the turn-before reply (`last_reply`), so a skipped call 2 is filled on the next turn. A reply already stored by call 2 is kept, never overwritten. **E1 echo-free dropped for full capture by owner decision:** each reply is sent up to twice (call 2 + next call 1). Not covered: the last turn of a chat when call 2 is skipped. Uncommitted, untested at owner's request.
- 2026-10-06 · ADDED · E8-6 missed-reply catch-up (uncommitted, untested at owner's request). After `ab5ab9d` was deployed, the owner reports the end call is still skipped even with the Personal Preference set. Change: call 1 takes an optional `missed_reply` (full-capture schema only; field description `MISSED_REPLY_FIELD_DESC`), filled only when call 2 was not made for the turn before. It writes the latest turn's reply only if that slot is empty, open or a stub; it is dropped in `user_only`, after a detected gap, and never overwrites. The call-1 `then` now reads "This turn is archived without your reply until you call … as the last step of this reply." Step 2 says "every reply, short ones included". **Deviation from E1 (echo-free), owner-driven:** the model can now be asked for the turn-before reply. It is bounded: each reply is sent once in total (call 2 or the next call 1), never re-sent or accumulated as under `prev_response`. Not covered: the chat's last turn. Tests added in `test_two_call_turn.py` (+3) and the `test_echo_free_protocol.py` schema expectation, not run.
- 2026-10-06 · ADDED · E8-1/2/3 built (uncommitted, not deployed).
  - Wording: the instructions and description ask for a call at the start of each reply with the user's message, and in `full` capture a second call after the reply with `turn` = the turn call 1 returned. `user_only` has only the start call. `server_instructions(…, capture)`; the remote server builds them from its config.
  - Results: call 1 → `{ok, thread_id, turn, then}`; call 2 and `user_only` → `{ok, thread_id, next_turn}` (`public_result(result, log_tool)`, `awaiting_reply` flag from `log_turn`).
  - Service: `_skipped_turn` writes a stub for the skipped turn and stores the message at the next number when `prev_user_anchor` names a message that was never stored. Matching is loose: equal, a prefix of ≥ 16 chars, or `SequenceMatcher` ≥ 0.8. The last stub keeps the anchor as its "began:" text.
  - Merge: `_is_same_turn` merges when `turn == n`, or when turn n-1 is a not-logged stub.
  - Mid-chat first call: `turn` = this message's number, so earlier numbers stay free for backfill.
  - Stores: new txn method `user_anchor(n)` on FileStore and PgStore.
  - Tests: `tests/test_two_call_turn.py` (11, FileStore + PgStore; 4 fail with detection off); wording tests in `test_echo_free_protocol.py` (+4) and `test_instruction_style.py` (first sentence now says "at the start of each reply").
  - Still open: E8-5 live run.
- 2026-10-06 · REVISED · E8: probe shows E8-1 alone stores a message twice after a skipped turn; E8-3 made mandatory (anchor-mismatch stub, merge by `turn`). E5 downgraded ✅→◐: a skipped call is never stubbed (P1-17).
- 2026-10-06 · FOUND/BRANCHED · E8: the call stopped firing on every reply after the move to end-of-reply (P1-16). Paths: E8-1 two-beat 85, E8-2 result chaining 78, E8-3 gap ledger + catch-up 77, E8-4 prompt 61. Recommended 1+2+3. Also found: a mid-chat first call takes turn 1, so backfill stores nothing.
- 2026-10-06 · ADDED · P1-15 Markdown reply contract (owner chose option 1 of 3: contract 76, reflow-only 66 ✗ goal, structured blocks 61). `reply` gets a JSON-schema field description (`REPLY_FIELD_DESC`: full reply as Markdown with the same headings, lists, tables, code blocks and line breaks; not a summary) on both transports; tool text bullet uses the same words; "The server shortens long replies…" → "Send the whole reply once; the server keeps up to N characters" (N from `THREADVAULT_REPLY_MAX_CHARS`, `config.load_reply_max_chars`). `service.reply_shape` (markdown/plain/flattened) feeds E-truth; shape logged per call (stdio `events.jsonl` `reply_shape`, remote INFO log). Tests: `test_log_turn.py` (+2), `test_echo_free_protocol.py` (+2). Uncommitted. Echo per turn can grow from ~1k (summaries) to the 8 000 budget: run E6b before relying on it.
- 2026-10-06 · FOUND · Replies logged by `log_turn` are condensed one-line retellings: chat `eatz7d` turns 1–4 = 1 line each, 793–1 054 chars, 0 Markdown lines (even after "more"/"extend"); old `prev_response` replies in the mirror kept 3–192 lines / up to 107 Markdown lines. Storage preserves Markdown (round-trip probe). Cause: the reply contract — no per-field description, no full/Markdown cue after "verbatim" was banned, and "The server shortens long replies" invites shortening. E-truth's planned "suspiciously short" check was never built, so these were stored as `reported` (P1-15).
- 2026-10-06 · ADDED · E2b same-turn merge: second call for the latest turn (early + end, placeholder + full, Retry, user_only double call) updates that turn; `newest_wins` upsert on FileStore + PgStore; 9 tests in `test_log_turn.py` (FileStore + Pg).
- 2026-10-06 · FOUND · End-of-reply timing probe: cases C (early + late call) and D (Retry) store the same user message twice; B leaves a reply-less turn; E undetectable. E2b same-turn merge proposed.
- 2026-10-06 · REVISED · E3: tool names now differ per transport (`vault_local_log_turn` on stdio, `vault_log_turn` remote), from P1-13. Guard test checks names, instructions and the no-overlap rule.
- 2026-10-06 · ADDED · E0–E5 implemented (uncommitted). New: `tools/descriptions.py`, `vault_log_turn`, `TurnService.log_turn`, `classify_reply`, `CaptureMode`, `reply_max_chars`, fidelity `reported` + migration `b7e1f0a2c3d4`, tests `test_log_turn.py` (13), `test_echo_free_protocol.py` (12). Migrated HTTP tests (cross-surface, oauth, latency, streamable) to `vault_log_turn`. Docs: ONBOARDING/CROSS_SURFACE preference text now names `vault_log_turn`; DEPLOYMENT lists the new env vars. E2 end-to-end over a real stdio MCP client, with a restart mid-chat: one file, reply kept, retry no-op, 20k reply stored as 8 000 `truncated`.
- 2026-10-06 · REVISED · `vault_save_turn` unlisted by default instead of a listed alias (Decisions 3).
- 2026-10-05 · FOUND · Evidence from flagged chat `6z3t53`: echo grew to 12–14k chars/turn; flag at turn 6 (double `n=6` call); later re-sends were summaries labelled verbatim. Added E-truth and E-budget; recorded that disguising the echo is out of scope.
- 2026-10-05 · FOUND · Evidence from flagged chat `6z3t53`: flag at turn 6 after ~15 k chars of copied output; chunk rule ignored (12–14 k single inputs); turn 6 retried. Added E6b reproduction script; evasion explicitly rejected.
- 2026-10-05 · REPLACED · v1 (outside MCP) superseded by this v2 (TIMELINE 67).
- 2026-10-05 · BRANCHED · B7 proposed from B6 RC6 under the MCP-only, deterministic constraint. Paths A–E scored (E 82, C 78, A 72, B 70 gate-fail on Windows, D 56, baseline 44).
