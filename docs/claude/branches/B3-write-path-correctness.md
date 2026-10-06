# B3: Write-path correctness

- **Source:** `write_path_correctness_plan.md`
- **Parent:** B0 §11 (25% write path), re-scoped by B2: the per-file mutex became transactions plus an advisory lock, atomic append became `ON CONFLICT`, and pagination moved to render time with sticky pages.
- **Runs inside:** B2 X2 (W1–W5) and X5 (W6).
- **Principle:** corruption and merged threads are unrecoverable; a missed turn is not. Trade coverage for zero corruption.

## Invariants (W-1 … W-11)
| ID | Invariant | Checked by | Status |
|---|---|---|---|
| W-1 | Every turn is in one thread, every thread in one account; never reachable cross-account | RLS + `tests/test_tenant_isolation.py` + fsck | ⚠ `PgStore.resolve_account_uuid` (`pg_store.py:435-464`) resolves an account **by its 20-char sanitised slug**. `'acme-engineering-team-alice'` and `'…-bob'` both become `acme-engineering-tea`, and `'Alice.Smith'` / `'alice-smith'` collide too (verified with `sanitize_slug`). Distinct account strings therefore **merge into one account**. `03-issues.md` P1-6 |
| W-2 | User `n` dense 1..max (stubs fill holes) | fsck (file + `--dsn`) | ✅ |
| W-3 | `(n, assistant)` only if `(n, user)` exists | fsck | ✅ |
| W-4 | A retry never changes state beyond the first call | Hypothesis, Test 3 | ◐ ⚠ PgStore ✅. **FileStore: only within one process.** Slots and registry are memory-only (`writer.py:386-398`), so after a restart a retry or resumed turn creates a new thread and drops `prev_response` (reproduced). P0-4 → B6 L1 |
| W-5 | A legitimate repeat ("continue") is never deduplicated | `turn_key` triple; Tests 3.5/3.6 | ✅ |
| W-6 | A turn's page never changes; user and assistant of the same `n` share a page | sticky page columns; fsck | ✅ |
| W-7 | `turns.hash == hash_v{k}(body)` | fsck recomputes | ✅ |
| W-8 | Same rows → byte-identical Markdown | golden files `tests/test_w6_renderer.py`; differential tests | ✅ |
| W-9 | Body cannot forge or break delimiters or front matter | nonce `k=`, `QuotedDumper` | ✅ |
| W-10 | A deleted thread is never recreated | `deleted_threads` tombstone | ✅ |
| W-11 | A save fully commits (turns + gaps + counters + outbox) or leaves no trace | single transaction; fault-injection test | ◐ ⚠ PgStore ✅. FileStore: turns are atomic, but `_create_thread` writes the page **before** the txn (`writer.py:454-456`), so a failed or chunk-buffered first save leaves a header-only `.md` (reproduced). P1-10 → B6 L2 |

