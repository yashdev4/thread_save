# B0: Foundation (Implementation Plan v2)

- **Source:** `thread_saving_mcp_plan_v2 by claude.md` (revision of `thread_saving_mcp_plan.md` v1)
- **Scope:** an MCP stdio server on the user's machine that writes conversation turns to Markdown files, with an SQLite index.
- **Children:** B1 (from §8 + §11), B2 (from §9), B3 (from §11, the 25% write path). Security (§7, 20%) was not split out; it lives in B2 S8 and B3 WD/WF.
- **Today:** this is the **stdio mode** (`python -m thread_save.server`, `server.py`), backed by `FileStore`. It still works, has the most tests (`run_tests.py` 21/21) and is the dev harness for B2/B3.

## Decision ledger

| § | Plan v2 decision | Status | Evidence / where it went |
|---|---|---|---|
| 0 | Every assistant turn has a `fidelity` flag; incomplete archives are visibly incomplete | ✅ | `models.py` Fidelity: verbatim 4 > abridged 3 > truncated 2 > stub 1 > open 0 (rank extended by B1 §3.2) |
| 1.1 | `vault/{account}/{YYYY}/{MM}/`, `_index/`, `_assets/` | ✅ | `storage/path_resolver.py:39-52`, `config.py:96-128` |
| 1.1 | Ship `.gitignore` = `*` at vault root | ✅ | `storage/path_resolver.py:127-133` |
| 1.2 | Filename `{date}T{HHmm}_{short}_{slug}_p{NN}.md`, **no account segment** | 🔀 | Account segment **re-added**: `{date}T{HHmm}_{account}_{short}_{slug}_p{NN}.md` (`path_resolver.py:66,81`). Driven by B2 S1 ("same filename convention including the `account` segment") and B3 fsck's filename/front matter/directory consistency check |
| 1.2 | `thread_id_short` is 6 chars of base32 | ◐→B3 | Taken from the ULID **random tail** (`storage/identity.py:33-35`) per B3 WA |
| 2.1 | `thread_open` tool allocates the thread; `save_turn` appends | 🔀 B1 | `thread_open` removed from the tool list (B1 R1); the first `vault_save_turn` creates the thread |
| 2.1–2.2 | `active.json` fallback binds a call without `thread_id` to the client's latest thread | 🔀 B1 | Replaced by anchor binding (B1 §4.1). `active.json` kept read-only (`storage/identity.py:4-9`) |
| 3.1 | No code fences around bodies; `## User` / `## Claude` headings | ✅ | `storage/formatter.py:202` |
| 3.1 | HTML-comment open + close delimiters | ✅ → B3 | Per-thread nonce `k=` added by B3 WF (`formatter.py:88`) |
| 3.1 | Server-generated timestamps with UTC offset | ✅ | `formatter.py:124,177` (`datetime.now().astimezone()` when not supplied) |
| 3.3 | Attachments recorded as markers, not content | ◐ | Marker count is written (`formatter.py:189-207`, `models.py:101`); `attachments.mode = store` not implemented ? |
| 3.4 | Gap detection via `client_turn_number` | ✅ → B1 | Extended into B1 §5 gaps/backfill (`storage/gaps.py`); `run_tests.py` Test 14 |
| 4.1 | Per-file async mutex, page-state cache, atomic tmp + fsync + rename | ✅ | `storage/writer.py:705-728` (two retries on `OSError`, which matches B3 WG's Windows rule) |
| 4.2 | Roll over at 400 KB or 60 turns; never split a turn | ✅ → B3 | `config.py:43-44`; made **sticky** (page assigned once) by B3 WE |
| 4.3 | Chunked long responses (`chunk_index`, `is_final`) | ✅ | `service.py:50-90` `_ChunkBuffer` |
| 4.3 | Buffer older than 5 min flushes as `fidelity: truncated` | ❌ ⚠ | `started_at` is recorded but never read (`service.py:72`); no flush exists. `run_tests.py` Test 13 calls `_flush_stale_chunks` only `if hasattr(...)` and asserts nothing → hollow pass. See `03-issues.md` P1-7 |
| 5 | Tools `thread_open`, `save_turn`, `find_thread`, `thread_stats` | 🔀 B1 | Now `vault_save_turn`, `vault_backfill`, `vault_find`, `vault_stats` (`server.py:150-365`) |
| 6 | SQLite FTS5 index; Markdown is the source of truth; index failures never fail a save | ✅ | `index/sqlite_index.py:65`. Remote mode uses Postgres `tsvector` instead (B2) |
| 6 | `rebuild_index` CLI | ? | Not found by name in `index/sqlite_index.py`. Check before relying on it |
| 7.1 | Path sanitisation matrix | ✅ | `security/sanitizer.py`, `tests/test_slug_validation.py` |
| 7.1 | Slug capped at 40 chars | ◐ DIVERGED | Capped at **20** (`config.py:53` `slug_max_chars = 20`; `pg_store.py:447,537`). Not recorded as a decision |
| 7.2 | Redaction before write, mask `[REDACTED:type:hash]` | ✅ → B3 | `security/redactor.py`; mask hash became an HMAC by B3 WD (`redactor.py:85-125`) |
| 7.2 | `redaction.mode = mask \| skip_turn \| off` | ◐ | Only on/off (`THREAD_SAVE_REDACTION`, `config.py:169`); no `skip_turn` |
| 7.3 | stdout guard before imports | ✅ | `server.py:21-22` |
| 7.4 | Skip the write if the hash matches one of the last 5 turns | 🔀 B1→B3 | Replaced by the B1 slot upsert (`security/idempotency.py` `SlotIndex`), then by the B3 `turn_key` triple |
| 8 | Always return `log_next_turn: true` | 🔀 B1 | Behind `THREADVAULT_NUDGE`, default **off** (B1 §6.4; `service.py:388`) |
| 8.1 | Description must say writes are local | ⚠ | The stdio description now says "in their ThreadVault account" (`server.py:119-120`), changed by D1. See B1 and `03-issues.md` P2-10 |
| 9 | Partition the synced vault by machine, or a single-writer lockfile | 🔀 B2 | Made unnecessary by B2's single remote writer. Not implemented for stdio |
| 10 | Milestone 1: TypeScript/ESM project, zod | 🔀 | Built in **Python** with the official `mcp` SDK (B1's "fixed decisions") |
| 11 | Effort weights 25/25/20/15/10/5 | BRANCHED | 25% write path → B3; 25% reliability → B1 |

## Open items owned by B0 (stdio mode)
- 5-minute stale-chunk flush (§4.3), plus a real assertion in Test 13.
- Decide between 20 and 40 for the slug cap, and record the decision.
- Restore "local files on this computer" in the **stdio** description (§8.1, B1 §6.1).

## Change log (newest first)
- 2026-10-05 · FOUND · Audit created this file. Recorded: account segment re-added to filenames, slug cap 40→20, stale-chunk flush missing (Test 13 hollow), stdio description lost the "local" destination.
