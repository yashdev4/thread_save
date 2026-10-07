# B4: GitHub archive export

- **Source:** `github_archive_export_plan.md`
- **Parent:** B2 S2 / X7 (the exporter); it also serves B0 stdio users through G7 local offload.
- **Goal:** every thread's Markdown lands in a **private** GitHub repo in the plan-v2 layout, without a local clone; local disk keeps only active threads.
- **Today:** GH1–GH7 exist as a well-tested **library** (`export/github.py`, `sync.py`, `layout.py`, `auth.py`, `offload.py`, `cli/github.py`). The **deployed** server does not use the policy layer: B5's `export/sync_loop.py` calls `GitHubDataApiTarget.push_batch` directly every 60 s and bypasses G4/G5/G6.

## Branch map (G1–G8)
| ID | Decision | Library status | Deployed (B5) status |
|---|---|---|---|
| G1 | Git Data API, no clone: ref → commit → tree with inline content → commit → `PATCH ref force:false`; restart on 422 | ✅ `export/github.py:270-463` | ✅ used |
| G1 | Pace on `x-ratelimit-*`; secondary-limit 403/429 → wait ≥ 60 s, exponential backoff, dead-letter | ✅ `github.py:91-102,131-221` | ◐ the loop logs and retries next tick |
| G1 | Split the batch if the tree body exceeds ~20 MB | ❌ not found | ❌ (the loop sends the whole vault every tick, so this will matter first) |
| G1 | Empty-repo bootstrap | ◐ ⚠ `53284c7` posts a tree without `base_tree`. GitHub's Git Data API returns 409 "Git Repository is empty" for tree creation on an empty repo (inferred; `03-issues.md` P1-3) | ⚠ |
| G2 | Fine-grained PAT, one repo, Contents R/W; warn 7 days before expiry | ✅ `export/auth.py:63-113` | ◐ token read from `GITHUB_TOKEN`; expiry warning not wired |
| G2 | GitHub App installation tokens for multi-user | ✅ `auth.py:135-174` (not wired anywhere) | ❌ |
| G2 | Tokens encrypted at rest, never in logs/events | ✅ `auth.py:32-43,115` scrub/mask; `tests/test_github_auth.py` | ◐ token only in env; loop log lines not audited ? |
| G3 | Per-account private repo; plan-v2 paths; generated README + `index/threads.json` + monthly index; deterministic | ✅ `export/layout.py`; FileStore/PgStore parity (`tests/test_github_layout.py`) | ✅ `extract_filestore_export_tree`; switches to `account_dir=True` when > 1 account dir exists |
| G4 | Settle (30 min) then batch (10 min); one in-flight batch per repo; skip unchanged pages; repo size guard at 750 MB | ✅ `export/sync.py` `GitHubBatchExporter`; D4 env vars `THREADVAULT_GH_SETTLE_MINUTES/BATCH_MINUTES` | ❌ **bypassed.** Fixed 60 s loop, full tree, new commit even when nothing changed (`03-issues.md` P1-2); D4 env vars have no effect in production |
| G5 | Single writer; detect human edits by blob sha and never overwrite; dead-letter push-protection rejections | ✅ `github.py:314-344`, `PushProtectionError`; `tests/test_github_conflicts.py` | ❌ the loop passes no `last_blob_sha`, so **human edits on GitHub are overwritten** |
| G6 | Private-repo guard before every batch | ✅ `verify_private_repo` (`github.py:223`) | ⚠ **off by default**: `THREADVAULT_ALLOW_PUBLIC_REPO` defaults to `"true"` (`sync_loop.py:77`) → `verify_private=False` (`03-issues.md` P0-3) |
| G6 | With envelope encryption on, refuse plaintext export unless `allow_plaintext_with_encryption` | ❌ not found (and B2 encryption is not applied anyway) | ❌ |
| G6 | Deletion semantics in README/onboarding; `cli.github squash` (the only force-push) | ✅ `cli/github.py`; `tests/test_github_squash.py`; README text in `layout.py` | — |
| G7 | Local offload: pointer `_index/offloaded.json`, rehydrate on write, continuation thread on failure; export manifest; fsck aware | ✅ `export/offload.py`; `storage/writer.py:393-396`; `tests/test_github_offload.py` | ❌ not used on Render; ? fsck support not re-verified |
| G8 | Per-account export config in Postgres, encrypted tokens, export status visible in `vault_stats` | ◐ remote target exists with the single-tenant guard (`export/worker.py:88-130,226-234`); no per-account config table | ❌ |

