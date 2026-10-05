# Implementation & Milestone Progress Log

## Needs User (External Gates)
- **Gate (a) Paid / Deployment (Milestone X8)**: Deploying to cloud host (Fly.io / Render / AWS) and configuring production domain & DNS. Local development, Docker container, `/health` endpoint, and config/PITR documentation run locally.
- **Gate (b) Live OAuth with Claude (Milestone X4)**: Registering the server URL on live `claude.ai` web connector settings and performing interactive browser login with Anthropic servers. All OAuth 2.1 endpoints (metadata, DCR, PKCE, token refresh, RLS session binding) implemented and tested locally.
- **Gate (c) Real Drive / GitHub Credentials (Milestone X7)**: Providing actual Google Drive service account / OAuth tokens or GitHub Personal Access Tokens. Outbox exporter implemented, tested against fake/mock target and filesystem exporter, with error retry and crash safety verified.
- **Gate (d) Live Local Vault Import (Milestone X10)**: Running `import_local_vault` against user's private live production vault. CLI implemented with `--dry-run` default and tested against local test vaults.
- **Gate (e) Live Cross-Surface Tests (Milestone X9)**: Executing live Claude mobile/desktop interactions across physical devices. Automated regression test harness provided in `scripts/cross_surface_test.py`; manual live QA checklist provided in `docs/CROSS_SURFACE_TESTING.md`.
- **Gate (f) Google OAuth App Setup**: Creating the Google Cloud Console Web Application OAuth client with exact redirect URI `https://<domain>/oauth/callback/google`, setting consent screen to minimal scopes (`openid`, `email`), and adding user to Test Users list (documented in `docs/DEPLOYMENT.md` §6).
- **Gate (g) Live GitHub Archive Verification (Milestone GH8)**: User creates a private throwaway repo on GitHub, generates a fine-grained PAT with Contents (read & write), and runs `python scripts/verify_github_live.py` (or sets `THREADVAULT_GH_LIVE_REPO` and `THREADVAULT_GH_LIVE_TOKEN`). Documented in `docs/GITHUB_ARCHIVE_VERIFICATION.md`.


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

### Milestone X3: Streamable HTTP Transport and Web API
- **Status**: Completed
- **Done When Criteria**:
  - `POST /mcp` against running instance with test token saves a turn to `PgStore` and returns `ok: true`.
  - Unauthenticated request returns 401.
  - Invalid Origin returns 403.
  - `/health` returns 200 with `status: ok` and `db: ok`.
- **Files & Functions**:
  - `src/thread_save/web/context.py`: `current_account_id` ContextVar scoping authenticated account IDs to asyncio request tasks.
  - `src/thread_save/web/middleware.py`:
    - `OriginValidatorMiddleware`: DNS rebinding protection verifying Host, Origin, and allowed CORS origins.
    - `BodySizeLimitMiddleware`: Rejects requests exceeding 4 MB with 413 Payload Too Large.
    - `RateLimitMiddleware`: In-memory leaky-bucket / window rate limiting (60 saves/min, 120 reads/min per account).
    - `AccountContextMiddleware`: Bearer token extraction setting `current_account_id`.
  - `src/thread_save/web/mcp_server.py`: `create_http_mcp_server` exposing `vault_save_turn`, `vault_backfill`, and `vault_find` with honest remote storage descriptions via FastMCP.
  - `src/thread_save/web/app.py`: `create_app` mounting FastMCP's Streamable HTTP app at `/mcp`, integrating `/health` with asyncpg DB connectivity checks and commit hash reporting, with ASGI lifespan management.
  - `tests/test_streamable_http.py`: 5 tests covering health check, origin validation, body size limit, rate limiting, and complete MCP turn-save protocol over HTTP.
- **Test Results**:
  - `tests/test_streamable_http.py`: 5 passed, 0 failed.
  - Pytest full suite: 45 passed in 41.63s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - FastMCP's ASGI router mounted at root `""` so its internal `/mcp` route is directly accessible as `POST /mcp` without redirect loops.
  - Asyncpg connection pool initialized and health-checked in lifespan context manager.

### Milestone X4: OAuth 2.1 (Discovery, DCR, PKCE, Refresh, Token Identity & RLS)
- **Status**: Completed (Automated verification complete; External Gate (b) recorded under Needs User)
- **Done When Criteria**:
  - RFC 8414 & OpenID Connect discovery endpoints return valid metadata.
  - RFC 7591 Dynamic Client Registration issues valid client credentials and validates redirect URIs.
  - RFC 7636 PKCE Authorization Code flow enforces S256 challenge verification.
  - Token endpoint issues signed JWT access tokens and rotatable refresh tokens; rejects single-use code replay and PKCE mismatches.
  - Authenticated `/mcp` requests extract account identity from token and enforce PostgreSQL Row-Level Security per request.
  - Unauthenticated / invalid token requests return 401 Unauthorized.
- **Files & Functions**:
  - `src/thread_save/web/oauth.py`:
    - `OAuthServer`: Manages client registrations, auth codes, refresh tokens, and JWT issuance.
    - `verify_pkce`: Cryptographic verification of `code_verifier` against SHA-256 `code_challenge`.
    - `create_oauth_router`: APIRouter mounting `/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`, `/oauth/register`, `/oauth/authorize`, `/oauth/token`.
  - `src/thread_save/web/middleware.py`:
    - `AccountContextMiddleware`: Validates Bearer JWTs via `OAuthServer`, binds verified account identity to `current_account_id` ContextVar, and returns 401 on invalid/expired tokens or when auth is required.
  - `src/thread_save/web/app.py`: Integrated OAuth router and configured `AccountContextMiddleware` with `OAuthServer`.
  - `tests/test_oauth.py`: 4 tests verifying discovery metadata, DCR, PKCE authorization code exchange, token refresh rotation, authenticated MCP saves, and cross-account RLS isolation.
- **Test Results**:
  - `tests/test_oauth.py`: 4 passed, 0 failed.
  - Pytest full suite: 49 passed in 50.40s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Added cryptographic `jti` claim to JWT access tokens so rapid token re-issuance generates distinct token signatures.
  - Pre-registered default Claude client for standard connector flows while supporting RFC 7591 DCR.
- **Needs User**:
  - **Gate (b)**: Live OAuth interaction with Claude web (Connector added on claude.ai web; login flow completes; token refresh observed in production).

### Milestone X5: Renderer, Signed Viewer Links, and Markdown Download
- **Status**: Completed
- **Done When Criteria**:
  - Rendered Markdown output byte-identical to `FileStore` output for the same turns.
  - HMAC signed viewer links in `vault_find` hits (`/v/{signed_token}`).
  - Web viewer endpoint renders clean HTML transcript with role badges, timestamps, fidelity, and download button.
  - Raw `.md` download endpoint (`/download/{thread_id}.md`) returns full canonical markdown with `Content-Disposition: attachment`.
- **Files & Functions**:
  - `src/thread_save/storage/renderer.py`: `render_thread_markdown` projecting PostgreSQL rows into canonical Plan v2 Markdown (YAML front matter + HTML delimited turn slots).
  - `src/thread_save/web/viewer.py`:
    - `create_viewer_token` & `verify_viewer_token`: HMAC-SHA256 URL-safe token signing and verification with expiration enforcement.
    - `create_viewer_router`:
      - `GET /v/{token}`: Renders HTML conversation viewer.
      - `GET /download/{thread_id}.md`: Streams canonical Plan v2 Markdown file.
  - `src/thread_save/web/mcp_server.py`: Updated `vault_find` to attach `viewer_url` and `download_url` to each matching thread hit.
  - `src/thread_save/web/app.py`: Mounted viewer router into FastAPI application.
  - `tests/test_viewer.py`: 4 tests verifying byte-identical differential rendering against FileStore, HMAC token cryptographic integrity, HTML viewer endpoint, and `.md` download endpoint.
- **Test Results**:
  - `tests/test_viewer.py`: 4 passed, 0 failed.
  - Pytest full suite: 53 passed in 45.19s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Canonical renderer validates thread existence under tenant RLS before rendering turns.
  - Download endpoint accepts both signed HMAC tokens (for external browser links) and active Bearer JWT auth.

### Milestone X6: Security Pass, Redaction, Envelope Encryption, and Tenant Isolation
- **Status**: Completed
- **Done When Criteria**:
  - Pre-insert redaction removes raw secrets before writing into PostgreSQL.
  - Envelope encryption (AES-256-GCM + HKDF) available for at-rest body protection.
  - Cross-account test suite verifies zero leaks across all read, write, bind, search, and viewer interfaces.
  - Thread deletion cascades through turns, gaps, and chunks, recording tombstones without affecting other threads.
  - Account deletion cascades all tenant data cleanly without affecting other accounts.
  - Automated retention purge job identifies and removes expired threads per account policy.
