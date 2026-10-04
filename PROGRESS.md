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
