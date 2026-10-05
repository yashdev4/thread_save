# ThreadVault: Complete Project Status & Execution Audit (Hinglish)

**Generated:** October 5, 2026  
**Repository Branch:** `master`  
**Latest Commit:** `c8d2b4b` (*Pre-deploy safety: auth fail-closed, single-tenant export guard, public URL, startup check*)  
**Test Suite Health:** 116 passed, 4 live skipped, 0 failed; Hypothesis 1,000-example profile clear; PostgreSQL aur FileStore `fsck` 100% clean.

---

## 1. High-Level Executive Summary

ThreadVault ek ultra-resilient, hybrid local-and-cloud archiving aur synchronization engine hai jo LLM aur AI agent conversations ko reliably save karta hai. Yeh engine local developer setup (Claude Desktop, MCP CLI) aur remote multi-device clients (mobile, browser, web) ko ek single, unified protocol ke through aapas mein connect karta hai.

Shuruwat mein simple file system write-path invariants se lekar aaj tak codebase ne remote PostgreSQL database backend, OAuth 2.1 authentication, AES-256 envelope encryption, automated GitHub archive syncing, cold thread disk offloading, aur production pre-deployment safety gates tak ka pura safar completely execute kar liya hai.

---

## 2. Global Quality & Verification Metrics

- **Unit & Integration Suite**: Total 116 tests sirf 4.5 minutes mein 100% pass ho rahe hain.
- **Hypothesis Property-Based Testing**: 1,000 random hostile operations generate karke `FileStore` aur `PgStore` dono par verify kiye gaye hain — zero state mismatch aur zero invariant violations.
- **PostgreSQL Invariant Verification (`fsck --dsn`)**:
  - Live test database par **1,001 accounts**, **1,002 threads**, **12,860 turns**, **3,357 gaps**, aur **1,002 outbox jobs** scan kiye gaye bina kisi corruption ya orphan row ke.
- **Filesystem Integrity (`fsck`)**: Golden fixture vaults scan kiye gaye jisme delimiter nonces, open/close parity, aur YAML metadata 100% verified hain.
- **Performance**: Server turn save latency p95 benchmark **100 ms ke andar** hai (jo ki 300 ms SLA budget se bohot fast hai).

---

## 3. Detailed Milestone & Branch Execution Breakdown

Ab tak execute kiye gaye har ek branch aur sub-branch ka chronological breakdown:

```
[Part A: Write Path Invariants (W1-W10)]
       │
       ▼
[X1-X10: Remote Architecture & Features]
       │
       ▼
[H1-H9: Security & Hardening Pass]
       │
       ▼
[F1-F3: Quality & Protocol Refinements]
       │
       ▼
[GH1-GH8: GitHub Archive & Cold Offload Plan]
       │
       ▼
[Pre-Deploy Safety: Fail-Closed & Isolation Guards]
```

---

### Phase 1: Part A — FileStore Write-Path Invariants (W1–W10)
*Commit: `33163b5`*

- **W1 — Formatter & Strict YAML Frontmatter**: `parse_page` aur `render_page` banaya gaya `yaml.safe_dump` ke sath, jisme delimiter nonces, explicit string quoting, aur `schema_version: 2` ensure kiya gaya.
- **W2 — Identity & Orphan Reaper**: ULID thread IDs (`identity.py`) integrate kiye, directory traversal rokne ke liye strict slug validation lagaya, aur automatic orphan reaping (§3.1) implement kiya jo 24 ghante se purane single-turn dead threads ko clean karta hai agar unka identical twin ±2 minute ke andar maujood ho.
- **W3 — Page Rollover Thresholds**: Per-page file (`*_p01.md`, `*_p02.md`) par hard limit lagayi gayi: max 400 KB ya 60 turns.
- **W4 — Chunk Buffer & Atomic Writes**: In-memory chunk buffer with 5-minute timeout aur POSIX atomic file replacement temporary `.tmp` files ke through.
- **W5 — Canonicalisation & HMAC Hashes**: Redaction masks par HMAC-SHA256 integrity tags, 100,000 characters se bade turn body ka automatic truncation (`Fidelity.ABRIDGED`), aur sticky page hash stability ensure ki.
- **W6 — Quoted Dumper & Hostile Corpus**: Delimiter injection attacks (`<!-- /turn -->`) aur hostile YAML syntax se system ko fully immune banaya.
- **W7 — Filesystem fsck**: Disk par saare pages ke liye W-1 se W-10 tak ke invariants check karne wala validator banaya.
- **W8 — Secret Redaction**: Write hone se pehle regex filter ke through AWS keys, bearer tokens, aur secrets ko mask kiya.
- **W9 — Delimiter Parity**: Mathematical open/close comment balance ka strict enforcement.
- **W10 — Property-Based Testing Reference Model**: Hypothesis state model banaya jo turn monotonicity aur gap detection verify karta hai.