## Branch detail (WA–WH)
| Branch | Decision | Status | Evidence / deviation |
|---|---|---|---|
| WA | ULID; short id from the random **tail** | ✅ | `storage/identity.py:33-35` |
| WA | Account slug assigned once at first login, then **immutable**; collision → suffix | ◐ ⚠ | OAuth path does this (`web/oauth.py:477-483`, random 4-hex suffix rather than numeric). But `resolve_account_uuid` runs `INSERT … ON CONFLICT (id) DO UPDATE SET slug = EXCLUDED.slug` with a slug derived from the account-id string. For an OAuth account (account id = UUID), the first save would **overwrite the email-derived slug** with a UUID-prefix slug. Inferred from code, not reproduced |
| WA | Turn-1 retry → split, then an hourly janitor reaps the orphan | ◐ | `storage/orphan.py` `reap_orphans` (24 h, ±2 min, same turn-1 hash, other thread has turn 2) is tested, but **nothing calls it** |
| WA | Tombstones; anchor search skips tombstoned threads | ✅ | `deleted_threads`; ledger X2/X6 |
| WB | Per-thread `pg_advisory_xact_lock` | ✅ | `pg_store.py` |
| WB | `turn_key = hash(anchor ‖ query ‖ hash(prev_response))` | ✅ | `turns.turn_key` + unique partial index (`ddd36e757df0_init_schema.py:64,69`) |
| WB | Latest-match anchor inside a bound thread; any multi-match across threads → new thread | ✅ | `SlotIndex.find_user_turn_by_anchor`; Test 17 |
| WB | `n = threads.max_n + 1` under the lock | ✅ | `threads.max_n` (`init_schema.py:41`) |
| WC | One call = one transaction, in the planned order | ✅ | `PgThreadTxn` |
| WC | `SET LOCAL statement_timeout 4s`, `idle_in_transaction 5s` | ✅ | `pg_store.py:86-87` |
| WC | `SET LOCAL` only (no pooled leak) | ✅ | `set_config('app.account_id',$1,true)` (ledger X2) |
| WC | Transactional outbox | ✅ | enqueue in txn, 2-min debounce. The consumer is never started (B2 X7) |
| WC | Telemetry after commit | ◐ | Stdio only; remote has no telemetry (B1 §7) |
| WD | `canonical_v1`: NFC, CRLF→LF, one trailing LF, keep leading whitespace | ✅ (read path fixed 2026-10-06, uncommitted) | `security/idempotency.py:32-47`; NUL stripping added (H1). **Read path breaks it (2026-10-06):** `formatter.parse_turns` does `.strip()` on the body (`formatter.py:127,137`), losing the first line's indentation, and drops any body line shaped `[… — not archived]`. Writes are exact; restore (B6 L1 hashes), `cli/migrate.py` → Postgres and fsck read the lossy version. No write→parse round-trip test existed. **Fix:** `formatter._turn_body` removes only the frame `format_turn` writes (role heading, one blank line, exactly `attachments=N` marker lines) and keeps the body; CRLF pages parse like LF. `tests/test_turn_roundtrip.py` (12 incl. Hypothesis, restart hash); 7 of them fail on the old parser. Real archives: `vault_local` 12/12 and mirror 21/21 header hashes match the parsed bodies |
| WD | Hash = `"v1:" + sha256[:16]` | ◐ DIVERGED | Full 64-hex sha256 with no prefix (`idempotency.py:50-53`); version is in the `turns.hash_version` column; 8-char display hash in comments. fsck accepts an 8-char or 64-char match |
| WD | Order: canonicalise → redact → hash → store | ? | Not re-verified |
| WD | HMAC masks `HMAC(server_key, secret)[:4]` | ✅ | `security/redactor.py:85-125`. No built-in default key (pre-deploy safety); ephemeral key locally. The unapplied `patch_hmac.py` at the repo root would hard-code `threadvault_dev_key`; **do not run it** |
| WD | Never reject for size; over the limit → prefix + `truncated` | ◐ | Limit is `MAX_BODY_CHARS = 100_000` **chars** (plan said ~256 KB) — `pg_store.py:176`, `writer.py:171` |
| WD | Chunks in `turn_chunks(thread_id,n,role,idx)`; assemble in txn; unfinished after 5 min → `truncated` | ❌ | Table exists in the schema but is **unused**. Chunks live in the in-memory `_ChunkBuffer` (`service.py:50-90`): lost on restart, not shared between workers, never flushed (B0 §4.3, `03-issues.md` P1-7) |
| WE | Sticky pages: page assigned at user-turn creation; counters on the thread; backfill never moves turns | ✅ | `threads.current_page/_turns/_bytes`, `turns.page` |
| WF | YAML through a library with explicit quoting; strip control chars from titles | ✅ | `storage/formatter.py` `QuotedDumper(SafeDumper)`; `tests/test_w6_renderer.py` |
| WF | Per-thread delimiter nonce | ✅ | `threads.delim char(4)`, `formatter.py:88` |
| WF | Round trip `parse(render(rows)) == rows` | ✅ | used by `cli/migrate.py`; `tests/test_migration_cli.py` |
| WG | Deletion cascade + export deletion | ◐ | DB cascade ✅; GitHub file deletion in batch (B4 GH5); outbox not running in prod |
| WG | Expand/contract migration policy doc | ❌ | Not found |
| WG | PITR + **restore drill before launch** | ❌ ⏸ | Only documented (`docs/DEPLOYMENT.md` §2). W7 not done |
| WG | FileStore on Windows: retry `os.replace`; fsync the directory | ✅ | `writer.py:705-728` |
| WH | `fsck` (file + `--dsn`) | ✅ | `src/thread_save/fsck.py`; H1 |
| WH | Hypothesis vs a reference model, both stores | ◐ | 1,000 examples per store. Exit criterion is **10,000** |
| WH | Concurrency, fault injection, differential tests | ✅ | `tests/test_pg_store.py` |
| WH | Nightly fsck in production with alerting | ❌ | Not scheduled |