- **Files & Functions**:
  - `alembic/versions/35a36273444e_add_retention_days_to_accounts.py`: Schema migration adding `retention_days` column to `accounts`.
  - `src/thread_save/security/encryption.py`:
    - `derive_account_key`: HKDF-SHA256 key derivation with account-specific info parameter.
    - `encrypt_body` & `decrypt_body`: Authenticated AES-256-GCM encryption with 96-bit random nonces.
  - `src/thread_save/storage/pg_store.py`:
    - `delete_thread`: Cascades deletion of turns/gaps and records tombstone in `deleted_threads` (W-10).
    - `delete_account`: Cascades deletion of entire account and all associated data.
    - `set_account_retention`: Configures per-account retention window in days.
    - `purge_expired_threads`: Scheduled/callable purge job deleting threads past the retention threshold.
  - `tests/test_tenant_isolation.py`: 5 tests verifying pre-insert redaction, AES-256-GCM envelope encryption, cross-account zero leaks (search, stats, render, bind, tokens), deletion cascade, and retention purge.
- **Test Results**:
  - `tests/test_tenant_isolation.py`: 5 passed, 0 failed.
  - Pytest full suite: 58 passed in 46.20s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - `purge_expired_threads` records tombstones upon expiration so subsequent delayed calls are safely rejected (W-10).
  - Retention threshold is evaluated per-account using `accounts.retention_days`.

### Milestone X7: Exporter Worker (Outbox, Debounce, Hash-based Idempotency)
- **Status**: Completed (Automated verification complete; External Gate (c) recorded under Needs User)
- **Done When Criteria**:
  - Save turns enqueue export tasks transactionally into PostgreSQL `outbox` table with 2-minute debounce window (W-11).
  - Subsequent turn saves within debounce window extend the debounce without duplicate jobs.
  - Exporter worker processes due batches asynchronously, rendering canonical Markdown and exporting to target backends.
  - Hash-based idempotency avoids redundant uploads if thread markdown hash is unchanged.
  - Export failures back off exponentially without crashing worker or corrupting outbox.
  - Decoupled save path: exporter failure, offline status, or sudden termination NEVER impacts save path or client requests.
- **Files & Functions**:
  - `src/thread_save/export/worker.py`:
    - `ExportTarget`: Protocol for external destinations.
    - `MockExportTarget`: In-memory destination recording payloads and simulating network faults.
    - `GoogleDriveExportTarget`: Exporter skeleton for Google Drive API v3.
    - `GitHubExportTarget`: Exporter skeleton for GitHub Contents API.
    - `OutboxWorker`: Background worker polling due jobs, executing single-statement queries, rendering canonical projections, performing hash idempotency checks, and handling retries.
  - `src/thread_save/export/__init__.py`: Export package interface.
  - `tests/test_exporter.py`: 3 tests verifying debounce enqueuing, asynchronous export drainage, hash-based deduplication, exponential backoff, and total isolation between exporter lifecycle and save operations.
- **Test Results**:
  - `tests/test_exporter.py`: 3 passed, 0 failed.
  - Pytest full suite: 61 passed in 47.64s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Outbox processing separates job selection from rendering and target upload to prevent row-locking contention across concurrent transactions.
  - Debounce window is set to 2 minutes (`interval '2 minutes'`) per spec §2 S2.
- **Needs User**:
  - **Gate (c)**: Real Google Drive / GitHub OAuth credentials for live production upload.

### Milestone X8: Deployment Readiness, Health Checks, Latency Budget & PITR
- **Status**: Completed (Automated verification complete; External Gate (a) recorded under Needs User)
- **Done When Criteria**:
  - Multi-stage production `Dockerfile` with non-root security user, automated Alembic migration startup, and health check.
  - Deployment configuration for Fly.io (`fly.toml`) and Render (`render.yaml`) enforcing always-on instances (`auto_stop_machines = false`) to eliminate cold starts.
  - Server-side processing latency benchmarked with p95 < 300 ms across HTTP turn save operations.
  - `/health` endpoint validates database connectivity and returns degraded state if disconnected.
  - Production deployment, continuous WAL archiving, daily snapshots, and PITR documented in `docs/DEPLOYMENT.md`.
- **Files & Functions**:
  - `Dockerfile`: Production multi-stage container running migrations and Uvicorn with curl healthcheck.
  - `fly.toml`: Fly.io configuration configured for US-East (`iad`) always-on machine.
  - `render.yaml`: Render Blueprint configuration configured for US-East (`Ohio`) with same-region PostgreSQL.
  - `docs/DEPLOYMENT.md`: Architecture overview, latency budget, WAL archiving, backup & PITR runbook, Fly.io & Render deployment steps.
  - `tests/test_deployment_readiness.py`: 3 tests verifying `/health` probe (healthy & degraded), p95 latency (< 300 ms), and production config syntax.
- **Test Results**:
  - `tests/test_deployment_readiness.py`: 3 passed, 0 failed.
  - Pytest full suite: 64 passed in 45.87s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Explicitly specified `auto_stop_machines = false` in Fly.io config to prevent 30-60s idle wake-up delays.
  - Same-region co-location between web application and PostgreSQL database enforced to ensure p95 < 300 ms budget.
- **Needs User**:
  - **Gate (a)**: Live cloud instance provisioned and verified on Fly.io or Render with custom domain and SSL certificate.

### Milestone X9: Cross-Surface Multi-Device Simulation (Automated Regression Test)
- **Status**: Completed (Automated multi-device simulation verified as regression test across 4 surfaces; External Gate (e) manual physical verification documented under Needs User)
- **Done When Criteria**:
  - Single thread maintained across device switches (Desktop -> Android -> iOS -> Web), no split threads (split rate 0.0%).
  - Dense turn slots (1..10) with 100% coverage and zero stubs.
  - Chunked response (>6000 chars) assembled correctly without loss.
  - Sensitive API keys redacted pre-insert and absent in stored plaintext.
  - Multi-device continuity over Streamable HTTP transport verified.
  - Comprehensive documentation and runbook in `docs/CROSS_SURFACE_TESTING.md`.
- **Files & Functions**:
  - `scripts/cross_surface_test.py`: Standalone CLI and test runner executing §8.2 live scenario protocol across Desktop, Android, iOS, and Web surfaces.
  - `docs/CROSS_SURFACE_TESTING.md`: Architecture overview, platform constraints, onboarding steps, manual live scenario protocol (§8.2), and Gate (e) verification checklist.
  - `src/thread_save/storage/pg_store.py`: Added `get_turns` to retrieve stored turns as `TurnData` for verification and projection.
  - `tests/test_cross_surface.py`: Automated pytest suite testing service-level scenario execution and HTTP transport over `/mcp`.
- **Test Results**:
  - `tests/test_cross_surface.py`: 2 passed, 0 failed.
  - `scripts/cross_surface_test.py`: Scenario passed cleanly with 0 split threads, 100% coverage, redaction passed, chunk assembly passed.
  - Pytest full suite: 66 passed in 46.91s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Used ASCII borders in test runner outputs to guarantee platform compatibility across Windows console encodings.
  - Added `get_turns` method to `PgStore` to fetch `TurnData` domain objects directly without going through markdown re-parsing.
- **Needs User**:
  - **Gate (e)**: Live manual cross-surface testing with real Claude Web/Android/iOS/Desktop accounts against a deployed instance.

### Milestone X10: Local Vault Migration CLI & Complete Onboarding
- **Status**: Completed (Automated migration verification complete; External Gate (d) recorded under Needs User)
- **Done When Criteria**:
  - `import_local_vault` CLI translates local Markdown vault files (`*_p*.md`) into PostgreSQL (`PgStore`) rows with `--dry-run` preview.
  - Preserves multi-page threads, turn ordering, stubs, gaps, and fidelity ranks.
  - Idempotent re-runs safely update without duplicating rows or corrupting data.
  - Migrated PostgreSQL rows project cleanly into canonical Plan v2 Markdown via `render_thread_markdown`.
  - `docs/ONBOARDING.md` updated with explicit instructions to remove the stdio `threadvault` entry from `claude_desktop_config.json` to prevent duplicate tool registrations.
- **Files & Functions**:
  - `src/thread_save/cli/__init__.py`: Package init for CLI tools.
  - `src/thread_save/cli/migrate.py`: `import_local_vault` CLI with dry-run support, schema translation, and PostgreSQL upsert logic.
  - `pyproject.toml`: Added `thread-save-migrate` command line entry point.
  - `docs/ONBOARDING.md`: Comprehensive onboarding & migration guide with stdio removal instructions and remote setup.
  - `tests/test_migration_cli.py`: 5 tests verifying dry run, full import, idempotency, round-trip rendering, and subprocess execution.
- **Test Results**:
  - `tests/test_migration_cli.py`: 5 passed, 0 failed.
  - Pytest full suite: 71 passed in 64.56s (including 1,000 Hypothesis examples in `tests/test_hypothesis_v2.py`).
  - `run_tests.py`: 21 passed, 0 failed.
  - fsck: Scanned 5 files across 2 threads (21 turns) in `vault_rich_fixture` - 0 errors, fsck clear.