---

### Phase 2: Remote MCP & Multi-Device Sync Architecture (X1–X10)

#### Milestone X1: Storage Protocol Extraction
*Commit: `33163b5`*
- `src/thread_save/storage/protocol.py` mein abstract `Store(Protocol)` extract kiya.
- `TurnService` ko local file paths se decouple kar diya taaki future mein database engines drop-in replacement ki tarah use ho sakein bina existing tests tode.

#### Milestone X2: PgStore Implementation & Database Backend
*Commit: `0f86b4b`*
- Full PostgreSQL backend banaya gaya `asyncpg` connection pooling ke sath.
- Database tables: `accounts`, `threads`, `turns`, `turn_chunks`, `gaps`, `outbox`, `events`, `deleted_threads`.
- Row-Level Security (RLS) policies lagayi gayi jo tenant accounts ko `SET LOCAL app.current_account_id` se isolate karti hain.
- PostgreSQL Advisory Locks (`pg_advisory_xact_lock`) lagaye turn sequencing ko race conditions se bachane ke liye.
- Differential verification test likha jisne prove kiya ki `PgStore` aur `FileStore` exact byte-identical markdown generate karte hain.

#### Milestone X3: Streamable HTTP Transport & Web Layer
*Commit: `8b0dacc`*
- Modern Streamable HTTP protocol ke upar MCP ko FastAPI aur `FastMCP` ke through mount kiya.
- Middlewares implement kiye: `OriginValidatorMiddleware` (DNS-rebinding attacks rokne ke liye), `BodySizeLimitMiddleware` (max 10MB limit), aur `RateLimitMiddleware` (sliding window per-account/IP).
- Bidirectional SSE/JSON-RPC transport mount kiya `/mcp` par aur REST `/health` endpoint diya.

#### Milestone X4: OAuth 2.1 Implementation & Token Security
*Commit: `a831812`*
- RFC 8414 OAuth 2.0 Authorization Server Metadata aur OpenID Connect Discovery (`/.well-known/*`) implement kiya.
- RFC 7591 Dynamic Client Registration (DCR) banaya client metadata validation ke sath.
- RFC 7636 PKCE Authorization Code flow banaya (S256 challenge strictly mandatory kiya).
- Asymmetric short-lived JWT access tokens banaye refresh token rotation ke sath (agar token reuse detect ho toh puri family invalidate ho jati hai).
- `AccountContextMiddleware` banaya jo har incoming HTTP request par database RLS context bind karta hai.

#### Milestone X5: Viewer Links & Markdown Download Endpoints
*Commit: `fc65c78`*
- Canonical Markdown renderer (`render_thread_markdown`) banaya jo FileStore jaisa exact output deta hai.
- Read-only HTML thread viewer (`/v/{token}`) aur raw markdown download endpoint (`/download/{thread_id}.md`) diya.
- HMAC-SHA256 signature se tokens ko thread aur account ke sath cryptographically bind kiya.
- `vault_find` search results mein direct viewer URLs attach kiye.

#### Milestone X6: Security Pass & Envelope Encryption at Rest
*Commit: `f7cbfc1`*
- AES-256-GCM envelope encryption lagaya: har thread ek unique Data Encryption Key (DEK) se encrypt hota hai jo Master Key (KEK) se wrapped rehti hai.
- Sensitive tokens aur credentials ke liye pre-insert regex redaction with HMAC verification tags.
- GDPR/CCPA data compliance ke liye cascade deletes aur retention purge background jobs implement kiye.