## Exit criteria (plan §6): **not met**
- [ ] fsck clean after 10,000 Hypothesis sequences (currently 1,000)
- [ ] golden files unchanged across two releases (no releases yet)
- [ ] one week of production with nightly fsck (production does not use PgStore)

## Open items owned by B3
1. **W-1:** resolve accounts by `oauth_sub`/id only, never by slug; never update an existing slug; add a test with two accounts whose names share a 20-char prefix.
2. Move chunk assembly into `turn_chunks` inside the txn, with the 5-min `truncated` flush.
3. Schedule the orphan reaper and the nightly fsck.
4. Write the migration policy doc; do the PITR restore drill (W7).
5. Raise Hypothesis to the 10k profile for release gating.
7. If B7 is approved: one txn per turn writes both slots; drop the `turn_chunks` plan (item 2) instead of building it.
6. FileStore restart safety (W-4) and atomic creation (W-11): B6 L1/L2. Add a two-instance restart case to the FileStore Hypothesis model.

## Change log (newest first)
- 2026-10-06 · FOUND/ADDED · P1-18: a call that lost or mistyped its `thread_id` opened a new file (deployed mirror: 7 one-turn files from a few chats). FileStore binding now recovers it by short id / near id (`id_fuzzy`), or by the single thread active in the last 6 h whose latest user message the call names (`recent`). Never on a chat's first call; never on ambiguity (I-3 kept). PgStore not yet. Uncommitted.
- 2026-10-06 · FIXED · WD read path: exact body round trip (`formatter._turn_body`), `tests/test_turn_roundtrip.py`; uncommitted.
- 2026-10-06 · FOUND · WD ✅ → ◐: parser strips first-line indentation and drops `[… — not archived]`-shaped lines; only `canonical_v1` had been checked.
- 2026-10-06 · REVISED · Migration tests build their fixture per run (were reading a stale folder that now holds the deployed mirror). Full suite: 162 passed, 4 skipped; run_tests 21/21.
- 2026-10-06 · REVISED · W-4 and W-11 restored for FileStore (B6 L1/L2): restart restore, atomic first page, in-memory rollback, no-op commits skip disk. New fidelity rank `reported`=4, `verbatim`=5 (migration `b7e1f0a2c3d4`).
- 2026-10-05 · BRANCHED · B7 proposal would make WD chunk assembly moot (no chunk fields) and remove `open` slots.
- 2026-10-05 · FOUND · B6 field report: FileStore W-4 holds only within one process (restart → split + lost reply); W-11 broken by page write in `_create_thread`. Both reproduced.
- 2026-10-05 · FOUND · Audit: W-1 slug-merge risk (verified), slug overwrite (inferred), `turn_chunks` unused and no stale flush, reaper/nightly fsck unscheduled, hash format and size limit diverge from plan.
- 2026-10-05 00:32 · ADDED · F1 `df2a850` fsck runs inside the PgStore Hypothesis and concurrency tests.
- 2026-10-04 22:35 · ADDED · H1 `4ed05ca` Hypothesis on both stores + `fsck --dsn`; NUL stripping.
- 2026-10-04 17:35 · ADDED · X2 `0f86b4b` PgStore with W-1…W-11 schema.
- 2026-10-04 17:18 · ADDED · `33163b5` "Part A": canonical_v1, QuotedDumper, sticky pages, orphan reaper, fsck for FileStore.