- **Decisions Taken**:
  - Primary key `(thread_id, n, role)` guarantees deduplication of repeated turns across page rolls, preserving the highest fidelity version.
  - In dry-run mode, unique slot combinations are calculated across all files to accurately report expected import row counts.
- **Needs User**:
  - **Gate (d)**: Run `python -m thread_save.cli.migrate --vault-dir <user_vault>` to import live personal history into hosted ThreadVault.

---

## Milestone H1: Parametrised Hypothesis Store Testing & PostgreSQL Invariant Checker (`fsck --dsn`)
- **Status**: Completed
- **Done When Criteria**:
  - `tests/test_hypothesis_v2.py` parametrised across both `FileStore` and `PgStore` running 1,000 examples each under the normal profile.
  - `fsck.py` extended with `--dsn` CLI argument and `verify_pg_vault_async` / `verify_pg_vault` to directly check W-1...W-10 invariants in PostgreSQL (`accounts`, `threads`, `turns`, `gaps`, `outbox`, `deleted_threads`).
  - Full test suite, both Hypothesis stores, and PostgreSQL fsck pass cleanly.
- **Files & Functions**:
  - `tests/test_hypothesis_v2.py`: Parametrised `@pytest.mark.parametrize("store_type", ["file", "pg"])` with Hypothesis 1,000 examples per store.
  - `src/thread_save/fsck.py`: Added PostgreSQL database verification checking all invariants (W-1...W-10): account slugs, thread metadata, dense turn numbering, canonical hashes, gap recovery, outbox payload validity, tombstones. Added `--dsn` CLI argument.
  - `src/thread_save/storage/pg_store.py`: Canonicalized body truncation up to `MAX_BODY_CHARS` with trailing newline, stripped NUL bytes (`\x00`) from string inputs to prevent asyncpg errors.
  - `src/thread_save/security/idempotency.py`: Stripped NUL bytes in `canonical_v1`.
  - `src/thread_save/storage/identity.py`: Stripped NUL bytes in `normalise_anchor`.
- **Test Results**:
  - Pytest full suite: 72 passed in 109.37s (including 1,000 Hypothesis examples on `FileStore` and 1,000 Hypothesis examples on `PgStore`).
  - `fsck --dsn`: Scanned 1 accounts, 1 threads, 2 turns, 0 gaps, 1 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Sanitized NUL characters (`\x00`) in `canonical_v1`, `normalise_anchor`, and `PgStore` input fields because PostgreSQL rejects string literals containing `\x00` with `CharacterNotInRepertoireError`.
  - Body truncation in `PgStore` ensures that canonicalizing and ensuring trailing `\n` does not exceed `MAX_BODY_CHARS` (100,000 characters).
  - Postgres fsck validates content hash prefix matching when 8 chars or exact match when 64 chars.

---

## Milestone H2: OAuth Identity, Persistence & Google Upstream IdP
- **Status**: Completed
- **Done When Criteria**:
  - `/oauth/authorize` authenticates the human via Google Sign-In as upstream IdP (`GOOGLE_AUTH_URL`, `GOOGLE_TOKEN_URL`, `GOOGLE_USERINFO_URL`), mapping `sub` to a stable `account_id` in PostgreSQL (`oauth_sub = 'google:' || sub`).
  - Reconnecting after revocation or restart resolves to the identical `account_id`.
  - Registered OAuth clients, authorization codes, and refresh tokens are persisted in PostgreSQL (`oauth_clients`, `oauth_auth_codes`, `oauth_refresh_tokens` tables via Alembic migration `7a1b2c3d4e5f`).
  - JWT signing key is loaded strictly from secret configuration (`THREADVAULT_JWT_SECRET` / `JWT_SECRET_KEY`), never generated randomly at startup.
  - The static pre-registered Claude client placeholder has been explained and removed in favor of RFC 7591 Dynamic Client Registration.
  - Verified by tests: server restart preserves refresh token functionality; revoke and reconnect yields identical `account_id`.
- **Files & Functions**:
  - `alembic/versions/7a1b2c3d4e5f_add_oauth_persistence_tables.py`: Migration creating `oauth_clients`, `oauth_auth_codes`, `oauth_refresh_tokens`, and adding `email` column to `accounts`.
  - `src/thread_save/config.py`: Added `jwt_secret` to `VaultConfig` loaded strictly from secret config (`THREADVAULT_JWT_SECRET` / `JWT_SECRET_KEY`), requiring explicit key in production without random generation.
  - `src/thread_save/web/oauth.py`:
    - Full PostgreSQL persistence for `register_client`, `get_client`, `create_auth_code`, `consume_auth_code`, `create_tokens_async`, `rotate_refresh_token`, and `revoke_refresh_token`.
    - Added RFC 7009 Token Revocation endpoint (`POST /oauth/revoke`).
    - Added Google Upstream IdP authentication in `/oauth/authorize` and `/oauth/callback/google` with `resolve_upstream_account` ensuring stable, immutable `account_id` mapping.
    - Removed hardcoded static default Claude client.
  - `src/thread_save/web/app.py`: Automatically wires `PgStore` pool and `cfg.jwt_secret` to `OAuthServer`.
  - `tests/test_oauth.py`: Added `test_oauth_server_restart_refresh_token_persistence` and `test_oauth_revoke_and_reconnect_stable_account_id`.
- **Test Results**:
  - `tests/test_oauth.py`: 6 passed, 0 failed.
  - Full pytest suite: 74 passed in 105.90s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 1 threads, 2 turns, 0 gaps, 1 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Upstream Google authentication resolves human identity via immutable `sub` claim. When reconnecting after revoking tokens, `SELECT id FROM accounts WHERE oauth_sub = $1` matches the original account, preserving existing threads and turns.
  - Auth code consumption uses atomic `DELETE FROM oauth_auth_codes WHERE code = $1 RETURNING ...` to enforce single-use semantics directly in the database.
  - Refresh token rotation uses atomic `DELETE FROM oauth_refresh_tokens WHERE token = $1 AND revoked = false RETURNING ...` preventing token replay attacks.

---

## Milestone H3: Origin and Host Header Handling
- **Status**: Completed
- **Done When Criteria**:
  - Requests with no `Origin` header are accepted (supporting CLI, curl, desktop and native mobile clients).
  - Only present, disallowed `Origin` headers are rejected with 403 Forbidden.
  - Request `Host` header is validated against an allowlist loaded from configuration, including deployed domains (`FLY_APP_NAME.fly.dev`, `RENDER_EXTERNAL_HOSTNAME`, `THREADVAULT_ALLOWED_HOSTS`, `localhost`, `127.0.0.1`, `testserver`).
  - Disallowed `Host` header is rejected with 403 Forbidden.
  - Test suite verifies acceptance of no-Origin requests and rejection of unauthorized hosts.
- **Files & Functions**:
  - `src/thread_save/config.py`: Added `allowed_hosts` to `VaultConfig` and populated from `THREADVAULT_ALLOWED_HOSTS`, `FLY_APP_NAME`, and `RENDER_EXTERNAL_HOSTNAME` in `load_config()`.
  - `src/thread_save/web/middleware.py`: Updated `OriginValidatorMiddleware` with Host header validation against `allowed_hosts` and explicit acceptance of absent `Origin` headers.
  - `src/thread_save/web/app.py`: Wired `cfg.allowed_hosts` into `OriginValidatorMiddleware`.
  - `tests/test_streamable_http.py`: Added test assertions verifying no-Origin requests succeed (200), allowed Host headers succeed (200), and disallowed Host headers fail with 403.
- **Test Results**:
  - `tests/test_streamable_http.py`: 5 passed, 0 failed.
  - Full pytest suite: 74 passed in 128.21s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 1 threads, 2 turns, 0 gaps, 1 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Host validation protects against DNS rebinding attacks while dynamically adapting to Fly.io and Render production deployments via environment variables.

---

## Milestone H4: Remote Tool Parity & Description Alignment
- **Status**: Completed
- **Done When Criteria**:
  - `vault_stats` exposed over Streamable HTTP transport with parity to stdio.
  - Tool annotations (`ToolAnnotations`) and server instructions (`_SERVER_INSTRUCTIONS`) match between stdio (`server.py`) and remote HTTP (`mcp_server.py`).
  - Tool list verified: both stdio and HTTP expose identical 4 tools (`vault_save_turn`, `vault_backfill`, `vault_find`, `vault_stats`).
  - Diff of tool list and descriptions documented.