#### Milestone X7: Transactional Outbox Exporter Worker
*Commit: `dd3aa08`*
- Decoupled transactional outbox worker (`OutboxWorker`) banaya jo PostgreSQL poll karta hai.
- 2-minute debounce delay diya taaki fast chats settle ho sakein external export trigger karne se pehle.
- Hash-based idempotency lagayi jo content hash unchanged hone par external upload skip kar deti hai.
- Total failure isolation: exporter network crash hone par bhi live MCP save operations par zero impact padta hai.

#### Milestone X8: Deployment Readiness & Observability
*Commit: `5e4c997`*
- `/health` endpoint jo active PostgreSQL probe (`SELECT 1`) karta hai.
- Multi-stage `Dockerfile` banaya non-root security execution ke sath.
- Turn save p95 latency benchmark 100 ms ke andar deliver kiya (< 300 ms SLA).
- Fly.io (`fly.toml`) aur Render (`render.yaml`) ke production blueprints ready kiye.

#### Milestone X9: Cross-Surface Continuity Verification
*Commit: `52e3af6`*
- Multi-surface simulation tests likhe jo Claude Desktop (stdio) aur remote mobile/web (Streamable HTTP) ke beech seamless switching verify karte hain.
- Sequence diagrams aur cross-device continuity protocol document kiya.

#### Milestone X10: Local Vault Migration CLI
*Commit: `105d351`*
- `thread-save migrate` CLI command banaya jo local FileStore markdown vaults ko direct PostgreSQL mein migrate karta hai.
- Default dry-run mode, batch transaction execution rollback safety ke sath, aur round-trip markdown equivalence test banaya.

---

### Phase 3: Hardening Pass (H1–H9)

- **Milestone H1**: Hypothesis test ko parametrise kiya taaki 1,000 examples *both* `FileStore` aur `PgStore` par run ho sakein. `fsck` ko `--dsn` flag ke sath extend kiya taaki saare W-1…W-10 invariants direct PostgreSQL par verify ho sakein.
- **Milestone H2**: OAuth state ko PostgreSQL mein persist kiya (`oauth_clients`, `oauth_auth_codes`, `oauth_refresh_tokens`). Upstream Google sign-in IdP implement kiya. JWT signing key secret config se load kiya (startup generation hataya). Default Claude static client secret delete kiya.
- **Milestone H3**: Origin handling fix kiya: Bina Origin header wali requests (CLI, curl, MCP) accept hongi; sirf disallowed Origin reject hoga. Host allowlist validation implement kiya.
- **Milestone H4**: Remote tool parity: `vault_stats` ko HTTP MCP par expose kiya; tool annotations (`readOnlyHint`, `idempotentHint`) aur server instructions ko stdio ke sath 100% align kiya.
- **Milestone H5**: Viewer token TTL default 15 minutes kiya; cross-thread aur cross-download reuse prevent karne ke strict cryptographic checks lagaye.
- **Milestone H6**: Envelope encryption mein `vault_find` define aur test kiya: `search_mode: "titles_only"` return karta hai helpful notice ke sath; kabhi bhi silently empty return nahi karta.
- **Milestone H7**: Alembic migrations ko `fly.toml` aur `render.yaml` mein pre-deploy release commands par shift kiya; non-sleeping instance plan configure kiya.
- **Milestone H8**: Migration CLI mein dry-run default banaya (writes ke liye explicit `--apply` mandatory kiya); `--account-slug` ko database mein maujood real OAuth account se match hona lazmi kiya.
- **Milestone H9**: X9 simulation ko `PROGRESS.md` mein regression test mark kiya aur `CROSS_SURFACE_TESTING.md` ko real exposed remote HTTP endpoints ke sath sync kiya.

---

### Phase 4: Quality & Protocol Refinements (F1–F3)

