"""Thread viewer and markdown download endpoints (§4, Milestone X5).

Implements:
- HMAC signed viewer links for secure external access (/v/{signed_token})
- Full HTML conversation transcript rendering
- Raw .md download endpoint (/download/{thread_id}.md)
- Integration with renderer for byte-identical plan v2 markdown
"""

from __future__ import annotations

import base64
import html
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

from thread_save.storage.pg_store import PgStore, _RANK_TO_FIDELITY
from thread_save.storage.renderer import render_thread_markdown
from thread_save.web.context import current_account_id

logger = logging.getLogger("thread_save.web.viewer")

VIEWER_SECRET_DEFAULT = os.environ.get(
    "THREADVAULT_VIEWER_SECRET",
    "threadvault-default-viewer-secret-key-32bytes-min!",
)
DEFAULT_VIEWER_TTL_SECONDS = int(
    os.environ.get("THREADVAULT_VIEWER_TTL_SECONDS", "900")
)  # 15 minutes default (§H5)


def create_viewer_token(
    thread_id: str,
    account_id: str,
    secret: str = VIEWER_SECRET_DEFAULT,
    ttl_seconds: int = DEFAULT_VIEWER_TTL_SECONDS,
) -> str:
    """Generate HMAC-SHA256 signed viewer token bound to thread_id + account_id (§H5)."""
    payload = {
        "tid": thread_id,
        "acc": account_id,
        "exp": int(time.time()) + ttl_seconds,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    b64_raw = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    b64_sig = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{b64_raw}.{b64_sig}"


def verify_viewer_token(
    token: str, secret: str = VIEWER_SECRET_DEFAULT
) -> tuple[str, str]:
    """Verify HMAC signature and expiration. Returns (thread_id, account_id)."""
    parts = token.split(".")
    if len(parts) != 2:
        raise ValueError("Malformed viewer token structure")

    raw_b64 = parts[0] + "=" * (-len(parts[0]) % 4)
    sig_b64 = parts[1] + "=" * (-len(parts[1]) % 4)

    try:
        raw = base64.urlsafe_b64decode(raw_b64)
        sig = base64.urlsafe_b64decode(sig_b64)
    except Exception as e:
        raise ValueError(f"Base64 decoding failed: {e}")

    expected_sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("Invalid viewer token signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"JSON decoding failed: {e}")

    if time.time() > payload.get("exp", 0):
        raise ValueError("Viewer token expired")

    tid = payload.get("tid")
    acc = payload.get("acc")
    if not tid or not acc:
        raise ValueError("Missing tid or acc in viewer token payload")

    return tid, acc


def create_viewer_router(
    store: PgStore, secret: str = VIEWER_SECRET_DEFAULT
) -> APIRouter:
    """Create router for HTML viewer and raw .md download endpoints."""
    router = APIRouter()

    @router.get("/v/{token}", response_class=HTMLResponse)
    async def view_thread(token: str):
        try:
            thread_id, account_id = verify_viewer_token(token, secret=secret)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=f"Invalid viewer token: {e}")

        # Fetch thread and turns from DB
        async with store.pool.acquire() as conn:
            acc_uuid = await store.resolve_account_uuid(conn, account_id)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)", str(acc_uuid)
                )
                t = await conn.fetchrow(
                    """SELECT id, title, slug, created_at, updated_at, open_turn, paused
                       FROM threads WHERE id = $1 AND account_id = $2""",
                    thread_id,
                    acc_uuid,
                )
                if not t:
                    raise HTTPException(status_code=404, detail="Thread not found")

                turns = await conn.fetch(
                    """SELECT n, role, body, fidelity, chars, recovered, created_at
                       FROM turns WHERE thread_id = $1
                       ORDER BY n ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END ASC""",
                    thread_id,
                )

        title = html.escape(t["title"] or "Untitled Thread")
        created_str = t["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
        updated_str = t["updated_at"].strftime("%Y-%m-%d %H:%M:%S UTC")

        turn_elements = []
        for row in turns:
            role = html.escape(row["role"].capitalize())
            role_class = "user" if row["role"] == "user" else "assistant"
            fidelity = _RANK_TO_FIDELITY.get(row["fidelity"], 4).value
            body_escaped = html.escape(row["body"])
            turn_elements.append(f"""
            <div class="turn-card {role_class}">
                <div class="turn-header">
                    <span class="role-badge {role_class}">{role}</span>
                    <span class="turn-meta">Turn #{row['n']} • {fidelity} • {row['chars']} chars</span>
                </div>
                <div class="turn-body"><pre>{body_escaped}</pre></div>
            </div>
            """)

        turns_html = "\n".join(turn_elements)
        download_url = f"/download/{thread_id}.md?token={token}"

        page_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title} - ThreadVault Viewer</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            margin: 0;
            padding: 2rem 1rem;
            display: flex;
            justify-content: center;
        }}
        .container {{
            max-width: 860px;
            width: 100%;
        }}
        header {{
            border-bottom: 1px solid #334155;
            padding-bottom: 1.5rem;
            margin-bottom: 2rem;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-wrap: wrap;
            gap: 1rem;
        }}
        h1 {{
            margin: 0 0 0.5rem 0;
            font-size: 1.75rem;
            color: #f8fafc;
        }}
        .meta {{
            font-size: 0.875rem;
            color: #94a3b8;
        }}
        .download-btn {{
            display: inline-block;
            background: #2563eb;
            color: #fff;
            padding: 0.5rem 1rem;
            border-radius: 0.375rem;
            text-decoration: none;
            font-weight: 500;
            font-size: 0.875rem;
            transition: background 0.15s;
        }}
        .download-btn:hover {{
            background: #1d4ed8;
        }}
        .turn-card {{
            background: #1e293b;
            border-radius: 0.5rem;
            margin-bottom: 1.25rem;
            border: 1px solid #334155;
            overflow: hidden;
        }}
        .turn-card.user {{
            border-left: 4px solid #38bdf8;
        }}
        .turn-card.assistant {{
            border-left: 4px solid #a855f7;
        }}
        .turn-header {{
            background: #0f172a;
            padding: 0.6rem 1rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #334155;
        }}
        .role-badge {{
            font-weight: 600;
            font-size: 0.8125rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .role-badge.user {{ color: #38bdf8; }}
        .role-badge.assistant {{ color: #a855f7; }}
        .turn-meta {{
            font-size: 0.75rem;
            color: #64748b;
        }}
        .turn-body pre {{
            margin: 0;
            padding: 1rem;
            white-space: pre-wrap;
            word-break: break-word;
            font-family: inherit;
            font-size: 0.9375rem;
            line-height: 1.5;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>{title}</h1>
                <div class="meta">Thread ID: <code>{thread_id}</code> | Created: {created_str} | Updated: {updated_str}</div>
            </div>
            <a href="{download_url}" class="download-btn">Download .md</a>
        </header>
        <main>
            {turns_html}
        </main>
    </div>
</body>
</html>"""
        return HTMLResponse(content=page_html)

    @router.get("/download/{thread_id}.md")
    async def download_thread_markdown(
        thread_id: str,
        token: Optional[str] = Query(None),
        request: Request = None,
    ):
        target_account: Optional[str] = None
        if token:
            try:
                tok_tid, tok_acc = verify_viewer_token(token, secret=secret)
                if tok_tid != thread_id:
                    raise HTTPException(
                        status_code=403,
                        detail="Token thread ID mismatch: token cannot be reused across threads",
                    )
                target_account = tok_acc
            except ValueError as e:
                raise HTTPException(status_code=403, detail=f"Invalid token: {e}")
        else:
            acc = current_account_id.get()
            if not acc or acc == "default":
                # Check authorization header
                raise HTTPException(
                    status_code=401, detail="Authentication required for download"
                )
            target_account = acc

        try:
            md_content = await render_thread_markdown(
                store=store,
                account_id=target_account,
                thread_id=thread_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error("Error rendering thread markdown for download: %s", e)
            raise HTTPException(status_code=500, detail="Failed to render markdown")

        return Response(
            content=md_content,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{thread_id}.md"'},
        )

    return router