## Milestones
| # | Status | Commit |
|---|---|---|
| GH1 Data API target | ✅ (except the 20 MB split) | `0302bd5` |
| GH2 layout + indexes | ✅ | `d9b80be` |
| GH3 commit policy | ✅ library only | `a736eeb` |
| GH4 conflicts + dead-letter | ✅ library only | `ce39084` |
| GH5 deletion + squash | ✅ | `f21e088` |
| GH6 auth | ✅ library only | `91c011e` |
| GH7 offload | ✅ stdio | `e0c57b5` |
| GH8 live verification | ⏸ gate g. 4 live tests skipped (`tests/test_github_live.py`); runner `scripts/verify_github_live.py` | `47a9890` |
| D4 configurable settle/batch | ✅ library only | `1f48fc8` |
| empty-repo bootstrap | ⚠ probably still broken | `53284c7` |

## Open items owned by B4 (most are the B5 fix)
1. Replace the body of `sync_loop.run_sync_once` with `GitHubBatchExporter` + `FileStoreExportManifest`: settle/batch, unchanged-page skip, conflict detection, dead-letter.
2. Default to the private-repo guard; require an explicit opt-out.
3. Empty repo: bootstrap with one Contents API `PUT README.md`, then use the Data API.
4. 20 MB tree split (first sync or large backfill).
5. Plaintext-with-encryption guard (after B2 actually encrypts).
6. GH8 live run against a throwaway private repo.
7. ✅ 2026-10-06 (committed `bad9e0c`) **Local read-only mirror** of the deployed archive: `scripts/pull_remote_vault.ps1` clones/fast-forwards `THREADVAULT_GH_REPO` into `vault_rich_fixture/` (`-Every N` keeps pulling). Refuses to overwrite a non-mirror folder. Verified: clone + re-pull, 10 pages.
8. **`yashdev4/thread_vault` is public right now** (P0-3 confirmed). Owner to make it private; then flip the code default (item 2).
9. Remove the 3 `test-account` fixture pages from the production mirror and stop local runs from pushing to it (`run_local.ps1` already blanks `THREADVAULT_GH_REPO`).

## Change log (newest first)
- 2026-10-07 · FIXED · P1-2: GitHub sync runs after saves only (FileStore `export_signal`, 30 s settle, 300 s max wait). It uploads only changed files (git blob sha vs branch tree) and makes no commit when nothing differs; the startup check pushes only what's missing. `render.yaml` now sets `THREADVAULT_SYNC_SETTLE_SECONDS` instead of the 60 s interval. Uncommitted.
- 2026-10-06 · ADDED · Local mirror pull script (item 7); P0-3 confirmed live: repo public; test-fixture pages found in the production mirror.
- 2026-10-05 · FOUND · Audit: the deployed loop bypasses G4/G5/G6; public repos allowed by default; 20 MB split and plaintext guard missing; empty-repo bootstrap likely broken.
- 2026-10-05 17:42 · REVISED · `1f48fc8` D4 settle/batch via env (library only).
- 2026-10-05 16:15 · BRANCHED → B5 · `53284c7` empty-repo bootstrap and batch sync wired into the B5 loop.
- 2026-10-05 14:44 · BRANCHED → B5 · `fcbfe80` the `sync_loop.py` mirror loop bypasses the G4/G5 policy layer.
- 2026-10-05 00:36 → 01:18 · ADDED · GH1–GH8 (`0302bd5`…`47a9890`).
