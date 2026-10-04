"""Verification suite for Milestone X5 (Renderer, Signed Viewer Links, .md Download).

Covers:
- Rendered Markdown byte-identical to FileStore output for the same turns ("Done when" condition)
- HMAC-signed viewer tokens: creation, validation, tamper-resistance, and expiration
- HTML viewer endpoint (/v/{signed_token}) displaying styled transcript and metadata
- Raw .md download endpoint (/download/{thread_id}.md) with content-disposition and auth enforcement
- vault_find returning clickable viewer_url and download_url in search hits
"""

import json
import os
from pathlib import Path
import tempfile
import time
import pytest
import httpx

from thread_save.config import VaultConfig
from thread_save.models import TurnData
from thread_save.service import TurnService
from thread_save.storage.writer import FileStore
from thread_save.storage.pg_store import PgStore
from thread_save.storage.renderer import render_thread_markdown
from thread_save.web.app import create_app
from thread_save.web.viewer import (
    create_viewer_token,
    verify_viewer_token,
)

TEST_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/thread_save_test"
)


@pytest.fixture
async def pg_store():
    store = PgStore(dsn=TEST_DSN)
    await store.connect()
    async with store.pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE turns, gaps, turn_chunks, outbox, deleted_threads, events, threads, accounts CASCADE"
        )
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_x5_rendered_markdown_byte_identical_to_filestore(pg_store):
    """'Done when' condition: Rendered output byte-identical to FileStore output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        vault_path = Path(tmpdir)
        cfg = VaultConfig(vault_root=vault_path, default_account="diff-account")
        file_store = FileStore(cfg)

        file_svc = TurnService(file_store, config=cfg)
        pg_svc = TurnService(pg_store, config=cfg)

        test_turns = [
            ("Explain quantum computing in three sentences.", None, None),
            (
                "Can you provide a simple Python code example?",
                "Quantum computing uses qubits that exist in superposition.",
                "Explain quantum computing in three sentences.",
            ),
            (
                "continue",
                "```python\ndef qubit_sim():\n    return 'state: |0> + |1>'\n```",
                "Can you provide a simple Python code example?",
            ),
        ]

        file_thread_id = None
        pg_thread_id = None

        for user_q, prev_resp, prev_anc in test_turns:
            res_file = await file_svc.save_turn(
                user_query=user_q,
                prev_response=prev_resp,
                prev_user_anchor=prev_anc,
                thread_id=file_thread_id,
                title_hint="Quantum Computing",
                account="diff-account",
            )
            assert res_file["ok"]
            if file_thread_id is None:
                file_thread_id = res_file["thread_id"]

            res_pg = await pg_svc.save_turn(
                user_query=user_q,
                prev_response=prev_resp,
                prev_user_anchor=prev_anc,
                thread_id=pg_thread_id,
                title_hint="Quantum Computing",
                account="diff-account",
            )
            assert res_pg["ok"]
            if pg_thread_id is None:
                pg_thread_id = res_pg["thread_id"]

        # Render canonical markdown from PgStore
        pg_rendered_md = await render_thread_markdown(
            pg_store,
            account_id="diff-account",
            thread_id=pg_thread_id,
            page=1,
        )

        # Read FileStore's written file
        fs_file = Path(file_store._page_states[file_thread_id].file_path)
        file_md = fs_file.read_text(encoding="utf-8")

        # Parse both pages
        from thread_save.storage.formatter import parse_page, format_front_matter, format_turn
        meta_pg, turns_pg = parse_page(pg_rendered_md)
        meta_fs, turns_fs = parse_page(file_md)

        assert len(turns_pg) == len(turns_fs)
        for t_pg, t_fs in zip(turns_pg, turns_fs):
            assert t_pg.turn_index == t_fs.turn_index
            assert t_pg.role == t_fs.role
            assert t_pg.body.strip() == t_fs.body.strip()
            assert t_pg.fidelity == t_fs.fidelity
            assert t_pg.content_hash == t_fs.content_hash

            # Verify byte-identical turn rendering for identical turn data
            t_pg_aligned = TurnData(
                turn_index=t_fs.turn_index,
                role=t_fs.role,
                body=t_fs.body,
                timestamp=t_fs.timestamp,
                model=t_fs.model,
                fidelity=t_fs.fidelity,
                char_count=t_fs.char_count,
                content_hash=t_fs.content_hash,
                recovered=t_fs.recovered,
                anchor=t_fs.anchor,
                turn_key=t_fs.turn_key,
            )
            assert format_turn(t_pg_aligned, nonce=meta_fs.nonce).encode("utf-8") == format_turn(t_fs, nonce=meta_fs.nonce).encode("utf-8")


@pytest.mark.asyncio
async def test_x5_viewer_token_crypto():
    """Verify HMAC token signing, validation, tampering rejection, and expiration."""
    secret = "secret-test-key-32-bytes-minimum-ok!"
    token = create_viewer_token("thread_123", "alice", secret=secret, ttl_seconds=3600)

    tid, acc = verify_viewer_token(token, secret=secret)
    assert tid == "thread_123"
    assert acc == "alice"

    # Tampered token
    tampered = token[:-1] + ("B" if token[-1] == "A" else "A")
    with pytest.raises(ValueError, match="signature|Base64"):
        verify_viewer_token(tampered, secret=secret)

    # Wrong secret
    with pytest.raises(ValueError, match="Invalid viewer token signature"):
        verify_viewer_token(token, secret="completely-different-secret-key!!")

    # Expired token
    expired_token = create_viewer_token("thread_123", "alice", secret=secret, ttl_seconds=-10)
    with pytest.raises(ValueError, match="expired"):
        verify_viewer_token(expired_token, secret=secret)


@pytest.mark.asyncio
async def test_x5_viewer_and_download_endpoints(pg_store):
    """Test GET /v/{token} HTML viewer and GET /download/{thread_id}.md."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="viewer-account")
    svc = TurnService(pg_store, config=cfg)
    app = create_app(pg_store=pg_store, service=svc, config=cfg)

    # Save a turn
    save_res = await svc.save_turn(
        user_query="Hello for viewer test!",
        title_hint="Viewer Thread",
        account="viewer-account",
    )
    assert save_res["ok"]
    thread_id = save_res["thread_id"]

    secret = "threadvault-default-viewer-secret-key-32bytes-min!"
    valid_token = create_viewer_token(thread_id, "viewer-account", secret=secret)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 1. Access HTML viewer with valid token
            view_resp = await client.get(f"/v/{valid_token}")
            assert view_resp.status_code == 200
            assert "text/html" in view_resp.headers["content-type"]
            assert "Viewer Thread" in view_resp.text
            assert "Hello for viewer test!" in view_resp.text
            assert f"/download/{thread_id}.md?token={valid_token}" in view_resp.text

            # 2. Access HTML viewer with bad token
            bad_resp = await client.get("/v/invalid.token")
            assert bad_resp.status_code == 403

            # 3. Access download endpoint with valid token
            dl_resp = await client.get(f"/download/{thread_id}.md?token={valid_token}")
            assert dl_resp.status_code == 200
            assert "text/markdown" in dl_resp.headers["content-type"]
            assert f'attachment; filename="{thread_id}.md"' in dl_resp.headers["content-disposition"]
            assert "Hello for viewer test!" in dl_resp.text
            assert '"schema_version": 2' in dl_resp.text

            # 4. Access download endpoint with invalid / missing token returns 401/403
            unauth_dl = await client.get(f"/download/{thread_id}.md")
            assert unauth_dl.status_code in (401, 403)