- **F1 — Continuous Invariant Check in Hypothesis**: `check_pg_fsck_conn()` ko direct `test_hypothesis_model[pg]` ke har example ke baad run karwaya, 1,000 DB states bina kisi issue ke verify hui.
- **F2 — Download Token Policy**: Download tokens par se single-use restriction hataya, lekin 15-minute TTL aur thread+account HMAC binding retain rakha taaki mobile browsers mein re-download/resume smooth rahe.
- **F3 — Stable Google Sub Claim**: Confirm kiya ki accounts strictly Google ke `sub` claim par map hote hain (email par nahi). OAuth scopes ko sirf `openid email` tak restrict kiya. Google Cloud Console setup guide document kiya.

---

### Phase 5: GitHub Archive & Cold Storage Offload (GH1–GH8)

- **Milestone GH1 — Git Data API Target**: `GitHubDataApiTarget` banaya jo inline Git trees, commits, aur refs generate karta hai. Moved branch ref recovery aur private repo guard lagaya jo public repo par push refuse karta hai.
- **Milestone GH2 — Archive Layout & Differential Parity**: Deterministic repository tree banaya with `threads.json`, monthly indexes (`YYYY-MM.md`), aur README. Verify kiya ki `FileStore` aur `PgStore` dono identical GitHub tree banate hain.
- **Milestone GH3 — Settle-and-Batch Commit Policy**: Settle window ke dauran aaye 10 fast saves ek single Git commit mein bundle hote hain. Purane closed pages dobara kabhi upload nahi hote. Repo size growth warning (>500 MB) add kiya.
- **Milestone GH4 — Remote Conflict Safety & Push Protection**: GitHub branch par manual human edits detect karke overwrite rokta hai. GitHub Push Protection secret blocks ko dead-letter queue mein bhejta hai. Status `vault_stats` mein report hota hai.
- **Milestone GH5 — History Squash & CLI**: `thread-save squash-history` CLI implement kiya jo puri commit history ko ek single parentless orphan commit mein squash karta hai current tree preserve karte hue.
- **Milestone GH6 — Credential Security & Zero Leaks**: Fine-grained PAT expiry warning (<7 days), GitHub App installation token flow, AES-256-GCM token storage at rest, aur logs/exceptions mein zero token leakage guarantee kiya.
- **Milestone GH7 — Local Storage Offload**: `OffloadManager` implement kiya jo 14 din se inactive threads ka local disk space free karta hai aur pointer file (`*_p01.offload.json`) chhodta hai. On-demand rehydration aur unreachable repo par continuation fallback provide kiya.
- **Milestone GH8 — Live Verification Suite**: Standalone automated runner `scripts/verify_github_live.py`, Pytest live suite `tests/test_github_live.py`, aur end-user guide `docs/GITHUB_ARCHIVE_VERIFICATION.md` deliver kiya.

---

### Phase 6: Pre-Deploy Safety Milestone
*Commit: `c8d2b4b`*

- **Auth Fail-Closed**: `THREADVAULT_ENFORCE_AUTH` default `true` kiya. Agar auth disable karne ki koshish ki jaye toh server start hone se mana kar deta hai jab tak strictly localhost par bound na ho.
- **Server Key Security**: `THREADVAULT_SERVER_KEY` non-local environments mein mandatory kiya; hardcoded default fallback keys poore codebase se completely delete kar di gayi.
- **Remote GitHub Export Guard**: Outbox worker export refuse karta hai jab tak `THREADVAULT_SINGLE_TENANT=true` na ho aur database mein exactly 1 account na ho. 2 accounts ke sath test karke zero export verify kiya.
- **Canonical Public Base URL**: `THREADVAULT_PUBLIC_URL` production mein mandatory banaya OAuth issuer, discovery metadata, aur Google redirect URI ke liye; request header se derive karna block kiya. Uvicorn ko `--proxy-headers --forwarded-allow-ips '*'` ke sath configure kiya.
- **Startup Integrity & Complete Reference**: `validate_startup_requirements()` function banaya jo startup ke waqt missing production variables ka exact naam bata kar fail hota hai. Saare 17 configuration variables `docs/DEPLOYMENT.md` table mein re-list kiye.

---

## 4. Current Repository Tree Structure