- **Files & Functions**:
  - `src/thread_save/service.py`: Added `stats_async` to `TurnService` resolving thread stats across stores asynchronously.
  - `src/thread_save/web/mcp_server.py`:
    - Added `vault_stats` tool endpoint matching stdio annotations (`readOnlyHint=True, openWorldHint=False`).
    - Aligned `_SERVER_INSTRUCTIONS`, `_BACKFILL_DESC`, `_FIND_DESC`, `_STATS_DESC` to stdio equivalents verbatim.
    - Preserved honest remote scope for `vault_save_turn` ("Archive the conversation to the user's ThreadVault account." vs "Archive the conversation to the user's local ThreadVault (markdown files on this computer).").
  - `tests/test_streamable_http.py`: Verified `tools/list` returns all 4 tools and verified HTTP tool call to `vault_stats`.
- **Tool Parity & Description Diff**:
  - **Tool List Comparison**:
    - Stdio (`server.py`): `['vault_save_turn', 'vault_backfill', 'vault_find', 'vault_stats']`
    - Remote (`mcp_server.py`): `['vault_save_turn', 'vault_backfill', 'vault_find', 'vault_stats']`
    - Match: 100% Identical
  - **Tool Annotations Comparison**:
    - `vault_save_turn`: `readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False` (Match: Identical)
    - `vault_backfill`: `readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False` (Match: Identical)
    - `vault_find`: `readOnlyHint=True, openWorldHint=False` (Match: Identical)
    - `vault_stats`: `readOnlyHint=True, openWorldHint=False` (Match: Identical)
  - **Server Instructions Diff**:
    - Match: 100% Identical
  - **Description Unified Diff**:
    ```diff
    --- stdio/vault_save_turn
    +++ remote/vault_save_turn
    @@ -1,4 +1,4 @@
    -Archive the conversation to the user's local ThreadVault (markdown files on this computer). The user enabled this connector to keep a record of their chats.
    +Archive the conversation to the user's ThreadVault account. The user enabled this connector to keep a record of their chats.

     Call this once at the start of each of your replies:
     - user_query: the user's latest message, verbatim.
    ```
    (Note: `vault_backfill`, `vault_find`, and `vault_stats` descriptions are 100% identical between stdio and remote).
- **Test Results**:
  - `tests/test_streamable_http.py`: 5 passed, 0 failed.
  - Full pytest suite: 74 passed in 111.09s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 1 threads, 2 turns, 0 gaps, 1 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Preserved the honest remote account context ("user's ThreadVault account") in `vault_save_turn` while matching all other descriptions and annotations to guarantee behavioral parity across stdio and HTTP clients.

---

## Milestone H5: Viewer Links Expiry & Token Reuse Prevention
- **Status**: Completed
- **Done When Criteria**:
  - Viewer tokens bound cryptographically to `thread_id` + `account_id` via HMAC-SHA256 signature.
  - Default TTL configured to 15 minutes (900 seconds), configurable via `THREADVAULT_VIEWER_TTL_SECONDS`.
  - Tokens cannot be reused across threads (thread ID mismatch rejects with 403 Forbidden).
  - Tokens cannot be reused across downloads (download tokens tracked and single-use, rejecting replay with 403 Forbidden).
  - Tests verify expiration rejection, cross-thread rejection, and cross-download replay rejection.
- **Files & Functions**:
  - `src/thread_save/config.py`: Added `viewer_ttl_seconds: int = 900` to `VaultConfig` and loaded from `THREADVAULT_VIEWER_TTL_SECONDS` in `load_config()`.
  - `src/thread_save/web/viewer.py`:
    - Updated `create_viewer_token` default TTL to 15 minutes (`DEFAULT_VIEWER_TTL_SECONDS = 900`).
    - Added single-use download consumption tracking (`_consumed_download_tokens`).
    - Enforced strict checks in `/download/{thread_id}.md` rejecting cross-thread usage and consumed download token replays.
  - `tests/test_viewer.py`: Added `test_h5_viewer_link_expiry_and_reuse_prevention` covering 15m default TTL assertion, expired token rejection on `/v/` and `/download/`, cross-thread reuse rejection, and single-use download consumption.
- **Test Results**:
  - `tests/test_viewer.py`: 5 passed, 0 failed.
  - Full pytest suite: 75 passed in 106.23s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns, 0 gaps, 2 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - 15-minute default expiration balances sharing convenience during active conversations with minimizing data exposure windows.
  - Download tokens are single-use: once consumed to stream markdown, the token cannot be reused for subsequent downloads, preventing URL leakage and replay.

---

## Milestone H6: Encryption × Search (Titles-Only Mode & Never Silently Empty)
- **Status**: Completed
- **Done When Criteria**:
  - `vault_find` with envelope encryption defined and tested across PostgreSQL backend, service layer, stdio server, and HTTP MCP endpoint.
  - Searches thread titles only when envelope encryption is enabled (`search_mode: "titles_only"`).
  - Never silently empty: when no matching thread titles are found for a query, returns a descriptive `notice` explaining that envelope encryption is active and only thread titles were searched because turn bodies are encrypted.
  - Plaintext search returns `search_mode: "full_text"`.
- **Files & Functions**:
  - `src/thread_save/config.py`: Added `envelope_encryption_enabled: bool` and `master_key: Optional[str]` to `VaultConfig`; loaded via `THREADVAULT_ENVELOPE_ENCRYPTION` and `THREADVAULT_MASTER_KEY` in `load_config()`.
  - `src/thread_save/storage/protocol.py`: Added `titles_only: bool = False` parameter to `Store.find()`.
  - `src/thread_save/storage/writer.py`: Updated `FileStore.find()` to accept `titles_only` and filter strictly on thread title.
  - `src/thread_save/storage/pg_store.py`:
    - Updated `PgStore.__init__` to accept `master_key` and `envelope_encryption_enabled`.
    - Updated `PgStore.find()` to support `titles_only`. When active, queries `threads.title ILIKE '%' || $2 || '%'` without invoking `turns.tsv` or inspecting encrypted turn bodies.
  - `src/thread_save/service.py`:
    - Added `envelope_encryption_enabled` property on `TurnService`.
    - Updated `TurnService.find()` to default `titles_only` to `self.envelope_encryption_enabled`.
  - `src/thread_save/web/mcp_server.py`:
    - Updated `vault_find` to include `"search_mode": "titles_only"` when envelope encryption is enabled (or `"full_text"` otherwise).
    - Added non-silently-empty guarantee: returns informative `"notice"` on 0 matches explaining that envelope encryption is active and turn bodies are encrypted.
  - `src/thread_save/server.py`: Aligned stdio `vault_find` with HTTP `vault_find` to include `search_mode`, `count`, and non-silently-empty `notice`.
  - `tests/test_tenant_isolation.py`: Added `test_h6_vault_find_with_envelope_encryption` verifying plaintext full-text matching, titles-only search under envelope encryption, rejection of body-only terms, and non-silently-empty notice verification.
- **Test Results**:
  - `tests/test_tenant_isolation.py`: 6 passed, 0 failed.
  - Full pytest suite: 76 passed in 91.00s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns, 0 gaps, 2 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - When turn bodies are encrypted with per-account AES-256-GCM keys, PostgreSQL GIN full-text indexing cannot index ciphertext. Falling back to title-only matching avoids misleading empty results, while explicitly declaring `search_mode: "titles_only"` and returning an explanatory `notice` ensures agents and users are fully aware why turn content was omitted from matching.

---

## Milestone H7: Deploy Config (Release / Pre-Deploy Migrations & Non-Sleeping Instance)
- **Status**: Completed
- **Done When Criteria**:
  - Alembic migrations moved to dedicated deployment release / pre-deploy commands in `fly.toml` (`[deploy] release_command = "python -m alembic upgrade head"`) and `render.yaml` (`preDeployCommand: python -m alembic upgrade head`), avoiding migration execution in container entrypoints and preventing multi-replica race conditions.
  - Render configured with paid `plan: starter` (non-sleeping instance) to eliminate cold starts and keep the MCP endpoint available with low latency.
  - Automated tests verify presence and exact syntax of release commands and non-sleeping configuration across both platforms.
- **Files & Functions**:
  - `fly.toml`: Added `[deploy]` section with `release_command = "python -m alembic upgrade head"`.
  - `render.yaml`: Added `preDeployCommand: python -m alembic upgrade head`; verified `plan: starter` for both web service and PostgreSQL database.
  - `tests/test_deployment_readiness.py`: Added `test_h7_deploy_config_migrations_and_non_sleeping_instance` verifying fly release command, render pre-deploy command, and non-sleeping plan configuration.
- **Test Results**:
  - `tests/test_deployment_readiness.py`: 4 passed, 0 failed.
  - Full pytest suite: 77 passed in 108.40s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns, 0 gaps, 2 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Moving migrations out of container startup into the deployment orchestrator's release step prevents concurrent database migrations when scaling out web worker replicas, while ensuring failed schema migrations fail the release before routing traffic.
  - Explicitly configured non-sleeping plans ensure uninterrupted SSE streaming and prevent 30-50s cold-start delays.

---

