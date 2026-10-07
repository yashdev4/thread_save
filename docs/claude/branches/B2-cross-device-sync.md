# B2: Cross-device sync (remote server)

- **Source:** `cross_device_sync_plan.md`, then `cross_device_sync_plan_2.md`. The only change in v2 is a **revised §4.1**: Store primitives without a transaction boundary were replaced by `Store` + `ThreadTxn`, because primitives called one at a time cannot satisfy W-11 on Postgres.
- **Parent:** B0 §9. "Any device" has exactly one MCP answer: a publicly hosted remote server.
- **Children:** B3 (correctness of X2/X5), B4 (the GitHub half of X7 and S2).
- **Today:** fully implemented and tested **locally** (PgStore + OAuth + RLS + viewer). **Not what is deployed**: production runs B5. Gates (a), (b), (e), (f) are open.

## Branch map (S1–S8)
| ID | Pick | Status | Evidence / notes |
|---|---|---|---|
| S1 | Postgres rows are canonical; Markdown is a rendered projection | ✅ local · 🔀 prod | `storage/pg_store.py`, `storage/renderer.py`; FileStore↔PgStore byte-identical differential tests. **Production uses FileStore on disk** (B5), the option this plan rejected ("ephemeral disks… pins you to one instance") |
| S2 | Signed short-lived viewer link + raw `.md` download + optional exporter | ✅ PgStore only | `web/viewer.py`; mounted only when the store is PgStore (`web/app.py:158`). In FileStore mode `vault_find` still returns `/v/…` links that 404 (`03-issues.md` P1-4) |
| S3 | Remote only; stdio kept as a dev harness **with different tool names (`devvault_*`)** | ◐ | Stdio still uses `vault_*`, so a Desktop user running both gets duplicate tools. Only mitigation is the ONBOARDING instruction to remove the stdio entry |
| S4 | OAuth 2.1 + DCR + PKCE + refresh; account = OAuth subject; RLS second fence; authless **rejected** | ✅ local · ⚠ prod | `web/oauth.py` (H2 Google upstream IdP, F3 keyed on Google `sub`), RLS in `alembic/versions/ddd36e757df0_init_schema.py:131-139`. Production runs **authless** (B5, `03-issues.md` P0-1) |
| S4 | Allowlist the `claude.ai` / `claude.com` `/api/mcp/auth_callback` redirect URIs | ? | DCR stores whatever `redirect_uris` the client registers (`web/oauth.py:143-178`). No explicit allowlist seen |
| S5 | Never depend on the device; `model_hint` is telemetry only | ✅ | Nothing branches on surface |
| S6 | PK `(thread_id,n,role)` + `pg_advisory_xact_lock(hashtext(thread_id))` + `ON CONFLICT` upsert | ✅ | `pg_store.py` (ledger X2); concurrency test in `tests/test_pg_store.py` |
| S7 | Always-on instance, same-region DB, p95 < 300 ms | ◐ | `fly.toml` `auto_stop_machines=false`; Render `starter` plan. p95 measured in-process only (`tests/test_deployment_readiness.py`); no cloud measurement (gate a) |
| S8 | Redaction before insert | ✅ | `security/redactor.py` with HMAC masks (B3 WD) |
| S8 | Encryption at rest (DB level) + optional per-account envelope encryption | ⚠ | `security/encryption.py` exists, but **`encrypt_body` is never called on turn bodies**. Only `encrypt_field` is used, for GitHub tokens (`export/auth.py:115`). With encryption "enabled", the only change is that `vault_find` switches to titles-only search (H6). Bodies are stored in plaintext. `03-issues.md` P1-8 |
| S8 | Tenant isolation tests | ✅ ⚠ | `tests/test_tenant_isolation.py`. **However**, `PgStore.resolve_account_uuid` looks accounts up by a 20-char sanitised slug, so distinct account strings can merge (B3 W-1, `03-issues.md` P1-6) |
| S8 | Thread/account deletion, retention per account | ◐ | `pg_store.delete_thread/delete_account/purge_expired_threads` exist and are tested. **Nothing schedules the purge** (no caller in `web/` or `server.py`) |
| S8 | Max body size + **per-account** rate limits | ◐ ⚠ | 2 MB limit, checked on `Content-Length` only. One limiter at 60 req/min keyed by the `x-account-id` header or client IP (`web/middleware.py:121-140`). Every Claude call arrives from Anthropic's egress IPs (S5), so **all users share one bucket**, and the header is client-chosen. `03-issues.md` P2-8 |
| S8 | Origin validation | ✅ | `web/middleware.py:31-74` (H3: an absent Origin is accepted; Host allowlist). FastMCP DNS-rebinding check disabled (`app.py:85`, `4842ec8`) |
| S8 | Tool description honesty: "your ThreadVault account on <service>" | ◐ | Description body says "ThreadVault account", but the remote **server instructions** still say "local markdown archive" (`web/mcp_server.py:34-37`). Tracked in B1 |
| S8 | Privacy policy | ❌ | Needed only for a connector-directory listing |

