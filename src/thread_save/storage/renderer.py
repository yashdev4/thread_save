"""Renderer: projects database rows into canonical Plan v2 Markdown (§4.1, Milestone X5)."""

from __future__ import annotations

from typing import Optional

from thread_save.models import Fidelity, ThreadMeta, TurnData
from thread_save.security.idempotency import compute_content_hash_short
from thread_save.storage.formatter import (
    format_front_matter,
    format_turn,
)
from thread_save.storage.pg_store import PgStore, _RANK_TO_FIDELITY


async def render_thread_markdown(
    store: PgStore,
    account_id: str,
    thread_id: str,
    page: int = 1,
) -> str:
    """Render canonical markdown projection for a thread page from Postgres."""
    async with store.pool.acquire() as conn:
        acc_uuid = await store.resolve_account_uuid(conn, account_id)
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.account_id', $1, true)", str(acc_uuid))
            t = await conn.fetchrow(
                """SELECT id, account_id, title, slug, created_at, updated_at,
                          open_turn, paused, max_n, current_page, current_page_turns,
                          current_page_bytes, delim
                   FROM threads WHERE id = $1 AND account_id = $2""",
                thread_id, acc_uuid
            )
            if not t:
                raise ValueError(f"Thread {thread_id} not found")

            # Fetch turns for this page
            turns_rows = await conn.fetch(
                """SELECT n, role, body, fidelity, chars, hash, recovered, created_at, page, turn_key, anchor
                   FROM turns
                   WHERE thread_id = $1 AND page = $2
                   ORDER BY n ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END ASC""",
                thread_id, page
            )

            # Build turn list
            turn_datas: list[TurnData] = []
            for r in turns_rows:
                fed = _RANK_TO_FIDELITY.get(r["fidelity"], Fidelity.VERBATIM)
                td = TurnData(
                    turn_index=r["n"],
                    role=r["role"],
                    body=r["body"],
                    timestamp=r["created_at"],
                    model="",
                    fidelity=fed,
                    char_count=r["chars"],
                    content_hash=compute_content_hash_short(r["body"]),
                    recovered=r["recovered"],
                    anchor=r["anchor"] or "",
                    turn_key=r["turn_key"],
                )
                turn_datas.append(td)

            min_n = min((td.turn_index for td in turn_datas), default=1)
            max_n = max((td.turn_index for td in turn_datas), default=0)

            # Metadata for page
            meta = ThreadMeta(
                schema_version=2,
                thread_id=thread_id,
                title=t["title"] or "Untitled Thread",
                slug=t["slug"] or "untitled",
                account=account_id,
                client="claude-desktop",
                created=t["created_at"],
                updated=t["updated_at"],
                page=page,
                turn_count=len(turn_datas),
                turn_range=[min_n, max_n],
                bytes=0,
                tags=[],
                nonce=t["delim"],
                open_turn=t["open_turn"],
                paused=t["paused"],
            )

            # Format body
            turn_blocks = [format_turn(td, nonce=t["delim"]) for td in turn_datas]
            turns_text = "\n\n".join(turn_blocks)
            if turns_text:
                turns_text = "\n\n" + turns_text + "\n"
            else:
                turns_text = "\n"

            # Compute bytes
            fm = format_front_matter(meta)
            total_bytes = len((fm + turns_text).encode("utf-8"))
            meta.bytes = total_bytes
            fm = format_front_matter(meta)

            return fm + turns_text