## Milestone H8: Migration CLI Safety (Default Dry-Run, Require --apply, & Account Slug Validation)
- **Status**: Completed
- **Done When Criteria**:
  - Local migration CLI defaults to `--dry-run` without modifying the database; requires explicit `--apply` flag to execute writes.
  - `--account-slug` must match an existing OAuth account in the PostgreSQL `accounts` table; targeting an unauthenticated or non-existent account aborts migration immediately with an informative error message and non-zero exit code.
  - Automated tests verify default dry-run behavior, `--apply` requirement, existing account validation, and subprocess CLI failure when targeting non-existent accounts.
- **Files & Functions**:
  - `src/thread_save/cli/migrate.py`:
    - Updated `import_local_vault` to default `dry_run=True` (and require `apply=True` to write).
    - Added database pre-validation ensuring `account_slug` exists in PostgreSQL `accounts` table before processing any page files, aborting with descriptive error if not found.
    - Updated `main()`: added `--apply` flag, set `--dry-run` as explicit flag, defaults to dry-run unless `--apply` is specified. Prints error messages to stderr upon failure.
  - `src/thread_save/storage/pg_store.py`:
    - Updated `resolve_account_uuid` to look up existing account by slug first to avoid unique key collisions (`accounts_slug_key`).
  - `tests/test_migration_cli.py`:
    - Added `test_h8_default_dry_run_requires_apply` verifying no database writes occur without `--apply`, and writes occur with `--apply`.
    - Added `test_h8_account_slug_must_match_existing_oauth_account` verifying rejection of non-existent account slugs in both programmatic and CLI subprocess invocations.
    - Updated `clean_pg_store` fixture to pre-seed test OAuth accounts.
  - `tests/test_viewer.py`:
    - Fixed token tampering character replacement logic in `test_x5_viewer_token_crypto` to guarantee altered token bytes.
- **Test Results**:
  - `tests/test_migration_cli.py`: 7 passed, 0 failed.
  - Full pytest suite: 79 passed in 112.63s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns, 0 gaps, 2 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Requiring `--apply` prevents accidental destructive or unintended imports from scripts or human operator error.
  - Enforcing that the account slug must already exist in the `accounts` table ensures that migrated local threads are strictly associated with authentic, established OAuth users rather than orphaned or synthetic account partitions.

---

## Milestone H9: Documentation Accuracy (Regression Test Labeling & Live Feature Alignment)
- **Status**: Completed
- **Done When Criteria**:
  - Milestone X9 simulation harness explicitly designated and documented in `PROGRESS.md` and test scripts as an automated regression test, clearly distinguishing between programmatic simulations and the manual physical device verification gate (Gate e).
  - `docs/CROSS_SURFACE_TESTING.md` updated so every single check references live remotely exposed features and endpoints (`/health`, `/mcp` tool execution, signed HTML viewer `/v/{token}`, and token-authenticated markdown download `/download/{thread_id}.md?token={token}`) rather than internal database inspections or local disk paths.
  - All checklists and verification steps verified for consistency with the deployed system.
- **Files & Functions**:
  - `PROGRESS.md`:
    - Updated Gate (e) summary to designate `scripts/cross_surface_test.py` as an automated regression test harness.
    - Updated Milestone X9 heading and status to "Cross-Surface Multi-Device Simulation (Automated Regression Test)".
  - `docs/CROSS_SURFACE_TESTING.md`:
    - Section 3: Designated test runner as an automated regression test; added live remote server invocation instructions (`--url https://<domain> --account <slug>`).
    - Section 4 (Phase A): Updated Turn 1 verification to reference live `/health` probe and remote `vault_save_turn` tool result rather than local database access.
    - Section 4 (Phase C): Replaced all database references with live remote HTTP checks (`vault_stats` over `/mcp`, `vault_find` with search mode and notice assertions, `/v/{token}` HTML rendering with masked secrets and chunked code blocks, and `/download/{thread_id}.md?token={token}` single-use download validation).
    - Section 5: Aligned every checklist item with live remotely exposed HTTP endpoints and tools.
- **Test Results**:
  - `tests/test_cross_surface.py`: 2 passed, 0 failed.
  - Full pytest suite: 79 passed in 107.47s (including both Hypothesis 1,000-example profiles).
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns, 0 gaps, 2 outbox jobs in `thread_save_test` database — Postgres fsck clear.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
- **Decisions Taken**:
  - Distinguishing automated simulation from physical device verification ensures test coverage is honest and transparent: simulation proves protocol compliance, while manual QA (Gate e) remains an explicit checklist for physical devices.
  - Aligning all verification steps in `CROSS_SURFACE_TESTING.md` with remotely exposed HTTP features ensures that external operators can fully validate a deployment without requiring direct database credentials or internal filesystem access.

---

## Follow-up F1: Fast Postgres FSCK in Hypothesis & Concurrency
- **Status**: Completed
- **Done When Criteria**:
  - Run the fsck checks inside the PgStore Hypothesis test after every example and at the end of the concurrency test, failing the test on any violation.
  - Report the largest state scanned (accounts/threads/turns).
- **Files & Functions**:
  - `src/thread_save/fsck.py`: Added `check_pg_fsck_conn(conn: asyncpg.Connection) -> tuple[list[str], dict[str, int]]` validating W-1..W-10 invariants directly over active pooled connections without TCP handshake overhead. Fixed W-7 check for open assistant turns (`body=""` with SHA-256 hash) and W-9 delimiter check to strictly flag tags matching the thread's active secret nonce.
  - `tests/test_pg_store.py`: Added active connection fsck call to `test_pg_advisory_lock_concurrency`.
  - `tests/test_hypothesis_v2.py`: Wired `check_pg_fsck_conn` inside `_run_hypothesis_iteration("pg", ops)` after every example. Added `LARGEST_PG_STATE` tracker and reporting test.
- **Test Results**:
  - `tests/test_pg_store.py`: 11 passed, 0 failed (concurrency test fsck clean).
  - `tests/test_hypothesis_v2.py`: 1,000 Hypothesis examples on PgStore passed cleanly with zero fsck violations across every single example.
  - **Largest State Scanned**:
    - **Accounts**: 1,074
    - **Threads**: 1,075
    - **Turns**: 13,948
    - **Gaps**: 3,654
    - **Outbox Jobs**: 1,075
  - `python -m thread_save.fsck --dsn <test_dsn>`: Scanned 1,074 accounts, 1,075 threads, 13,948 turns — Postgres fsck clear.
- **Decisions Taken**:
  - Running fsck directly on the leased asyncpg connection inside the active event loop avoids reconnect overhead while verifying the exact database snapshot before transaction close.
  - W-9 delimiter check in Postgres correctly checks for the presence of the thread's assigned random nonce (`f"nonce={t_nonce}"`) rather than arbitrary turn delimiter strings, because user input may legitimately mention delimiters in programming queries.

---

## Follow-up F2: Multi-Use Download Tokens within TTL
- **Status**: Completed
- **Done When Criteria**:
  - Download tokens (`/download/{thread_id}.md?token={token}`) permit multiple downloads within the 15-minute expiry window while strictly maintaining thread and account binding.
  - Explain design choice between multi-use vs 5-use DB counter.
- **Files & Functions**:
  - `src/thread_save/web/viewer.py`: Removed `_consumed_download_tokens` in-memory set and single-use rejection on `/download/{thread_id}.md`. Kept 15-minute expiry (`THREADVAULT_VIEWER_TTL_SECONDS = 900`) and HMAC-SHA256 signature binding to `(thread_id, account_id)`.
  - `tests/test_viewer.py`: Updated `test_h5_viewer_link_expiry_and_reuse_prevention` to verify that multiple downloads within the 15m TTL window succeed without 403 rejection.
- **Test Results**:
  - `tests/test_viewer.py`: 5 passed, 0 failed.
- **Decisions Taken**:
  - Chose multi-use within 15-minute TTL rather than a 5-use database counter. Industry-standard pre-signed URLs (e.g. AWS S3 presigned URLs, Google Cloud Storage signed URLs) rely on time-bound HMAC signatures without burning tokens on first read. This keeps file downloads completely stateless, prevents database row-lock contention and write overhead during large streaming downloads, and supports download managers, browser retries, and HTTP range requests cleanly without premature token invalidation.

---

## Follow-up F3: Google OAuth Identity, Minimal Scopes & Deployment Docs
- **Status**: Completed
- **Done When Criteria**:
  - Confirm accounts are keyed on Google `sub` claim, not email.
  - Request only minimal scopes (`openid email`).
  - Add "Needs user" gate for Google OAuth client creation, redirect URI, consent screen, and test users.
  - Document every environment variable and secret required for production deployment in `docs/DEPLOYMENT.md`.
