# 02 — Current state (as of 2026-10-05)

## Git / repo
- Branch `master`, HEAD `a672217`. Recent work: D0–D4 (instruction style, ≤80-char openings, late-join backfill, PG coverage report, configurable GH intervals), then deploy hotfixes (`77ec4bd` restore preDeployCommand, `a672217` keep "ThreadVault account" in description).
- Working tree: `.gitignore` modified (adds `PROGRESS.md`, `.docs/`); `PROGRESS.md` **staged for deletion** (it is tracked *and* now git-ignored); `PROGRESSs.md` (typo'd copy, 963 lines) is untracked. Net effect: the progress ledger is about to vanish from the repo unless `PROGRESSs.md` is renamed back / added.
- No real secrets in history (checked `.env` never committed; `ghp_…`/`github_pat_…` hits in `tests/` are fake fixtures like `ghp_test_12345`). `.env` locally holds a `GITHUB_TOKEN` — keep ignored.

## Tests (run 2026-10-05, Python 3.14.4, mcp 2.2.0, local Postgres up)
- `pytest tests --ignore=test_hypothesis*.py` → **123 passed, 4 skipped (live GitHub), 0 failed** in 32 s.
- `test_results.txt` (from `run_tests.py`) → 21/21 protocol scenarios pass.
- Hypothesis profiles were not re-run this session (last recorded green: 1 000 examples × 2 stores).
- ⚠ Green tests prove the *Postgres + OAuth + outbox* design. The **deployed standalone path** (`web/app.py` file mode + `export/sync_loop.py` + paused auth) has **no tests at all** (grep for `sync_loop`, `run_sync_once`, `AUTH_PAUSED` in `tests/` returns nothing; commit `fcbfe80` added the mode and only removed a test).

## What is actually deployed (render.yaml)
Native Python web service `threadvault-web`, Render Ohio, starter plan:
- `THREADVAULT_STORAGE_BACKEND=file`, `THREADVAULT_ROOT=/data/vault`, 1 GB disk at `/data/vault`.
- `THREADVAULT_AUTH_PAUSED=true`, `THREADVAULT_ENFORCE_AUTH=false`, `THREADVAULT_ALLOWED_HOSTS=*`.
- GitHub sync: `THREADVAULT_GH_REPO=https://github.com/yashdev4/thread_vault.git`, every 60 s, token from `GITHUB_TOKEN` (set in dashboard).
- `preDeployCommand: python -m alembic upgrade head` (irrelevant for file mode).
- No `THREADVAULT_PUBLIC_URL`, no Google creds, no DB. Gates (a)/(b)/(f) in the ledger are therefore *not* what was done; the team took a shortcut to get Claude connecting.

## Doc vs. reality drift
| Doc says | Reality |
|---|---|
| `DEPLOYMENT.md`/`ONBOARDING.md`: Postgres + Google OAuth, 7 required secrets, auth fail-closed | Deployed with file store, auth **paused**, none of those secrets (bypassed by the `AUTH_PAUSED` early-return in `startup.py:91`) |
| `STATUS_REPORT_*`: 116 tests, "deployment-ready", last commit `c8d2b4b` | Now 123+4; 20+ commits since, incl. the unauthenticated standalone mode; reports don't mention it |
| `PROGRESS` D4: settle/batch env vars drive GitHub updates | Only `GitHubBatchExporter` reads them; the deployed `sync_loop.py` ignores them (fixed 60 s) |
| Tool server instructions say "local markdown archive" | True for stdio; remote description body says "ThreadVault account" (kept intentionally in `a672217`) |
| `STATUS_REPORT` tree lists `cli.py`, `security/crypto.py`, `scripts/test_cross_surface.py` | Actual: `cli/` package, `security/encryption.py`, `tests/test_cross_surface.py` |
| `.env` / `render.yaml` use `THREADVAULT_ROOT` | Code only reads `THREAD_SAVE_VAULT_ROOT` — see P0-2 |

## Root-level clutter (candidates to delete/move)
One-shot patch scripts already applied: `patch.py`, `patch3.py`, `patch_fsck.py`, `patch_hmac.py` (embeds a hard-coded key `threadvault_dev_key`; **not** applied — `idempotency.py` has no such key), `patch_hyp.py`, `patch_hyp2.py`, `patch_tests.py`, `patch_writer.py`, `inject_tests.py`, `add_protocol_methods.py`. Ad-hoc: `test_3_5.py`, `test_smoke.py`, `run_tests.py` (21-scenario runner, still useful → move to `scripts/`). Generated/ignored: `hypothesis_out.txt` (218 KB), `test_results.txt`, `vault_rich_fixture/` (fsck golden fixture, referenced by ledger — keep but move under `tests/fixtures/`), `__pycache__`, `.hypothesis`.
