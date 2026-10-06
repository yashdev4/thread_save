# 01 — Project understanding

## What it is
**ThreadVault** (`thread-save-mcp`, package `thread_save`, Python ≥3.11) is an MCP server that archives a user's Claude conversations. Claude is told (via tool descriptions + server instructions) to call `vault_save_turn` at the start of every reply, passing the user's new message plus the *previous* assistant reply. The server stitches those into dense, numbered turns per thread and stores them as canonical Markdown ("Plan v2") or Postgres rows, optionally mirroring to a GitHub repo.

Tools (identical over stdio and HTTP): `vault_save_turn`, `vault_backfill`, `vault_find`, `vault_stats`.

## Three run modes (this matters — they behave very differently)
| Mode | Entry | Storage | Auth | Notes |
|---|---|---|---|---|
| **stdio / local** | `python -m thread_save.server` (see `claude_desktop_config.example.json`) | `FileStore` (markdown files under `THREAD_SAVE_VAULT_ROOT`) | none | Original product; also has SQLite FTS index (`index/sqlite_index.py`) |
| **Remote full** (designed, never deployed) | `uvicorn thread_save.web.app:create_app --factory` + `DATABASE_URL` | `PgStore` (asyncpg, RLS, Alembic) | OAuth 2.1 + Google IdP + JWT | Viewer `/v/{token}`, downloads, outbox exporter, 7 required secrets |
| **Remote standalone** (**what is actually on Render**) | same uvicorn command, `THREADVAULT_STORAGE_BACKEND=file` | `FileStore` on server disk | **paused** (`THREADVAULT_AUTH_PAUSED=true`) | Background loop pushes the vault to GitHub every 60 s |

Mode selection is in `web/app.py:58-66`: `file` backend (or no `DATABASE_URL`) → `FileStore`, else `PgStore`.

## Core protocol (store-agnostic: `service.py:TurnService.save_turn`)
1. Global pause (`vault/.paused`) / per-thread opt-out phrases ("don't save this chat" …).
2. **Bind thread**: by `thread_id`, else by `prev_user_anchor` (first 80 chars of previous user msg, normalised), else create new. Principle: *split, never merge* (invariant I-3).
3. Chunk reassembly for long `prev_response` (`chunk_index`/`is_final`, in-memory buffer, 5 min timeout).
4. Truncate >100 000 chars → `Fidelity.TRUNCATED`. Redact secrets (regex + HMAC tag, `security/redactor.py`).
5. `turn_key = sha256(prev_anchor | query | hash(prev_response) | client_turn_number)` → idempotent retries.
6. One transaction: compute `n`, detect gaps (write **stubs**), upsert `(n-1, assistant)`, upsert `(n, user)`, put an `OPEN` placeholder assistant slot, enqueue export.
7. Response: `{ok, thread_id, n, binding, missing?}`; `missing` (cap 10) tells Claude what to send via `vault_backfill`.

Fidelity ranks: `OPEN < STUB < TRUNCATED < ABRIDGED < VERBATIM` (higher overwrites lower; see `models.py`).

## Invariants (W-1…W-10, checked by `fsck.py` for both stores)
Dense turn numbering per thread; canonical hashes recomputed; delimiter nonces (`<!-- turn nonce=… -->`) so user text can't forge turn boundaries; sticky pages (rollover at 400 KB / 60 turns, closed pages never rewritten); stubs ↔ gaps consistency; tombstones (`deleted_threads`) so late saves can't resurrect deleted threads; orphan reaper for 1-turn dead threads.

## Module map (`src/thread_save/`)
- `service.py` — TurnService (protocol logic). `models.py` — Pydantic models, `Fidelity`, `MAX_BODY_CHARS`.
- `storage/` — `protocol.py` (Store/ThreadTxn Protocols), `writer.py` (**FileStore**, 739 lines), `pg_store.py` (**PgStore**, 976 lines, RLS + advisory locks), `formatter.py` (page parse/render, `QuotedDumper`), `renderer.py` (PG rows → canonical markdown, byte-identical to FileStore), `identity.py` (ULID + anchor normalise), `gaps.py`, `orphan.py`, `path_resolver.py`.
- `security/` — `idempotency.py` (`canonical_v1`, hashes), `redactor.py`, `encryption.py` (AES-256-GCM/HKDF), `sanitizer.py`.
- `web/` — `app.py` (factory + lifespan), `mcp_server.py` (FastMCP/`MCPServer` over Streamable HTTP, mounted at `/mcp`), `middleware.py` (Origin/Host, body size, rate limit, account context), `oauth.py` (986 lines: DCR, PKCE, JWT, Google upstream), `viewer.py` (HMAC links, **PgStore only**), `startup.py` (fail-closed checks), `context.py` (`current_account_id` ContextVar).
- `export/` — `github.py` (Git Data API target: 3 writes per batch), `layout.py` (archive tree: README, `index/threads.json`, `index/YYYY-MM.md`), `sync.py` (`GitHubBatchExporter`: settle/batch/conflict/dead-letter), `sync_loop.py` (**simple 60 s push loop actually used in standalone mode**), `worker.py` (PG outbox worker), `offload.py` (cold-thread offload + rehydrate), `auth.py` (PAT expiry, App tokens, token scrubbing).
- `cli/` — `migrate.py` (local vault → PG, dry-run default), `github.py` (`squash`). `report.py` (coverage diagnostics, `--dsn`), `fsck.py` (`--dsn`).
- `alembic/` — 4 migrations (init schema, anchor, retention_days, oauth tables).

## Infra files
`Dockerfile` (Fly, 2 workers, runs alembic then uvicorn), `fly.toml`, `render.yaml` (**current deploy target**, native Python, 1 GB disk at `/data/vault`), `requirements.txt`/`pyproject.toml`.

## Tests
`tests/` ≈ 25 files; Postgres tests expect `postgresql://postgres:@127.0.0.1:5432/thread_save_test` (override with `DATABASE_URL`). `tests/test_hypothesis_v2.py` runs 1 000 property-based examples on each store (~2–4 min). `run_tests.py` (21 protocol scenarios) and `test_smoke.py` are standalone root-level runners. Live GitHub tests are `@pytest.mark.live` and skip without `THREADVAULT_GH_LIVE_*`.
