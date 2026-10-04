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

### Milestone X9: Cross-Surface Live Test Script & Multi-Device Verification
- **Status**: Completed (Automated multi-device simulation verified across 4 surfaces; External Gate (e) documented under Needs User)
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