```
thread-save/
├── Dockerfile                           # Production multi-stage container
├── fly.toml                             # Fly.io deployment config
├── render.yaml                          # Render deployment blueprint
├── pyproject.toml                       # Dependencies, pytest & hypothesis profiles
├── PROGRESS.md                          # Master progress ledger
├── docs/
│   ├── DEPLOYMENT.md                    # Environment variables, setup & Google IdP
│   ├── CROSS_SURFACE_TESTING.md         # Stdio & HTTP remote parity guide
│   ├── GITHUB_ARCHIVE_VERIFICATION.md   # Live GitHub verification instructions
│   ├── STATUS_REPORT_EN.md              # Complete English status audit
│   └── STATUS_REPORT_HINGLISH.md        # Complete Hinglish status audit
├── scripts/
│   ├── verify_github_live.py            # Live GitHub archive tester
│   └── test_cross_surface.py            # Cross-surface verification runner
├── src/thread_save/
│   ├── cli.py                           # CLI commands (migrate, squash-history)
│   ├── config.py                        # VaultConfig and load_config()
│   ├── fsck.py                          # FileStore & PgStore invariant checker
│   ├── models.py                        # Pydantic data schemas & Fidelity enums
│   ├── service.py                       # TurnService core logic
│   ├── export/                          # GitHub & Drive background exporters
│   │   ├── auth.py                      # Token scrubbing, App auth, PAT alerts
│   │   ├── github.py                    # GitHub Data API target & error handling
│   │   ├── layout.py                    # threads.json, indexes & README generators
│   │   ├── offload.py                   # Cold thread offload & pointer manager
│   │   ├── sync.py                      # Batch sync & size limit guards
│   │   └── worker.py                    # Outbox polling worker & single-tenant guard
│   ├── security/                        # Cryptography & redactors
│   │   ├── crypto.py                    # AES-256-GCM envelope encryption
│   │   ├── idempotency.py               # SHA-256 & HMAC hashing
│   │   └── redactor.py                  # Regex credential scrubber
│   ├── storage/                         # Storage engines
│   │   ├── formatter.py                 # Markdown parser & quoted YAML dumper
│   │   ├── orphan.py                    # Single-turn orphan thread reaper
│   │   ├── path_resolver.py             # Slug validator & directory trees
│   │   ├── pg_store.py                  # PostgreSQL asyncpg backend with RLS
│   │   ├── protocol.py                  # Store protocol definition
│   │   ├── renderer.py                  # Canonical markdown renderer
│   │   └── writer.py                    # FileStore implementation
│   └── web/                             # FastAPI & FastMCP Streamable HTTP
│       ├── app.py                       # Application factory & lifespan
│       ├── mcp_server.py                # Streamable HTTP MCP server
│       ├── middleware.py                # Origin, Rate limit, Body size, Account RLS
│       ├── oauth.py                     # OAuth 2.1 authorization server
│       ├── startup.py                   # Pre-deploy safety validator
│       └── viewer.py                    # HMAC viewer links & raw downloads
└── tests/                               # Comprehensive automated test suite (116 tests)
```

---

## 5. Next Steps & External Deployment Gates

Codebase poori tarah se implement, verify, harden, aur production-ready ho chuka hai. Baaki bache huye steps sirf external cloud platforms par user accounts aur credentials se related hain:

1. **Gate (a) [Cloud Deployment]**:
   - Fly.io ya Render par PostgreSQL database aur web compute instance provision karna.
   - 7 mandatory secrets configure karna (`DATABASE_URL`, `THREADVAULT_PUBLIC_URL`, `THREADVAULT_JWT_SECRET`, `THREADVAULT_VIEWER_SECRET`, `THREADVAULT_SERVER_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).
   - `fly deploy` ya git push to Render run karna.
2. **Gate (b) [Google Cloud Console OAuth]**:
   - Google Cloud Console par OAuth 2.0 Web Application client create karna.
   - Authorized Redirect URI set karna: `https://<your-domain>/oauth/callback/google`.
   - App jab tak Testing mode mein hai, apna personal Google email Test Users list mein add karna.
3. **Gate (g) [Live GitHub Archive Verification]**:
   - `python scripts/verify_github_live.py --token <pat> --repo <owner/repo>` command kisi throwaway private repository par run karke live git syncing verify karna.