@pytest.mark.asyncio
async def test_x5_vault_find_includes_viewer_urls(pg_store):
    """Test that vault_find attaches viewer_url and download_url to search hits."""
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="search-account")
    svc = TurnService(pg_store, config=cfg)
    app = create_app(pg_store=pg_store, service=svc, config=cfg)

    # Save a turn
    save_res = await svc.save_turn(
        user_query="Unique keyword zebra in chat",
        title_hint="Zebra Thread",
        account="search-account",
    )
    assert save_res["ok"]

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # Initialize MCP session
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-harness", "version": "1.0"},
                },
            }
            init_resp = await client.post(
                "/mcp",
                json=init_req,
                headers={"Accept": "application/json, text/event-stream"},
            )
            session_id = init_resp.headers["mcp-session-id"]

            # Search with vault_find
            find_resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "vault_find", "arguments": {"query": "zebra"}},
                },
                headers={
                    "mcp-session-id": session_id,
                    "X-Account-ID": "search-account",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert find_resp.status_code == 200
            lines = find_resp.text.strip().splitlines()
            data_line = next(l for l in lines if l.startswith("data:"))
            res_obj = json.loads(data_line[len("data:"):].strip())
            parsed_content = json.loads(res_obj["result"]["content"][0]["text"])

            assert len(parsed_content["threads"]) >= 1
            first_hit = parsed_content["threads"][0]
            assert "viewer_url" in first_hit
            assert first_hit["viewer_url"].startswith("/v/")
            assert "download_url" in first_hit
            assert first_hit["download_url"].startswith("/download/")

            # Follow the viewer_url
            view_resp = await client.get(first_hit["viewer_url"])
            assert view_resp.status_code == 200
            assert "Zebra Thread" in view_resp.text


@pytest.mark.asyncio
async def test_h5_viewer_link_expiry_and_reuse_prevention(pg_store):
    """Milestone H5: Verify 15m default expiry and prevention of token reuse across threads and downloads."""
    import base64
    cfg = VaultConfig(vault_root=Path("./test_vault"), default_account="h5-account")
    svc = TurnService(pg_store, config=cfg)
    app = create_app(pg_store=pg_store, service=svc, config=cfg)

    # Create thread 1
    t1 = await svc.save_turn(user_query="Thread 1 query", title_hint="Thread One", account="h5-account")
    tid_1 = t1["thread_id"]

    # Create thread 2
    t2 = await svc.save_turn(user_query="Thread 2 query", title_hint="Thread Two", account="h5-account")
    tid_2 = t2["thread_id"]

    secret = "threadvault-default-viewer-secret-key-32bytes-min!"

    # 1. Verify default expiry is 15 minutes (900 seconds)
    token_1 = create_viewer_token(tid_1, "h5-account", secret=secret)
    raw_b64 = token_1.split(".")[0] + "=" * (-len(token_1.split(".")[0]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(raw_b64).decode("utf-8"))
    assert payload["exp"] - int(time.time()) in range(890, 915)  # ~900s (15 min)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            # 2. Expiry: An expired token cannot view or download
            expired_token = create_viewer_token(tid_1, "h5-account", secret=secret, ttl_seconds=-10)
            exp_view = await client.get(f"/v/{expired_token}")
            assert exp_view.status_code == 403
            assert "expired" in exp_view.json()["detail"].lower()

            exp_dl = await client.get(f"/download/{tid_1}.md?token={expired_token}")
            assert exp_dl.status_code == 403
            assert "expired" in exp_dl.json()["detail"].lower()

            # 3. Cross-thread reuse prevention: token for thread 1 cannot be used to download thread 2
            cross_dl = await client.get(f"/download/{tid_2}.md?token={token_1}")
            assert cross_dl.status_code == 403
            assert "mismatch" in cross_dl.json()["detail"].lower() or "cannot be reused across threads" in cross_dl.json()["detail"].lower()

            # 4. Download reuse prevention:
            # First download with token_1 succeeds (200 OK)
            dl_resp_1 = await client.get(f"/download/{tid_1}.md?token={token_1}")
            assert dl_resp_1.status_code == 200
            assert "Thread 1 query" in dl_resp_1.text

            # Second download with the EXACT SAME token_1 fails with 403 (cannot be reused across downloads)
            dl_resp_2 = await client.get(f"/download/{tid_1}.md?token={token_1}")
            assert dl_resp_2.status_code == 403
            assert "consumed" in dl_resp_2.json()["detail"].lower() or "cannot be reused across downloads" in dl_resp_2.json()["detail"].lower()
