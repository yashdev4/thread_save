# Implementation & Milestone Progress Log

## Needs User (External Gates)
- **Gate (a) Paid / Deployment (Milestone X8)**: Deploying to cloud host (Fly.io / Render / AWS) and configuring production domain & DNS. Local development, Docker container, `/health` endpoint, and config/PITR documentation run locally.
- **Gate (b) Live OAuth with Claude (Milestone X4)**: Registering the server URL on live `claude.ai` web connector settings and performing interactive browser login with Anthropic servers. All OAuth 2.1 endpoints (metadata, DCR, PKCE, token refresh, RLS session binding) implemented and tested locally.
- **Gate (c) Real Drive / GitHub Credentials (Milestone X7)**: Providing actual Google Drive service account / OAuth tokens or GitHub Personal Access Tokens. Outbox exporter implemented, tested against fake/mock target and filesystem exporter, with error retry and crash safety verified.
- **Gate (d) Live Local Vault Import (Milestone X10)**: Running `import_local_vault` against user's private live production vault. CLI implemented with `--dry-run` default and tested against local test vaults.
- **Gate (e) Live Cross-Surface Tests (Milestone X9)**: Executing live Claude mobile/desktop interactions across devices. Verification script, test scenarios, and manual QA checklist provided.

---

## Part A: Close Write-Path for FileStore
- **Status**: Completed
- **Files & Functions**:
  - `src/thread_save/security/idempotency.py`: `canonical_v1` (NFC Unicode normalisation, CRLF/CR -> LF, trailing newlines stripped with exactly one LF, leading whitespace preserved), `hash_v1`, `turn_key_v1`.
  - `src/thread_save/storage/formatter.py`: `QuotedDumper(yaml.SafeDumper)` ensuring clean YAML output without `!!python` tags.
  - `tests/test_write_path_invariants.py`: `test_sticky_pages`, `test_orphan_reaper`, `test_v1_legacy_file_handling`.
  - `tests/test_hmac_and_key_rotation.py`: Added `test_canonical_v1_indented_vs_unindented_code`.
  - `tests/test_w6_renderer.py`: Added `test_quoted_dumper_subclasses_safedumper_and_no_python_tags`.
  - `tests/build_fsck_fixture.py`: Generated rich fixture vault `vault_rich_fixture` with 3 pages, stubs, recovered turn, truncated turn (105k chars), redacted turn (HMAC mask), schema v1 file.
  - `src/thread_save/fsck.py`: Invariant checker scanning `*_p[0-9][0-9].md`, checking dense turns, W-7 recomputed hashes, W-9 delimiters, stub/gap consistency.
- **Test Results**:
  - `run_tests.py`: 21 passed, 0 failed.
  - `tests/test_protocol.py`: 11 passed, 0 failed.
  - `tests/test_w6_renderer.py`: 5 passed, 0 failed.
  - `tests/test_hmac_and_key_rotation.py`: 4 passed, 0 failed.
  - `tests/test_slug_validation.py`: 4 passed, 0 failed.
  - `tests/test_write_path_invariants.py`: 3 passed, 0 failed.
  - Hypothesis (`tests/test_hypothesis_v2.py`): 1,000 examples passed cleanly (0 shrunk failures).
  - `fsck`: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Strict preservation of indentation in `canonical_v1` as required by spec so indented vs unindented code produce distinct hashes.
  - FileStore implements `Store` protocol and `ThreadTxn` with atomic writes, in-memory transaction buffer, and directory fsync.

---

## Milestone X2: PgStore Implementation & Verification
- **Status**: Completed
- **Done When Condition**: Suite passes on both stores (FileStore and PgStore), differential markdown test passes, fault-injection and RLS isolation tests pass.
- **Files & Functions**:
  - `alembic/versions/ddd36e757df0_init_schema.py` & `978b10d4cad9_add_anchor_to_turns.py`: Complete database schema with `accounts`, `threads`, `turns`, `gaps`, `turn_chunks`, `outbox`, `deleted_threads`, `events`, and Row-Level Security (`FORCE ROW LEVEL SECURITY`).
  - `src/thread_save/storage/pg_store.py`: `PgStore` and `PgThreadTxn` implementing `Store` and `ThreadTxn` protocols:
    - Single transaction per save (`BEGIN` ... `COMMIT`/`ROLLBACK`).
    - Statement & idle transaction timeouts (`4s` / `5s`).
    - Parameterized RLS via `SELECT set_config('app.account_id', $1, true)`.
    - Per-thread advisory transaction lock `pg_advisory_xact_lock(hashtext(thread_id))`.
    - Single statement fidelity-ranked upsert (`INSERT ... ON CONFLICT DO UPDATE WHERE ...`).
    - Sticky pagination (`current_page`, `current_page_turns`, `current_page_bytes`).
    - Tombstones via `deleted_threads` (W-10).
    - Transactional export enqueue into `outbox` with 2-minute debounce (W-11).
  - `src/thread_save/storage/renderer.py`: `render_thread_markdown` projecting database rows into canonical Plan v2 Markdown.
  - `src/thread_save/service.py`: Store-agnostic `TurnService` wired with `enqueue_export()` and `txn.recover_gap()`.
  - `src/thread_save/storage/writer.py`: Fixed `FileThreadTxn` `REPLACE` regex boundary matching to search closing tag strictly after opening tag.
  - `tests/test_pg_store.py`: 11 tests verifying happy path, continue x3, duplicate idempotency, interleaved chats, gaps & backfill, tombstone after delete, fault injection rollback, RLS pooled isolation, outbox enqueue, advisory lock concurrency, and differential comparison against `FileStore`.
- **Test Results**:
  - `tests/test_pg_store.py`: 11 passed, 0 failed.
  - `tests/test_protocol.py`: 11 passed, 0 failed.
  - `run_tests.py`: 21 passed, 0 failed.
  - Invariant tests (`test_w6_renderer.py`, `test_hmac_and_key_rotation.py`, `test_slug_validation.py`, `test_write_path_invariants.py`): 16 passed, 0 failed.
  - Total pytest suite: 38 passed in 4.73s.
  - Hypothesis (`tests/test_hypothesis_v2.py`): 1,000 examples passed cleanly in 37.66s.
  - `fsck`: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - `set_config('app.account_id', $1, true)` inside transaction blocks used for safe parameter binding in asyncpg without leaking between pooled connections.
  - Integer fidelity ranking mapped directly between `Fidelity` enum `.rank` property and PostgreSQL `smallint` column.
  - `total_turns` in stats counts completed user and assistant turns excluding the provisional `Fidelity.OPEN` placeholder.