## Milestones (X1–X10) and hardening (H, F)
| # | Done-when (plan) | Status | Commit · notes |
|---|---|---|---|
| X1 | `Store` interface; one code path | ✅ | Pre-git; `storage/protocol.py`, `service.py` `TurnService`. Uses the **v2 `ThreadTxn`** design |
| X2 | Same protocol suite passes on PgStore | ✅ | `0f86b4b`; H1 `4ed05ca` parametrised Hypothesis over both stores |
| X3 | Tools callable over Streamable HTTP | ✅ | `8b0dacc`; H3 `133ae2d` Origin/Host; H4 `1e9d90e` `vault_stats` parity |
| X4 | Connector added on claude.ai; login completes; refresh observed | ◐ ⏸ gate b | `a831812` local; H2 `b419b62` persisted OAuth state + Google IdP; F3 `a9ee239` keyed on `sub`. **Live login never done** |
| X5 | Rendered output byte-identical to FileStore | ✅ | `fc65c78`; H5 `3f93ea3` 15-min viewer TTL; F2 `ddffc47` download tokens reusable within TTL |
| X6 | Cross-account suite: zero leaks | ◐ ⚠ | `f7cbfc1`; H6 `f7abe62` titles-only search. Envelope encryption not applied (above); slug merge risk (B3) |
| X7 | Exporter idempotent; killing the worker never affects saves | ◐ | `dd3aa08` `OutboxWorker`. **Never started by the app** (no instantiation in `src/`). `GoogleDriveExportTarget.export_thread` returns `True` without uploading (`export/worker.py:67-86`), so outbox jobs would be marked done with nothing exported. GitHub half → B4 |
| X8 | p95 < 300 ms from the cloud; no cold starts for 24 h | ◐ ⏸ gate a | `5e4c997`; H7 `0523c2f` pre-deploy migrations. Docker uses `--workers 2` (risky with FileStore, P1-5). Actual deploy is B5 |
| X9 | Live run on web/Android/iOS/Desktop | ◐ ⏸ gate e | `52e3af6` simulation only; H9 `97c900d` relabelled it a regression test |
| X10 | Local vault imported; Desktop shows one set of tools | ◐ ⏸ gate d | `105d351` CLI; H8 `93acac1` dry-run by default, `--apply` required |
| Pre-deploy safety | Auth fail-closed, server key, single-tenant export guard, `PUBLIC_URL` | ✅ → 🔀 B5 | `c8d2b4b`. **Bypassed** 12 h later by `THREADVAULT_AUTH_PAUSED` (`fcbfe80`, `74a2eba`) |

## What production would need to actually run B2
`DATABASE_URL` (Render Postgres, same region) · `THREADVAULT_PUBLIC_URL` · `THREADVAULT_JWT_SECRET` · `THREADVAULT_VIEWER_SECRET` · `THREADVAULT_SERVER_KEY` · `GOOGLE_CLIENT_ID/SECRET` (gate f) · unset `THREADVAULT_AUTH_PAUSED` and `THREADVAULT_STORAGE_BACKEND=file` · add `alembic` + `sqlalchemy` to the deps (P1-1) · run `thread-save-migrate --apply` for any existing file vault · start `OutboxWorker`, the purge job and the orphan reaper from the app lifespan.

## Open items owned by B2
1. Apply envelope encryption to `turns.body` (and decrypt in the renderer and viewer), or remove the claim.
2. Rate-limit key = authenticated account id, never a header or IP.
3. Start the background jobs (outbox, retention purge, orphan reaper, nightly fsck) in the app lifespan.
4. Make the Drive target fail loudly when it is not configured, instead of reporting success.
5. Rename the stdio tools to `devvault_*`, or document why not.
6. Gates a/b/e/f, once B5 is replaced or merged (see B5).

## Change log (newest first)
- 2026-10-07 Â· REPLACED Â· Owner removed the Postgres stack from the code: `pg_store`, `renderer`, `viewer`, outbox `worker`, `export/sync.py`, `cli/migrate` (+ `scripts/verify_github_live.py`, 8 test files, PG tests in 9 more). App always uses FileStore; `vault_find` no longer returns `viewer_url`/`download_url` (they 404'd on FileStore). **Kept** (live dependency): `web/oauth.py` (routes mounted, middleware verifies Bearer tokens with it) and `cli/github.py` (squash; named in the mirror README). OAuth, HTTP and cross-surface tests moved to FileStore. Found: FileStore `find` ignores the account (Bob sees Alice's thread) â†’ xfail test. Alembic, `asyncpg` (fsck/report `--dsn`), Dockerfile untouched. Uncommitted.
- 2026-10-05 · FOUND · Audit: envelope encryption never applied to bodies; outbox/purge/reaper never scheduled; Drive target reports success without uploading; rate limiter shared across users; stdio tool names unchanged (S3).
- 2026-10-05 14:44 → 16:15 · REPLACED (prod) · B5 took over production (`fcbfe80`…`53284c7`). B2 remains the tested target architecture.
- 2026-10-05 02:04 · ADDED · `c8d2b4b` pre-deploy safety.
- 2026-10-05 00:32 · REVISED · F2 `ddffc47` (download tokens multi-use in TTL), F3 `a9ee239` (Google `sub` identity).
- 2026-10-04 22:35 → 23:32 · REVISED · H1–H9 hardening (`4ed05ca`…`97c900d`).
- 2026-10-04 17:35 → 22:04 · ADDED · X2–X10 (`0f86b4b`…`105d351`).
- (pre-git) · REVISED · Plan v2 of this doc replaced §4.1 Store primitives with `Store` + `ThreadTxn`; X1 built on that.
