# 04 — Action plan

Goal: get the deployed ThreadVault (Render, file store + GitHub mirror) safe and reliable, then decide whether to graduate to the Postgres/OAuth design that the tests already prove.

## Step 0 — Confirm facts on the live service (no code changes)
1. Is `github.com/yashdev4/thread_vault` **private**? (P0-3)
2. Render shell / logs: does `/data/vault` contain threads, or `~/thread_vault`? Did the last deploy wipe data? (P0-2)
3. Render logs: did `preDeployCommand` succeed or fail with `No module named alembic`? (P1-1) Any `409 Git Repository is empty` or "Unexpected error syncing" lines? (P1-3)
4. GitHub repo: how many commits since deploy, and do index files list all threads? (P1-2, P0-2)
5. Ask the owner: *what symptoms are you actually seeing?* (e.g. Claude not connecting, saves missing, nothing in GitHub, 401/403). Match them to the list in `03-issues.md` before touching code.

## Step 1 — Stop the bleeding (small, safe diffs; each with a test)
| # | Change | Files |
|---|---|---|
| 1 | Read `THREADVAULT_ROOT` (alias of `THREAD_SAVE_VAULT_ROOT`) | `config.py`, `tests/` |
| 2 | Default `THREADVAULT_ALLOW_PUBLIC_REPO=false` | `export/sync_loop.py`, `render.yaml` |
| 3 | Add a static bearer-token gate while auth is "paused" (`THREADVAULT_STATIC_TOKEN`); Claude connector can send it as the custom-connector auth header; set it in Render dashboard | `web/middleware.py`, `web/startup.py`, tests |
| 4 | Add `alembic`+`sqlalchemy` deps **or** make preDeploy conditional on `DATABASE_URL` | `requirements.txt`, `pyproject.toml`, `render.yaml` |
| 5 | Skip a push when nothing changed (compare tree/manifest hash) | `export/sync_loop.py` |

## Step 2 — Make the standalone path real
- Wire `GitHubBatchExporter` + `FileStoreExportManifest` into the loop (settle/batch, conflict detection, dead-letter); honour `THREADVAULT_GH_SETTLE_MINUTES/BATCH_MINUTES`.
- Empty-repo bootstrap via Contents API; configurable branch.
- Add tests: `run_sync_once` with `httpx.MockTransport` (there is already a `MockGitHubApi` in `tests/test_github_export.py` to reuse), auth-paused middleware behaviour, vault-root resolution.
- Drop dead `viewer_url`/`download_url` in file mode or implement a FileStore viewer.

## Step 3 — Decide the destination architecture (owner decision)
- **A. Stay on FileStore + GitHub** (simple, cheap): finish Step 2, add real auth (static token or Google OAuth with file-backed state), delete the Postgres-only deploy docs or mark them optional.
- **B. Go to PgStore + OAuth** (what the 123 tests cover): provision Render Postgres, set the 7 secrets, run `thread-save-migrate --apply` from the local vault, switch `STORAGE_BACKEND`, use the outbox exporter (needs `THREADVAULT_SINGLE_TENANT=true`).
Recommendation: A for now (it is what is deployed; smallest path to "safe"), B once more than one user needs it.

## Step 4 — Docs & repo hygiene
- Resolve `PROGRESS.md`/`PROGRESSs.md`; refresh `STATUS_REPORT_*` and `DEPLOYMENT.md` to describe the real deployed mode; document env vars actually read by the code (`grep -rhoE 'environ.get\("[A-Z_]+"' src | sort -u`).
- Clean root clutter (list in `02-current-state.md`). Move one-shot patch scripts out or delete.
- Fix the missing `Optional`/`Any` imports; dedupe description lines; pin `mcp>=2`.

## Verification commands
```powershell
# fast suite (~30 s, needs local Postgres thread_save_test)
python -m pytest tests -q --ignore=tests/test_hypothesis_v2.py --ignore=tests/test_hypothesis.py
# protocol scenarios
python run_tests.py
# invariants
python -m thread_save.fsck vault_rich_fixture
python -m thread_save.fsck --dsn postgresql://postgres:@127.0.0.1:5432/thread_save_test
# full property-based (≈2–5 min)
python -m pytest tests/test_hypothesis_v2.py -q
# live smoke of the deployed server
curl https://<service>.onrender.com/health
```

## Working agreements for future Claude sessions
- Treat `PROGRESSs.md`/ledger claims as *intent*; verify against code + this folder.
- Never print `.env` values (it holds a GitHub token). Do not commit secrets.
- Avoid adding coercive words to tool descriptions (`tests/test_instruction_style.py` enforces; first line ≤ 80 chars incl. "every reply" for `vault_save_turn`).
- Any change to the turn format must keep FileStore ↔ PgStore byte-identical rendering (differential tests) and pass `fsck` on both.