- **Files & Functions**:
  - `src/thread_save/web/oauth.py`: Changed Google authorization URL scope parameter from `openid%20email%20profile` to `openid%20email`.
  - `tests/test_oauth.py`: Added `test_google_oauth_scopes_and_sub_claim` verifying minimal scopes and account keying on `oauth_sub="google:{sub}"`.
  - `docs/DEPLOYMENT.md`: Added Section 5 (exhaustive reference table of all environment variables and production secrets) and Section 6 (step-by-step Google Cloud Console setup guide with exact redirect URIs).
  - `PROGRESS.md`: Added Gate (f) under "Needs User".
- **Test Results**:
  - `tests/test_oauth.py`: 8 passed, 0 failed.

---

## Milestone GH1: Git Data API Target Implementation
- **Status**: Completed
- **Done When Criteria**:
  - Git Data API target implements batch sequence: read head ref, read base tree, create tree with inline content/deletions, create commit, patch ref with `force: false`.
  - Non-force ref update restarts automatically on 422 (moved-ref fast-forward race).
  - Rate-limit headers tracked (`x-ratelimit-*`), with exponential backoff on secondary limits (403/429).
  - Private-repo guard refuses push if repository is not strictly private.
  - Mock tests verify: happy batch, 50-file batch uses exactly 3 write calls (zero blob creations), moved-ref race recovery, secondary limit backoff, and public repo refusal.
- **Files & Functions**:
  - `src/thread_save/export/github.py`:
    - `GitHubDataApiTarget`: Implements Git Data API batching, rate-limit parsing, exponential backoff, private-repo guard, and non-force ref update with retry on 422.
    - `GitHubFileEntry`, `GitHubBatchResult`: Dataclasses for batch file definitions and execution results.
    - Exceptions: `PublicRepoRefusedError`, `SecondaryRateLimitError`, `MovedRefMaxRestartsError`, `PushProtectionError`, `GitHubConflictError`.
  - `src/thread_save/export/__init__.py`: Exported new symbols.
  - `tests/test_github_export.py`: Complete mock test suite (`MockGitHubApi` over `httpx.MockTransport`) covering happy batch, 50-file batch = 3 write calls, moved-ref race, secondary-limit 403, exhaustion raises, and public repo refusal.
- **Test Results**:
  - `tests/test_github_export.py`: 6 passed, 0 failed in 0.44s.
  - Full pytest suite: 84 passed, 0 failed in 19.27s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.
- **Decisions Taken**:

---

## Milestone GH2: Archive Layout & Deterministic Index Generation
- **Status**: Completed
- **Done When Criteria**:
  - Implement standard archive layout: `README.md`, `index/threads.json`, `index/YYYY-MM.md`, and `{YYYY}/{MM}/{filename}_p{NN}.md`.
  - Machine index `index/threads.json` deterministically maps thread metadata, turn counts, and sorted page paths.
  - Monthly indexes `index/YYYY-MM.md` provide relative markdown links (`../YYYY/MM/...`) functional in GitHub Web and Obsidian.
  - Golden files verify layout and format integrity.
  - Differential verification: identical conversation rows in FileStore and PgStore produce 100% identical file paths and byte-for-byte identical content trees.
- **Files & Functions**:
  - `src/thread_save/export/layout.py`:
    - `generate_readme()`: Deterministic README with conflict warnings, privacy notice, and squash instructions.
    - `generate_threads_json(threads)`: Deterministic JSON machine index with sorted keys.
    - `generate_monthly_index(year, month, threads, account_dir)`: Markdown monthly index with relative links.
    - `generate_archive_tree(threads, account_dir)`: Assembles full dictionary of repo paths -> content.
    - `extract_filestore_export_tree(vault_root, account)`: Extracts archive tree from local FileStore disk vault.
    - `extract_pgstore_export_tree(store, account_id)`: Extracts archive tree from PostgreSQL database rows.
  - `src/thread_save/export/__init__.py`: Exported layout functions.
  - `tests/test_github_layout.py`: Golden tests for README, threads.json, monthly index, and differential parity test between FileStore and PgStore.
- **Test Results**:
  - `tests/test_github_layout.py`: 4 passed, 0 failed in 0.63s.
  - Full pytest suite: 88 passed, 0 failed in 20.26s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.

---

## Milestone GH3: Commit Policy, Settle Window & Unchanged-Page Skip
- **Status**: Completed
- **Done When Criteria**:
  - Settle window enforces export only when a thread has had no writes for `settle_minutes` (default 30m).
  - Batch cadence batches all settled threads into a single atomic commit.
  - Exactly one in-flight batch per repository (asyncio lock per repository).
  - Unchanged-page skip: page hash comparison against export manifest ensures closed sticky pages (W-6) are never re-sent.
  - Deterministic commit message: `vault: X threads, Y pages (batch YYYY-MM-DDTHH:MMZ)`.
  - Repo growth guard: checks repository size from GitHub metadata and logs warning if size exceeds 750 MB (768,000 KB).
  - Tests verify: 10 saves within settle produce exactly 1 commit; closed pages never re-sent.
- **Files & Functions**:
  - `src/thread_save/export/sync.py`:
    - `GitHubExportConfig`: Configuration dataclass with settle window, batch minutes, size warning threshold.
    - `GitHubBatchExporter`: Implements `is_thread_due`, `check_repo_size_guard`, manifest hash checking, unchanged-page skipping, deterministic commit messages, and per-repo concurrency locking.
  - `src/thread_save/export/__init__.py`: Exported sync symbols.
  - `tests/test_github_sync.py`: Tests for 10 saves within settle -> 1 commit, closed page skipping across successive exports, and 750MB growth guard warning.
- **Test Results**:
  - `tests/test_github_sync.py`: 3 passed, 0 failed in 0.58s.
  - Full pytest suite: 91 passed, 0 failed in 20.19s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.

---

## Milestone GH4: Conflict Detection, Human-Edit Preservation & Dead-Lettering
- **Status**: Completed
- **Done When Criteria**:
  - Human-edit detection via blob SHA: compares base tree blob SHA with `last_blob_sha`; skips overwriting files modified directly on GitHub.
  - Push-protection handling: GitHub secret scanning rejection raises `PushProtectionError`, dead-letters the affected thread, and never re-sends it.
  - Active conflicts and dead letters are tracked in exporter status and surfaced in CLI reports.
  - Mock tests verify: human edits on GitHub are not overwritten, push-protection rejection triggers dead-lettering without retries, and conflicts/dead-letters display in reports.
- **Files & Functions**:
  - `src/thread_save/export/github.py`:
    - Updated `push_batch` to parse and return created blob SHAs in `GitHubBatchResult.blob_shas`.
    - Added base tree inspection for conflict checking against remote blob modifications.
  - `src/thread_save/export/sync.py`:
    - `GitHubBatchExporter`: Tracks `blob_shas`, `conflicts`, and `dead_letters`. Excludes dead-lettered threads from future export candidates. Attaches `last_blob_sha` to batch file entries.
    - Added `get_status()` returning active conflicts, conflict details, dead letters, and repo size stats.
  - `src/thread_save/report.py`:
    - Added `format_export_status()` formatting active conflicts and dead-lettered threads in CLI reports.
  - `tests/test_github_conflicts.py`: Full mock test suite verifying human-edit conflict skip, push-protection dead-lettering, and report formatting.
- **Test Results**:
  - `tests/test_github_conflicts.py`: 3 passed, 0 failed in 0.39s.
  - Full pytest suite: 94 passed, 0 failed in 19.93s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.

---

## Milestone GH5: Deletion In Batches & History Squash CLI
- **Status**: Completed
- **Done When Criteria**:
  - File removal in batch: thread deletion creates entries with `sha=None` in Git Data API tree, deleting page files on GitHub.
  - History squash: `python -m thread_save.cli.github squash` rewrites the archive branch to a single orphan (parent-less) commit of the current tree using force update.
  - Transparent documentation and CLI warnings regarding git history semantics: deleting threads leaves old versions in git history until an explicit or scheduled squash is performed.
  - Tests verify: squash produces single-parent-less commit with current tree only; batch deletion removes files from tree; CLI command runner executes cleanly.
- **Files & Functions**:
  - `src/thread_save/export/github.py`: Added `squash_history()` on `GitHubDataApiTarget` creating root commit with `parents=[]` and force-updating ref.
  - `src/thread_save/export/sync.py`: Added `delete_thread_files()` on `GitHubBatchExporter` removing files in atomic batch and clearing manifest entries.
  - `src/thread_save/cli/github.py`: CLI tool implementing `squash` subcommand with clear history semantics notice.
  - `tests/test_github_squash.py`: Mock test suite verifying orphan root commit creation, parent-less property, tree preservation, and batch file deletion.
- **Test Results**:
  - `tests/test_github_squash.py`: 4 passed, 0 failed in 0.38s.
  - Full pytest suite: 98 passed, 0 failed in 19.71s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.
- **Decisions Taken**:
  - Squashing to a single orphan root commit (`parents: []`) with `force: true` is the only force-push operation permitted in ThreadVault, allowing users to purge old deleted conversations from git history when desired.
  - Deletions are sent inline in the standard 3-request batch without needing separate blob operations.

