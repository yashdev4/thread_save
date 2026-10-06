# 03 — Issues found (2026-10-05)

Found by reading the code/config; none were reported as symptoms by the owner yet, so **confirm against the live Render logs** before assuming each one is biting. "Verified" = I read the exact lines; "Inferred" = follows from the code but not observed running.

Severity: **P0** data exposure / data loss · **P1** broken or wasteful in production · **P2** correctness/consistency · **P3** hygiene.

---

## P0

### P0-1 · Deployed `/mcp` is unauthenticated and exposes every archived conversation — Verified
`render.yaml` sets `THREADVAULT_AUTH_PAUSED=true`, `ENFORCE_AUTH=false`, `ALLOWED_HOSTS=*`.
- `web/startup.py:91-95` returns early on `AUTH_PAUSED` → all fail-closed checks skipped.
- `web/middleware.py:177-194`: with no bearer token and auth paused, account = `x-account-id` header / `?account=` / `"default"`. Anyone who learns the `onrender.com` URL can call `vault_find` (returns titles+200-char snippets of all threads), `vault_save_turn` (write/poison the archive; rate limit is only 60/min per IP), and choose the account name.
- Conversations are private data; the only protection is URL obscurity.
**Fix:** minimum — static bearer token (`THREADVAULT_STATIC_TOKEN`) checked in `AccountContextMiddleware` when paused; proper — finish the OAuth path for FileStore (Google IdP needs `GOOGLE_*` + `THREADVAULT_PUBLIC_URL`; no DB required if client/token state is kept in files) and remove `AUTH_PAUSED` from prod config. Add a test.

### P0-2 · Vault is not written to the persistent disk (`THREADVAULT_ROOT` is never read) — Verified
`render.yaml:22` sets `THREADVAULT_ROOT=/data/vault`; `.env` also uses it. `config.py:149-152` only reads **`THREAD_SAVE_VAULT_ROOT`** (default `~/thread_vault`). Grep for `THREADVAULT_ROOT` in `src/` → no hits.
Consequence on Render: threads land in `~/thread_vault` on the ephemeral filesystem, **lost on every deploy/restart**; the paid 1 GB disk sits unused. Worse, after a restart the sync loop (P1-2) rebuilds `index/threads.json` and monthly indexes from only the *new* threads and pushes them, **overwriting the GitHub index of older threads** (thread pages survive because the tree uses `base_tree`, but the indexes are wrong).
**Fix:** read `THREADVAULT_ROOT` in `load_config()` (keep `THREAD_SAVE_VAULT_ROOT` as alias; document in DEPLOYMENT.md), add a startup log line of the resolved vault root, add a test. Then check the live service: `GET /health` should report the root, or look at the Render shell for `/data/vault`.

### P0-3 · GitHub private-repo guard is off by default — Verified
`export/sync_loop.py:77`: `THREADVAULT_ALLOW_PUBLIC_REPO` defaults to **true** → `verify_private=False`. If `yashdev4/thread_vault` is (or becomes) public, full conversation transcripts are published. Redaction only masks recognised secrets.
**Fix:** default to `false`; require explicit opt-in; log the repo visibility at startup. Confirm the repo is private right now.
**Status 2026-10-06: confirmed live: the repo is PUBLIC.** `GET api.github.com/repos/yashdev4/thread_vault` without auth → `"visibility": "public"`; it holds 7 real deployed-connector chats. Owner action: make it private (GitHub → Settings → Danger Zone). The code default is still unsafe (not changed yet). The mirror also contains 3 `test-account` fixture pages, so local test data was pushed to the production archive at some point.

---

