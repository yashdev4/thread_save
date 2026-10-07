# B5: Standalone deploy (unplanned branch)

- **Source:** no plan document. It appeared in commits between 2026-10-05 10:10 and 17:49, apparently to get a working Claude connector quickly without Postgres or Google OAuth (gates a, b, f).
- **What it is:** FastAPI app (`web/app.py`) on Render with `FileStore` on a 1 GB disk, `THREADVAULT_AUTH_PAUSED=true`, and a background loop (`export/sync_loop.py`) that pushes the whole vault to `github.com/yashdev4/thread_vault` every 60 s.
- **Why it matters:** this is **what users touch today**. It is the least-tested path in the repo: no test references `sync_loop`, `run_sync_once` or `AUTH_PAUSED`, and `fcbfe80` removed a test.

## What B5 overrides
| Overridden decision | Owner | B5 behaviour | Risk |
|---|---|---|---|
| S1 Postgres canonical; server disk rejected | B2 | FileStore on a Render disk | single instance; P0-2 path bug means data may not be on the disk at all |
| S4 OAuth; authless rejected | B2 | `AUTH_PAUSED=true` → `validate_startup_requirements` returns early (`web/startup.py:91-95`); account comes from the `x-account-id` header or `?account=` (`web/middleware.py:177-194`) | **P0-1**: anyone with the URL can read and write every archive |
| Pre-deploy safety: fail-closed, required secrets | B2 | Bypassed via `AUTH_PAUSED` (`74a2eba`: JWT secret and server key fall back to defaults/ephemeral) | Redaction-mask HMAC key is **per-process random** (`redactor.py:117-120`) unless `THREADVAULT_SERVER_KEY` is set. Render `generateValue` does set it |
| H3 Host allowlist; FastMCP DNS-rebinding check | B2 | `THREADVAULT_ALLOWED_HOSTS=*` + rebinding protection disabled (`app.py:85`, `4842ec8`) | acceptable only with auth on |
| G4 settle/batch, G5 conflicts, G6 private guard | B4 | 60 s loop, full tree, `verify_private` off by default | P1-2 commit spam; human edits overwritten; P0-3 public-repo leak |
| X5 viewer | B2 | not mounted for FileStore; `vault_find` still returns `/v/…` links | P1-4 dead links |
| Docker runtime | B2 X8 | switched to the **native Python runtime** (`bed5e2d`) | `alembic` not installed but `preDeployCommand` runs it (P1-1) |
| FileStore state survives restart | B3 W-4 (FileStore) | Registry/slots are memory-only. Every Render deploy or **idle spin-down** wipes them | **P0-4**: every chat that continues after a restart splits into a new file, and the previous reply is dropped. Fixed by B6 L1 |
| Vault root env var | B0 config | `render.yaml` sets `THREADVAULT_ROOT`; code reads only `THREAD_SAVE_VAULT_ROOT` (`config.py:149-152`) | **P0-2**: writes go to `~/thread_vault` on ephemeral disk |

## Commit history of this branch
| Time (2026-10-05) | Commit | Change |
|---|---|---|
| 10:10 | `549e1f6` | render.yaml: all production secrets/env vars (still the B2 design) |
| 10:11 | `34c38aa` | support `RENDER_EXTERNAL_URL` |
| 14:44 | `fcbfe80` | **BRANCHED:** standalone FileStore mode, `AUTH_PAUSED`, background GitHub sync; removed a pre-deploy test |
| 15:07 | `bed5e2d` | Docker → native Python runtime on Render |
| 15:21–15:23 | `dcfc48c`, `6697498` | .gitignore; commit message "." |
| 15:37 | `128f2a5` | buildCommand `pip install .` |
| 15:45 | `85c1fd7` | add `ulid-py`, `pyjwt` deps |
| 16:01 | `74a2eba` | secret defaults allowed when auth paused |
| 16:09 | `4842ec8` | disable FastMCP DNS-rebinding host check |
| 16:15 | `53284c7` | empty-repo initial commit + batch sync |
| 17:44 | `77ec4bd` | restore `preDeployCommand` (alembic) |

## Decision needed (owner)
- **Option A, keep B5 as the product:** fix P0-1/2/3, then wire the B4 policy layer into the loop. Auth via a static bearer token first, then OAuth with file-backed state. Cheapest path to "safe".
- **Option B, retire B5 and deploy B2:** Postgres + Google OAuth + outbox exporter (needs gates a/b/f and `THREADVAULT_SINGLE_TENANT=true` for GitHub). This is what the tests prove.
- Recommendation: **A now, B when a second user arrives.** Record the choice here and in TIMELINE as `REPLACED` or `REVISED`.

## Open items (ordered)
1. P0-2 accept `THREADVAULT_ROOT` as an alias (or fix `render.yaml`); confirm where existing data lives before redeploying.
2. P0-3 private-repo guard on by default. **Confirmed 2026-10-06: the repo is public now**; owner to make it private first.
3. P0-1 auth gate (static token) while OAuth is paused.
4. ~~P1-1 alembic dependency or a conditional pre-deploy.~~ ✅ conditional pre-deploy: `python -m thread_save.cli.predeploy` skips migrations on FileStore, runs alembic with Postgres (alembic still undeclared for a Postgres deploy).
5. P1-2 swap the loop body for `GitHubBatchExporter` (B4 item 1).
6. Tests for all of the above (`tests/test_standalone_mode.py`).
7. P0-4 restart amnesia: B6 L1 (shared FileStore fix); also P1-9 last reply (B6 L3) applies to remote users.

## Change log (newest first)
- 2026-10-07 · FOUND · `/health` took 32 s to answer: a cold start, so the instance sleeps even though `render.yaml` says `plan: starter`. If it is on the free plan, `/data/vault` is not a persistent disk. Owner to check the plan.
- 2026-10-06 · FIXED · P1-1: `preDeployCommand` → `python -m thread_save.cli.predeploy` (same store choice as `create_app`); unpushed `77ec4bd` would otherwise fail every standalone deploy (no alembic, falls back to the 127.0.0.1 test URL).
- 2026-10-06 · REVISED · P1-14 guard verified on the HTTP app (file backend) and its log lines now reach Render's log; still not deployed. Mirror check: `…KTMNR7` removed from `thread_vault` by hand (`37da652`); `…G27WKM` and `…NWKG42` remain. New since: `…6M03BX` (`client: claude-desktop`, a real chat).
- 2026-10-06 · FOUND/REVISED · 3 of the 7 deployed threads are Claude Code sessions (P1-14), now in the public mirror. Claude Code no longer loads the connector (user `deniedMcpServers`); server guard ready, not deployed. Owner to decide whether to delete those 3 threads from Render + GitHub.
- 2026-10-06 · FOUND/REVISED · Deployed-connector chats were being saved by the local stdio server (shared tool names, P1-13); fixed by `vault_local_*` names. Deployed data lives only on Render + the GitHub mirror; `scripts/pull_remote_vault.ps1` gives a local copy in `vault_rich_fixture/`. P0-3 confirmed live.
- 2026-10-06 · REVISED · P0-4 fixed in code (B6 L1), **not deployed**. A Render deploy also changes the tool list to `vault_log_turn`, so users' Personal Preference text must be updated (ONBOARDING Step C).
- 2026-10-05 · FOUND · P0-4 (via B6): FileStore restart amnesia also hits production on every deploy/spin-down.
- 2026-10-05 · FOUND · Audit: P0-1/2/3, P1-1/2/3/4; recorded as branch B5.
- 2026-10-05 14:44 · BRANCHED · `fcbfe80` (see table above).