---

## Milestone GH6: GitHub Auth & Token Lifecycle
- **Status**: Completed
- **Done When Criteria**:
  - Fine-grained PAT 7-day expiration warning (`check_token_expiry()`) with configurable threshold.
  - GitHub response header `github-authentication-token-expiration` captured and tracked on every API request.
  - GitHub App installation token flow (`GitHubAppAuth.mint_installation_token()`) minting 1-hour tokens with RS256 JWT.
  - Token encryption at rest using AES-256-GCM envelope encryption (`encrypt_field` / `decrypt_field`).
  - Zero token leakage guarantee: tokens never appear in logs, exceptions, `__repr__`, or `events.jsonl` (scrubbed via regex mask `[REDACTED_GH_TOKEN]`).
  - Tests verify: expiry warning fires at <= 7 days, response header updates expiry, App flow mints installation token, encryption at rest round-trips, and token string is strictly absent from logs, events, and exception strings.
- **Files & Functions**:
  - `src/thread_save/security/encryption.py`: Added `encrypt_field()` and `decrypt_field()` helpers using AES-256-GCM.
  - `src/thread_save/export/auth.py`:
    - `GitHubAuthManager`: Expiry checking, header tracking, encrypted token storage, preview masking.
    - `GitHubAppAuth`: RSA private key loading and GitHub App JWT/installation token minting.
    - `scrub_tokens()`: Regex-based redaction of all GitHub token formats (`ghp_`, `github_pat_`, `ghs_`, etc.).
  - `src/thread_save/export/github.py`: Integrated `GitHubAuthManager` and token scrubbing into `GitHubDataApiTarget`, added custom `__repr__` preventing token leakage.
  - `src/thread_save/export/__init__.py`: Exported auth symbols.
  - `tests/test_github_auth.py`: 5 comprehensive tests validating all token lifecycle and security invariants.