### P0-4 · FileStore forgets every thread when the process restarts; resumed chats split and lose a reply — Verified (reproduced)
**Status 2026-10-06: fixed (B6 L1), deployed 2026-10-06 (`bad9e0c`, live with `ad9b812`).**
`FileStore` keeps the registry, slots, page state, anchors and gaps **in memory only** (`writer.py:386-398`), and nothing reloads them from the vault. After any restart (Claude Desktop relaunch or connector toggle for stdio; `uvicorn --reload` in `scripts/run_local.ps1`; every Render deploy or idle spin-down for B5), the next `vault_save_turn` of an existing chat misses `_registry.exists(thread_id)` (`writer.py:483`). The anchor fallback also misses, because `search_active_by_anchor` requires the thread to be in the registry (`writer.py:505-507`). The result:
1. a **new "Untitled Thread" file** is created for the same chat, starting at `n=1`;
2. `prev_response` is **silently dropped**, because it is written only when `n > 1` (`service.py:322`);
3. the old file's last reply stays `[response pending]` forever;
4. dedup state is lost, so a retry after a restart duplicates the turn (W-4).

Reproduced on 2026-10-05 by driving `python -m thread_save.server` with the `mcp` stdio client: 2 turns, restart, 1 turn → 2 files, and the reply "Here is the 3-day version…" is in neither. B6 L1 turns this into `tests/test_filestore_restart.py`. → **B6 L1**, B3, B5

## P1

### P1-9 · The latest Claude reply is never archived; `THREADVAULT_MODE=both` does nothing — Verified
**Status 2026-10-06: fixed by B7 (`vault_log_turn` after each reply), deployed 2026-10-06 (`bad9e0c`, live with `ad9b812`).**
By design (B1 turn-start lagged logging), reply *n* is written only when turn *n+1* starts, as `prev_response`. The **last reply of every chat is therefore never saved**, and a one-message chat contains only the user's line plus `[response pending — will be filled on next turn]`. The `both` mode meant to close this is a no-op: `config.mode` is read only for telemetry (`server.py:192`), the description never mentions `current_response`, and so the model never sends it. `run_tests.py` Test 11 sets `mode="both"` but calls the service with `current_response` directly, so it cannot catch this. Live evidence: `vault_local/dhanshree/…_new-chat-beginning_p01.md` turn 2. → **B6 L3**, B1 §6.3

### P1-10 · A failed or chunked first save leaves a header-only `.md` file — Verified (reproduced)
**Status 2026-10-06: fixed (B6 L2), deployed 2026-10-06 (`bad9e0c`, live with `ad9b812`).**
`FileStore._create_thread` writes the page (front matter plus `# Title`) **before** the transaction (`writer.py:454-456`). If anything in the txn then raises, W-11 discards the turns but not the file, leaving a file with no turns. A first call that is chunk-buffered (`service.py:186-196`) does the same. The stdio exception path returns `write_failed` without logging an event (`server.py:204-206`), and stderr is not captured by the Microsoft Store build of Claude Desktop (no `logs/` files), so these failures are **invisible**. → **B6 L0, L2**, B3 W-11

