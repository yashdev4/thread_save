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