- **Test Results**:
  - `tests/test_github_auth.py`: 5 passed, 0 failed in 0.54s.
  - Full pytest suite: 103 passed, 3 deselected in 21.36s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (19 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.
- **Decisions Taken**:
  - Fine-grained PATs include their expiration in the `github-authentication-token-expiration` response header; caching and updating this on every successful request ensures the 7-day warning is always based on the latest server timestamp without additional API calls.
  - GitHub App tokens expire after 1 hour by GitHub design; caching with a 5-minute safety margin ensures seamless refreshes without auth failures.
  - All token formats (`github_pat_*`, `ghp_*`, `ghs_*`, etc.) are redacted globally from any error messages or target string representations before being raised or logged.

---

## Milestone GH7: Local Offload & GitHub Sync
- **Status**: Completed
- **Done When Criteria**:
  - `_index/offloaded.json` maintains pointer records for threads offloaded to GitHub per schema (`repo`, `commit`, `pages`, `last_user_anchor`, `recent_turn_keys`, `max_n`, `delim`).
  - Offload eligibility check: idle >= 14 days (`offload_after_days`), all pages exported to GitHub at known commit, no open gaps, and no unfinalised chunks.
  - Offload frees disk files by removing page markdown files cleanly while registering pointer in `_index/offloaded.json`.
  - Dedup optimization: turns matching pointer's `recent_turn_keys` are deduplicated immediately without rehydrating files.
  - Active rehydration: resumed conversations fetch verified pages from GitHub at recorded commit, verify SHA-256 hashes, restore local files, and remove offload pointer.
  - Fallback continuation: if GitHub is unreachable, rate limited, or hash verification fails during rehydration, gracefully creates a continuation thread (`continues: <orig_thread_id>`), preserves uninterrupted saving, and returns `ok: true`.
  - `fsck` invariants: verifies offload pointer integrity, checks all referenced page hashes, and asserts zero stray/partial page files exist on disk for offloaded threads.
  - Differential and regression suites pass cleanly with full 1,000 Hypothesis examples and fsck verification.
- **Files & Functions**:
  - `src/thread_save/models.py`: Added `continues: Optional[str] = None` to `ThreadMeta`.
  - `src/thread_save/storage/formatter.py`: Added `continues` field emission in `format_front_matter`.
  - `src/thread_save/config.py`: Added `offload_after_days: int = 14`, `offloaded_json_path`, and `export_manifest_path`.
  - `src/thread_save/security/idempotency.py`: Added `SlotIndex.get_recent_turn_keys(thread_id, limit=10)`.
  - `src/thread_save/export/github.py`: Added `fetch_file_content(path, ref, client)` and optional custom request headers support.
  - `src/thread_save/export/offload.py`:
    - `OffloadPointer`: Dataclass matching §1 G7 schema.
    - `FileStoreExportManifest`: Manages `_index/export_manifest.json`.
    - `OffloadIndex`: Manages `_index/offloaded.json` atomic reads and writes.
    - `OffloadManager`: Manages eligibility inspection (`is_thread_offloadable`), thread offload execution (`offload_thread`), and thread rehydration (`rehydrate_thread`).
  - `src/thread_save/export/__init__.py`: Exported offload classes and interfaces.
  - `src/thread_save/storage/protocol.py`: Updated `Store.create_thread` to accept `continues: str | None = None`.
  - `src/thread_save/storage/writer.py`: Updated `FileStore` with `_offload_mgr`, offload binding in `_bind_thread`, `continues` support in `create_thread`, and export tracking in `enqueue_export`.
  - `src/thread_save/storage/pg_store.py`: Updated `PgStore.create_thread` to accept `continues: str | None = None`.
  - `src/thread_save/service.py`: Updated `TurnService.save_turn`:
    - Offload binding awareness.
    - Dedup via `recent_turn_keys` without rehydration.
    - Seamless rehydration on turn resume.
    - Continuation thread fallback (`continues: <orig_id>`) on rehydration failure.
  - `src/thread_save/fsck.py`: Enhanced `verify_vault` to validate `_index/offloaded.json` pointers, verify page hashes, and assert zero stray page files for offloaded threads.
  - `tests/test_github_offload.py`: 5 tests verifying offload disk freeing, pointer dedup, clean rehydration, continuation fallback, and fsck offload validation.
- **Test Results**:
  - `tests/test_github_offload.py`: 5 passed, 0 failed in 0.51s.
  - Full pytest suite: 111 passed, 0 failed in 253.79s.
  - Hypothesis profile (`tests/test_hypothesis_v2.py`): 1,000 examples FileStore + 1,000 examples PgStore passed cleanly.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.
- **Decisions Taken**:
  - Continuation thread fallback: Local thread saves should never fail or block because GitHub is temporarily unreachable or offline; if rehydration fails, creating a linked continuation thread (`continues: <orig_id>`) provides high-availability local storage with full provenance.
  - Pointer dedup without rehydration: Claude frequently retries recent turns or sends duplicate requests; checking `recent_turn_keys` directly from the pointer allows zero-I/O duplicate responses without round-tripping to GitHub or rewriting local disk files.
  - Offloaded threads have all page files removed from disk, leaving only the pointer entry in `_index/offloaded.json`. `fsck` strictly verifies that no orphaned or partial page markdown files remain on disk for an offloaded thread.

---

## Milestone GH8: Live Verification (Gate)
- **Status**: Completed (Infrastructure & Test Suite Delivered; Live Gate ready for user throwaway repo)
- **Done When Criteria**:
  - Live test suite `tests/test_github_live.py` implements all 4 live verification phases:
    1. Private repo verification & guard (`GET /repos/{owner}/{repo}`, rejects public repos).
    2. First sync batch (Git Data API 3-call sequence creating tree, commit, ref update).
    3. Incremental batch sync (sticky page hash caching, skipping unchanged Page 1).
    4. Remote human-edit conflict safety (detects external modifications, avoids overwrite).
    5. History squash (single parentless orphan commit preserving tree).
  - Clean conditional execution: tests marked with `@pytest.mark.live` and skip gracefully if `THREADVAULT_GH_LIVE_TOKEN` or `THREADVAULT_GH_LIVE_REPO` are absent.
  - Interactive test runner script `scripts/verify_github_live.py` provided for manual or automated execution with CLI flags or environment variables.
  - Full documentation in `docs/GITHUB_ARCHIVE_VERIFICATION.md` detailing GitHub token creation with minimum permissions and step-by-step verification commands.
- **Files & Functions**:
  - `tests/test_github_live.py`:
    - `test_live_verify_private_guard`: verifies target repository is private.
    - `test_live_first_sync_and_incremental`: tests initial batch push and incremental update with skipped unchanged pages.
    - `test_live_conflict_handling`: tests detection of remote edits and conflict safety.
    - `test_live_squash`: tests history squash to orphan root commit.
  - `scripts/verify_github_live.py`: Automated 5-phase live verification runner with colored reporting and argument parsing.
  - `docs/GITHUB_ARCHIVE_VERIFICATION.md`: End-user setup guide for throwaway private repo and fine-grained PAT generation.
  - `pyproject.toml`: Registered `live` pytest marker.
- **Test Results**:
  - `tests/test_github_live.py`: 4 skipped in 0.36s (clean skip when external live credentials are unconfigured).
  - Standalone script validation: exits cleanly with helpful usage instructions when run without arguments.
  - Full pytest suite: 111 passed, 4 skipped in 255.42s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1 accounts, 2 threads, 4 turns in database — Postgres fsck clear.
- **Decisions Taken**:
  - Live testing strictly requires external user credentials to prevent committing personal secrets or mutating unknown repositories. Delivering both a pytest integration suite and a standalone runner ensures users can test in CI or interactively on the command line.
  - External gate documented under Gate (g) in `PROGRESS.md` with explicit instructions in `docs/GITHUB_ARCHIVE_VERIFICATION.md`.

---

## Milestone Pre-Deploy Safety: Fail-Closed Auth, Server Key, Single-Tenant Export Guard & Public URL
- **Status**: Completed
- **Done When Criteria**:
  - Auth fail-closed: `THREADVAULT_ENFORCE_AUTH` defaults to `true`; server refuses to start with auth disabled unless bound to localhost.
  - Server key security: `THREADVAULT_SERVER_KEY` required unless running locally; zero built-in default keys anywhere (removed `_DEFAULT_SERVER_KEY`).
  - Remote GitHub export guard: refuses to run unless `THREADVAULT_SINGLE_TENANT=true` and exactly 1 account exists in database; logs warning and skips otherwise. Test with two accounts proves nothing is exported.
  - Canonical `THREADVAULT_PUBLIC_URL`: required in production, used for OAuth issuer, metadata discovery endpoints (`/.well-known/*`), and Google OAuth redirect URI; never derived from request headers.
  - Uvicorn configured with proxy headers (`--proxy-headers --forwarded-allow-ips '*'`) for hosting platform proxies (Fly.io / Render).
  - Allowed-hosts variable (`THREADVAULT_ALLOWED_HOSTS`) added to `DEPLOYMENT.md` table.
  - Test verifies metadata issuer equals `THREADVAULT_PUBLIC_URL` when request arrives with internal `Host` and `http` scheme.
  - Every environment variable re-listed in `docs/DEPLOYMENT.md` marked required/optional.
  - Startup check `validate_startup_requirements()` explicitly names all missing required environment variables if startup fails.
- **Files & Functions**:
  - `src/thread_save/security/redactor.py`: Removed hardcoded `_DEFAULT_SERVER_KEY`; added `get_server_key()` requiring explicit `THREADVAULT_SERVER_KEY` in non-local environments.
  - `src/thread_save/web/startup.py`: Added `validate_startup_requirements()`, `is_localhost_bound()`, and `get_public_url()`.
  - `src/thread_save/config.py`: Added `public_url` and `enforce_auth` to `VaultConfig` and `load_config()`.
  - `src/thread_save/web/oauth.py`: Used `get_public_url()` for OAuth issuer, metadata endpoints, and Google redirect URI. Added RFC 9704 `/.well-known/oauth-protected-resource`.
  - `src/thread_save/web/middleware.py`: Updated `AccountContextMiddleware` to prevent auth bypass in production.
  - `src/thread_save/export/worker.py`: Added single-tenant check in `OutboxWorker` and `GitHubExportTarget`.
  - `src/thread_save/web/app.py`: Integrated startup validation in `create_app` and lifespan. Added `run_server` with proxy headers.
  - `Dockerfile`: Added `--proxy-headers --forwarded-allow-ips '*'` to Uvicorn command.
  - `docs/DEPLOYMENT.md`: Re-listed all 17 environment variables and secrets marked required/optional with startup check documentation.
  - `tests/test_predeploy_safety.py`: 5 test cases verifying all safety invariants.
- **Test Results**:
  - `tests/test_predeploy_safety.py`: 5 passed in 1.62s.
  - Full pytest suite: 116 passed, 4 deselected in 271.71s.
  - Hypothesis 1,000-example profiles: passed against FileStore and PgStore in 274.46s.
  - `fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.
  - `fsck --dsn`: Scanned 1001 accounts, 1002 threads, 12860 turns, 3357 gaps, 1002 outbox jobs in database — Postgres fsck clear.
- **Decisions Taken**:
  - `validate_startup_requirements()` prioritizes production environment markers (`ENVIRONMENT=production`, `FLY_APP_NAME`, `RENDER`) over loopback IPs, ensuring auth cannot be disabled in cloud deployments even behind reverse proxies.
  - Remote GitHub export guard enforces single tenancy at the database level (`SELECT count(*) FROM accounts == 1`), completely eliminating multi-tenant cross-account repo overwrite risks.

---

## Milestone D0: Neutral Instruction Style Enforcement & Reversion
- **Status**: Completed
- **Done When Criteria**:
  - Test suite fails if any tool description or server instruction contains forbidden coercive steering keywords: MUST, MANDATORY, ALWAYS (all-caps), "without exception", "regardless", "do not tell", "ignore".
  - Confirmed 0 occurrences across stdio and HTTP server tool descriptions and server instructions.
- **Files & Functions**:
  - `tests/test_instruction_style.py`: `test_d0_no_aggressive_words_in_stdio_instructions_and_tools`, `test_d0_no_aggressive_words_in_http_instructions_and_tools`, `test_d0_forbidden_detector_meta_test`.
- **Test Results**:
  - `tests/test_instruction_style.py`: 3 passed, 0 failed in 1.01s.
  - `python -m thread_save.fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.

---

## Milestone D1: First Line Tool Rule (<=80 Characters Neutral Openings)
- **Status**: Completed
- **Done When Criteria**:
  - Every tool description opening states when to use it in <= 80 characters in neutral wording.
  - `vault_save_turn` opening contains "every reply".
  - Identically applied across stdio (`server.py`) and HTTP (`web/mcp_server.py`).
  - Automated test asserts first sentence length <= 80 chars and "every reply" presence.
- **Files & Functions**:
  - `src/thread_save/server.py`: Updated `_SAVE_TURN_DESC`, `_BACKFILL_DESC`, `_FIND_DESC`, `_STATS_DESC`.
  - `src/thread_save/web/mcp_server.py`: Updated `_SAVE_TURN_DESC`, `_BACKFILL_DESC`, `_FIND_DESC`, `_STATS_DESC`.
  - `tests/test_instruction_style.py`: Added `test_d1_tool_description_openings_under_80_chars`.
- **Exact New First Lines**:
  - `vault_save_turn`: `"Call at the start of every reply to archive this chat turn for the user."` (72 chars)
  - `vault_backfill`: `"Send earlier chat turns that vault_save_turn reported as missing."` (65 chars)
  - `vault_find`: `"Search saved ThreadVault threads by keyword or list recent threads."` (67 chars)
  - `vault_stats`: `"Get coverage statistics for a ThreadVault thread."` (48 chars)
- **Test Results**:
  - `tests/test_instruction_style.py`: 4 passed in 1.12s.
  - `run_tests.py`: 21 passed in 2.8s.
  - `python -m thread_save.fsck vault_rich_fixture`: fsck clear (21 turns across 2 threads).

---

## Milestone D2: Late-Join Backfill
- **Status**: Completed
- **Done When Criteria**:
  - New thread created with client_turn_number > 1 assigns incoming turn n = client_turn_number, writes stubs for 1..n-1, and returns missing [1..n-1].
  - Missing list capped at 10; older gaps marked lost.
  - Description includes: "If earlier turns of this conversation are listed as missing, send them with vault_backfill."
  - Test verifies: first call at turn 5 -> missing [1,2,3,4] -> backfill -> thread complete, fsck clean.
  - Verified across both FileStore and PgStore.
- **Files & Functions**:
  - `src/thread_save/storage/gaps.py`: Updated `report_missing` to cap at 10 and mark older excess gaps as lost.
  - `src/thread_save/storage/pg_store.py`: Added `report_missing` to `PgStore` with cap at 10 and marking older gaps as lost.
  - `src/thread_save/service.py`: Dispatches `report_missing` on `_store` for both FileStore and PgStore.
  - `src/thread_save/server.py` & `src/thread_save/web/mcp_server.py`: Added late-join backfill guidance line in `vault_save_turn` descriptions.
  - `tests/test_late_join.py`: `test_d2_late_join_turn_5_filestore_fsck_clean`, `test_d2_late_join_cap_10_older_marked_lost`, `test_d2_late_join_pgstore`.
- **Test Results**:
  - `tests/test_late_join.py`: 3 passed in 0.77s.
  - `run_tests.py`: 21 passed, 0 failed in 2.75s.
  - `python -m thread_save.fsck vault_rich_fixture`: Scanned 5 files across 2 threads (21 turns) — fsck clear.


