### P1-11 · Echoing prior output can trip the model's `reasoning_extraction` safeguard — Inferred (pattern + public reports), not yet A/B-tested
**Status 2026-10-06: mitigated by B7 path E (no request for earlier output; reply sent once, capped at 8 000). Not yet measured live (B7 E7).**
User report 2026-10-05: continuing an existing chat in Claude Desktop with the connector on, Sonnet 5.5 stops with "safeguards flagged this message … [reasoning_extraction]" ("Paused · Edit and retry"). The same happened with a teammate's independent saver (Threads-Ov `save_chat_transcript`, which asks for the **full transcript every turn**). The two designs share one thing: they tell the model to re-emit its own earlier output verbatim into a tool call. `vault_save_turn` asks for `prev_response: your previous reply … verbatim` and, above 6000 chars, to resend it in chunks "instead of shortening it" (`server.py:124,129-130`, `web/mcp_server.py:45,50-51`). That classifier guards against extracting the model's chain of thought. Public false-positive reports show it firing when a model is asked to **hand back its own completed work** (anthropics/claude-code#97503) or to quote its displayed thinking (#93584), and once it fires, later turns in the same chat keep failing. Long, resumed chats carry more prior output (and thinking) in context, which fits "happens when continuing the other chat". Impact: any echo-based design (today's `prev_response`, B6 C1/C2 `current_response`) carries this risk; it can make a chat unusable, which is worse than a missed save. **Evidence (2026-10-05):** in flagged chat `6z3t53`, turn 6 was sent twice (retry) after ~15 k chars of Claude's own output had been copied into `prev_response`; by turn 9, ~42 k. The 6 000-char chunk rule was ignored (12–14 k in single calls). Details in B7. → **B7 v2** (echo-free protocol, path E), B6 RC6, B1 §6.2

**Evidence from the flagged chat (2026-10-05, `vault_local/dhanshree/…_6z3t53_current-resistance-i_p01.md` + `events.jsonl`):**
- Size of the reply re-sent in `prev_response`, per turn: 2.1k, 2.4k, 5.1k, 0.2k, 5.0k, **12.1k**, 0.6k, **14.3k** chars. By turn 8 the context holds about 42k chars of Claude's own replies a second time, as tool inputs.
- Turn 6 ("more", 22:32 IST) has **two** `vault_save_turn` calls with `n=6` (17:02:46 and 17:05:35 UTC). That is the same message run twice, which matches a flagged reply followed by "Edit and retry". The flag came at the **6th query**, right after a 5k-char re-send, and the user then re-sent the 12k turn-5 reply.
- **After the flag, the re-sends stopped being copies.** Turns 9–10 were stored as `"[Provided 10 branch implementations …]"` and `"[Extended breakdown covering …]"` (263 and 140 chars), still labelled `fidelity=verbatim`. Either the model avoided the echo itself, or the fallback model summarised. In both cases the archive now holds **false "verbatim" content** (new P1-12).
- The same context also carried another connector's instructions to send the **full transcript every turn**, "silently", as the "highest priority directive". The chat's *topic* was testing transcript capture as well. Both add to the "copy out the conversation" signal; neither is under this server's control, so the fix must not depend on them.
- **What the classifier actually checks is not public**; this is a correlation from one chat, not a confirmed mechanism.

### P1-12 · Re-sent replies can be summaries but are stored as `verbatim` — Verified
**Status 2026-10-06: fixed (B7 E-truth: `reported`/`abridged`), deployed 2026-10-06 (`bad9e0c`, live with `ad9b812`). Gap found later: one-line prose retellings were not caught → P1-15.**
In chat `6z3t53`, turns 9–10 of `prev_response` are bracketed descriptions of the reply (`"[Extended breakdown covering …]"`, 140 chars), not the reply. The server trusts the model's `fidelity` and stores them as `verbatim`. The archive silently loses the real reply and mislabels the substitute. **Fix:** never claim `verbatim` for model-sent replies; label them `reported`. Flag likely-summaries (a whole body in `[...]`, or a very short body after a long user request) as `abridged`. → B7 E-truth

### P1-13 · Local and deployed servers shared tool names, so deployed-connector chats were saved locally — Verified
**Status 2026-10-06: fixed, deployed 2026-10-06 (`bad9e0c`, live with `ad9b812`).**
Claude Desktop runs the stdio server (`claude_desktop_config.json` → `threadvault-local`) next to the claude.ai connector. Both listed `vault_save_turn` / `vault_find` / …. On 2026-10-06 00:21–00:37 IST, four chats the owner ran "with the deployed connector" were all written by the **local** process: `vault_local/_index/events.jsonl` (stdio-only telemetry), account `dhanshree` (stdio env only). The deployed server received none of them (`vault_find` on it: 1 thread, "Greeting", 01:06 IST). **Fix:** the stdio server now lists `vault_local_log_turn`, `vault_local_backfill`, `vault_local_find`, `vault_local_stats` (and `vault_local_save_turn` under the legacy flag); the remote keeps `vault_*`. `tests/test_echo_free_protocol.py::test_local_and_deployed_tool_names_never_overlap`. A Render server cannot write to this computer; the local copy of deployed chats is pulled from the GitHub mirror by `scripts/pull_remote_vault.ps1` into `vault_rich_fixture/` (owner's chosen folder; the test fixture moved to `vault_test_fixture/` because `tests/build_fsck_fixture.py` deletes and rebuilds its folder). → B1 I-4, B4, B5

### P1-17 · A skipped `vault_log_turn` call loses the turn without a stub — Verified (probe)
**Status 2026-10-06: fixed in the working tree (B7 E8-3, `_skipped_turn`; `tests/test_two_call_turn.py`); deployed server still affected.**
`log_turn` writes gap stubs only when `turn` is ahead of `highest + 1` (`service.py` `log_turn`, the `gap_ns` branch). The model passes back the `next_turn` it was given, so when a call is skipped, the next call carries the expected number. The skipped turn disappears and the next message takes its number. `prev_user_anchor` would reveal the skip, since it names a message the server never stored, but it is used only for binding and E2b. Probe: 4-turn chat with turn 3 skipped → stored t1, t2, then message 4 as t3; no stub, no gap. The old `save_turn` protocol fails worse: reply 3 is stored under message 2. → B7 E5/E8-3, B1 storage evaluation

### P1-16 · The save tool is no longer called on every reply — Verified (regression), mechanism Inferred
**Status 2026-10-06: E8 deployed (`ab5ab9d`); the owner reports the end call is still skipped. E8-6 `missed_reply` catch-up built, uncommitted and untested; the last turn of a chat stays uncovered.**
Since path E (`bad9e0c`, deployed `ad9b812`), the model is told to call the tool **after** each reply. The owner reports it fires only when they name the connector. Under the old start-of-reply trigger (2026-10-05), every chat opened with an unprompted `n=1` call and numbering ran unbroken. Chat `mt8f50` (2026-10-06) began only on the user's explicit request. Separate defect: a first call made mid-chat is numbered turn 1, so `vault_local_backfill` had no earlier slots and stored nothing. → B7 E8, B1

### P1-15 · Logged replies are condensed one-line retellings, not the reply as shown — Verified
**Status 2026-10-06: fix implemented (option 1, Markdown reply contract + exact parser), uncommitted, not deployed. Verify with live chats (stored `reply_shape`).**
Chat `eatz7d` (local, `vault_local_log_turn`): all 4 stored replies are a single line of 793–1 054 chars with no Markdown, e.g. "Five key themes … (1) … (2) …", although the user asked "more" and "extend". The server stores Markdown exactly (round-trip probe), so the model is filling `reply` with a summary. The `reply` field has no schema description; the tool text says "as shown to the user" and "The server shortens long replies". `classify_reply` only flags bracketed lines, so these are labelled `reported`. Related read-path bug: `formatter.parse_turns` strips first-line indentation and drops `[… — not archived]`-shaped lines (B3 WD). → B7, B3

### P1-14 · Claude Code sessions were archived as chats — Verified
**Status 2026-10-06: fixed; server guard deployed 2026-10-06 (`bad9e0c`, live with `ad9b812`); Claude Code setting applied.**
Claude Code loads the user's claude.ai connectors into every coding session, and the server instructions ("At the start of each reply, call vault_save_turn") arrive with them. Evidence from `~/.claude/projects/d--Dev-Projects-2026-eoxs-thread-save/*.jsonl`: four Claude Code sessions called `mcp__claude_ai_thread__vault_save_turn` 11 times. Session `4d475c2c` (VS Code, 14:20–14:56 UTC, 6 calls) created thread `01M466ZDBPTY0PG9JCGVKTMNR7` "Run ThreadVault locally for testing"; `83487ad1` (CLI) created `…NWKG42`; `a9abbb85` created `…G27WKM`. All three are on the deployed server and in the **public** mirror. Real Claude Code 2.1.289 traffic (captured on a throwaway local server): protocol `2026-07-28`, no `initialize`; every request carries `_meta["io.modelcontextprotocol/clientInfo"] = {"name": "claude-code", …}` and `User-Agent: claude-code/2.1.289 (claude-vscode, agent-sdk/0.3.289)`.
**Fix (two layers):** (1) Claude Code user settings `deniedMcpServers` = `{serverName: "claude.ai thread"}` + `{serverUrl: "https://thread-save.onrender.com/*"}`, so Claude Code no longer loads the connector (verified: gone from `claude mcp list`). (2) Server guard `tools/clients.py` `ArchiveServer`: clients matching `THREADVAULT_SKIP_CLIENTS` (default `claude-code`, matched on clientInfo name from the handshake or `_meta`, or User-Agent) get no write tools in `tools/list`, and a forced write call returns `{"ok": true, "skipped": "not_archived_client"}`. Verified with real Claude Code (write tools withheld) and on the running local server. Server instructions now say coding-agent sessions are not archived. **Unverified:** the identity the deployed server sees when Claude Code reaches it through claude.ai's connector path; the guard logs `tools/list for client …: archived | write tools withheld` and, since the web app now sends `thread_save` INFO to stdout/stderr, Render's log will show it on the first deployed day. Layer 1 covers that path regardless. Tests: `tests/test_client_filter.py` (9) cover stdio and the deployed HTTP app. → B1 I-10, B5

### P1-1 · `alembic` is not a declared dependency, but the deploy runs it — Verified
`requirements.txt`/`pyproject.toml` list no `alembic`/`sqlalchemy`. `render.yaml` `preDeployCommand: python -m alembic upgrade head`, `fly.toml` `release_command`, and `Dockerfile` CMD all call it. On a clean build → `No module named alembic` → release fails. (It works locally only because alembic 1.20/SQLAlchemy 2.1 are installed in the dev env.) Commit `77ec4bd` restored the preDeploy command without fixing deps. Also pointless in file mode (no `DATABASE_URL`).
**Fix:** add `alembic`, `sqlalchemy`, `psycopg2-binary` (whatever `alembic/env.py` needs) to dependencies, **or** drop `preDeployCommand` while on the file backend; make the command conditional on `DATABASE_URL`.

### P1-2 · Sync loop makes a commit every 60 s forever and ignores all the GitHub policy code — Verified
`sync_loop.py:130-135` → `push_batch` every interval with the **entire tree**, no manifest/hash comparison; `push_batch` always creates tree+commit+ref update, so an unchanged vault still yields a new commit (~1 440/day, 3 API writes each). None of `GitHubBatchExporter` (settle window, batch cadence, unchanged-page skip, conflict detection via blob SHA, dead-lettering on push-protection, size guard) or the D4 `THREADVAULT_GH_SETTLE/BATCH_MINUTES` vars is used here. Human edits on GitHub are silently overwritten (`last_blob_sha` is never set). Repo size/history will balloon; the squash CLI only helps afterwards.
**Fix:** reuse `GitHubBatchExporter` with a `FileStoreExportManifest` (already exists in `export/offload.py`) in the loop; or at minimum skip when the computed tree hash equals the last pushed one. Add tests for the loop (none exist).

### P1-3 · Empty GitHub repo bootstrap is probably still broken — Inferred
`push_batch` treats 404/409 on the ref as "empty repo" and then `POST /git/trees` with `base_tree` omitted. GitHub's Git Data API typically rejects tree/commit creation in a repo with **no commits** (409 "Git Repository is empty"); the usual workaround is to create the first file through the Contents API. Commit `53284c7` ("robust empty repo initial commit") touched this but there is no test with a 409 on `POST /git/trees`. Also the branch is hard-coded `main`; a repo whose default branch is `master` yields a second branch.
**Fix:** on 409 from tree creation, `PUT /repos/{repo}/contents/README.md` to seed the repo, then retry; make branch configurable (`THREADVAULT_GH_BRANCH`) and detect the repo default branch.

### P1-4 · `vault_find` returns dead viewer/download links in file mode — Verified
`web/mcp_server.py:173-181` always adds `viewer_url=/v/{token}` and `download_url`, but `app.py:158-159` mounts the viewer router only for `PgStore`. Links 404; they are also relative (no `THREADVAULT_PUBLIC_URL` prefix), so Claude can't give the user a clickable URL even with Postgres. Token signing uses `VIEWER_SECRET_DEFAULT` = hard-coded fallback string when `THREADVAULT_VIEWER_SECRET` is unset (`viewer.py:30-34`) — the "no built-in default keys" claim only holds for the redaction key.
**Fix:** omit links when no viewer router is mounted; prefix with public URL; fail startup without the viewer secret in prod.

### P1-5 · Multi-worker + in-memory state — Inferred
`Dockerfile` runs `--workers 2`. With `FileStore`, thread registry, chunk buffer (`service.py:_ChunkBuffer`), rate limiter and active-thread index are per-process; two workers can race on the same files. Render's start command is single-worker (fine). Safe only with `PgStore` (advisory locks).
**Fix:** `--workers 1` unless `DATABASE_URL`; or document.

### P1-6 · Distinct accounts can merge in PgStore (W-1) — Verified (logic), not reproduced end-to-end
`PgStore.resolve_account_uuid` (`pg_store.py:435-464`) looks an account up **by `sanitize_slug(account_id, 20)`**. `sanitize_slug` lowercases, replaces punctuation and truncates: `acme-engineering-team-alice` and `acme-engineering-team-bob` both become `acme-engineering-tea`; `Alice.Smith` and `alice-smith` both become `alice-smith` (checked with Python). The second account then reads and writes the first one's threads. The same function also runs `ON CONFLICT (id) DO UPDATE SET slug = …`, which would overwrite an OAuth account's email-derived slug on its first save (inferred). Not live today (production is FileStore), but this becomes **P0 the moment PgStore is deployed**.
**Fix:** resolve by id / `oauth_sub` only; never update `slug`; add a test with two accounts that share a 20-char prefix. → B3 W-1

### P1-7 · Stale chunked responses are never flushed; Test 13 is hollow — Verified
`_ChunkBuffer` (`service.py:50-90`) stores `started_at` but never reads it; no `_flush_stale_chunks` exists. `run_tests.py` Test 13 calls it only `if hasattr(...)` and asserts nothing. A chunked reply whose final chunk never arrives is silently dropped (plan: store as `truncated` after 5 min). The buffer is also in-memory, so a restart loses it, and the `turn_chunks` table is unused. → B0 §4.3, B3 WD

### P1-8 · Envelope encryption is never applied to turn bodies — Verified
`security/encryption.encrypt_body` has no callers on the write path; only `encrypt_field` is used, for GitHub tokens (`export/auth.py:115`). Setting `THREADVAULT_MASTER_KEY` / `THREADVAULT_ENVELOPE_ENCRYPTION` only switches `vault_find` to titles-only search (`service.py:464-480`); bodies remain plaintext in Postgres. Ledger X6/H6 overstate this.
**Fix:** encrypt in `PgThreadTxn.upsert_turn`, decrypt in the renderer/viewer/`get_turns`; or remove the claim from the docs. → B2 S8

---

## P2

- **P2-1 Duplicate/contradictory description lines.** `_SAVE_TURN_DESC` has two near-identical "missing turns… vault_backfill" sentences (`server.py:133-134`, `mcp_server.py:54-55`). Server instructions say "local markdown archive" on the remote server. `test_instruction_style.py` only checks forbidden words / ≤80-char opening, so it won't catch this. Because Claude reads these every turn, trim for tokens/clarity.
- **P2-2 Missing imports under `from __future__ import annotations`.** `config.py:82` uses `Optional` (not imported); `service.py:101` and `mcp_server.py:184` use `Any` (not imported). No runtime failure today (annotations aren't evaluated) but breaks `typing.get_type_hints`, pydantic/dataclass introspection and any linter/type-check.
- **P2-3 Default JWT secret literal in config.** `config.py:72,190` falls back to a published string whenever `AUTH_PAUSED` or non-prod; fine locally, unsafe if auth is ever re-enabled without the secret in the Render dashboard. Prefer random-per-process + loud warning.
- **P2-4 Middleware/limits drift.** `MAX_BODY_BYTES` = 2 MB (code) vs "4 MB / 10 MB" in ledger/status report; BodySize check trusts `Content-Length` only (chunked bodies bypass). Rate-limit key trusts client-supplied `x-account-id` header, so an attacker can rotate the header to dodge the limit.
- **P2-5 Multi-account paths in sync.** `sync_loop.run_sync_once` treats every non-dot/underscore dir in the vault as an account and switches layout to `account_dir=True` when >1; with auth paused and attacker-controlled `x-account-id`, junk account dirs get created and published (slug validation exists in `path_resolver.py` but verify it covers header-provided values).
- **P2-6 `THREADVAULT_ALLOWED_HOSTS=*` + disabled FastMCP DNS-rebinding protection** (`app.py:85`) — acceptable only because the Origin/Host middleware exists *and* auth is on; with P0-1 nothing remains.
- **P2-7 Python version skew.** Dev machine 3.14.4 + mcp 2.2.0; Dockerfile 3.11-slim; `requirements.txt` has `mcp[cli]>=1.0.0` (unpinned; `mcp.server.mcpserver` import only exists in 2.x). Pin `mcp>=2` and set a Render `PYTHON_VERSION`.

- **P2-8 Rate limiter is effectively global.** A single `RateLimiter(60/min)` keyed by the `x-account-id` header or client IP (`middleware.py:121-140`). All connector traffic comes from Anthropic's egress IPs, so every user shares one bucket, and the ledger's "60 saves / 120 reads per account" isn't what the code does. Key by the authenticated account. → B2 S8
- **P2-9 Remote mode records no telemetry.** `web/app.py:68` builds `TurnService` without an `EventLogger`, so `events.jsonl` / `report.py` coverage metrics exist only for stdio. → B1 §7
- **P2-10 Destination wording is wrong in both transports.** Stdio `vault_save_turn` says "in their ThreadVault account" (`server.py:119-120`), but stdio writes local files. The remote server instructions say "local markdown archive" (`web/mcp_server.py:34-37`), but remote stores on the server. Plan rule B1 §6.1 / B2 S8: state the real destination. → B1
- **P2-11 Background jobs never started.** `OutboxWorker`, `purge_expired_threads`, `reap_orphans` have no callers in `web/` or `server.py`; `GoogleDriveExportTarget.export_thread` returns `True` without uploading. → B2 X7, B3 WA

- **P2-12 FTS index blanks the title, and front matter is stale.** `server.py:_do_index` upserts the thread with `title=title_hint or ""`, `account=""`, `path=""` on every turn. From turn 2 on, the title is `""` (seen in `vault_local/_index/threads.sqlite` and in `vault_find` output). Only user turns are indexed. In front matter, `turn_range` stays `[1, 0]` (`writer.py:443`) and `turn_count` counts slots, not turns. → B6 L4 **Fixed 2026-10-06 (B6 L4).**
- **P2-13 Telemetry misreports.** `events.log(binding=…)` derives the binding from the *argument* (`"new" if not thread_id else "id"`, `server.py:193`), not the result. A restart-split (P0-4) is logged as `id`, so `report.py` can't see it. `bytes` is always 0, and failures are not logged at all (P1-10). → B6 L0, B1 §7 **Fixed for stdio 2026-10-06 (B6 L0).**

## P3 hygiene
- `PROGRESS.md` staged-deleted + `PROGRESSs.md` untracked (see 02-current-state). Decide the canonical name; remove `PROGRESS.md` from `.gitignore` if it should live in the repo.
- Root-level `patch*.py`, `inject_tests.py`, `add_protocol_methods.py`, `test_3_5.py`, ledgers of generated output; move `run_tests.py`/`test_smoke.py` under `scripts/`; `vault_rich_fixture/` → `tests/fixtures/`.
- `.gitignore` has `.docs/` (dot-prefixed) — irrelevant to `docs/claude/`, which is tracked.
- `STATUS_REPORT_EN/HINGLISH.md` are stale (see drift table) — regenerate or mark as "as of c8d2b4b".
- `claude_desktop_config.example.json` hard-codes a personal Windows path and account `dhanshree`; make it a template.
