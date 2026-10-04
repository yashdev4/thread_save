"""Markdown formatting for thread files (§3).

Rebuilt for reliability layer:
- Turn blocks use (n, role) slot structure
- Stub bodies for missed turns
- Recovered marker on backfilled turns
- Open fidelity for unclosed last reply
- Snippet extraction for vault_find (§I-9: ≤200 chars, never full turns)
- HTML comment delimiters with opening AND closing tags
"""

from __future__ import annotations

import re
import yaml
import secrets
from datetime import datetime
from typing import Optional

from thread_save.models import Attachment, Fidelity, ThreadMeta, TurnData
from thread_save.security.idempotency import compute_content_hash_short


class QuotedDumper(yaml.SafeDumper):
    pass


def _repr_str(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


QuotedDumper.add_representer(str, _repr_str)


def format_front_matter(meta: ThreadMeta) -> str:
    """Serialize ThreadMeta to YAML front matter block using safe_dump with explicit string quotes."""
    if not hasattr(meta, "nonce") or not meta.nonce:
        meta.nonce = secrets.token_hex(2)
        
    data = {
        "schema_version": 2,
        "thread_id": meta.thread_id,
        "title": meta.title,
        "slug": meta.slug,
        "account": meta.account,
        "client": meta.client,
        "model": meta.model,
        "created": meta.created.isoformat(),
        "updated": meta.updated.isoformat(),
        "page": meta.page,
        "prev": meta.prev,
        "next": meta.next,
        "turn_count": meta.turn_count,
        "turn_range": [meta.turn_range[0], meta.turn_range[1]],
        "bytes": meta.bytes,
        "gaps": meta.gaps,
        "redacted": meta.redacted,
        "tags": meta.tags,
        "open_turn": meta.open_turn,
        "paused": meta.paused,
        "nonce": meta.nonce
    }
    if getattr(meta, "continues", None):
        data["continues"] = meta.continues
    yaml_str = yaml.dump(data, Dumper=QuotedDumper, sort_keys=False)
    return "---\n" + yaml_str + "---\n"


def parse_front_matter(content: str) -> tuple[ThreadMeta, str]:
    """Parse YAML front matter block and return (ThreadMeta, remaining_content)."""
    if not content.startswith("---"):
        raise ValueError("Content does not start with front matter marker '---'")
    parts = content.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError("Unclosed front matter block")
    yaml_text = parts[1]
    remaining = parts[2]
    data = yaml.safe_load(yaml_text)
    if "created" in data and isinstance(data["created"], str):
        data["created"] = datetime.fromisoformat(data["created"])
    if "updated" in data and isinstance(data["updated"], str):
        data["updated"] = datetime.fromisoformat(data["updated"])
    meta = ThreadMeta(**data)
    return meta, remaining


def parse_turns(content: str, nonce: str = "") -> list[TurnData]:
    """Parse turn blocks from markdown content, honouring only matching nonce delimiters (W6)."""
    turns: list[TurnData] = []
    
    if nonce:
        open_pattern = re.compile(rf"<!-- turn ([^>]*\bnonce={re.escape(nonce)}\b[^>]*) -->")
        close_pattern = re.compile(rf"<!-- /turn [^>]*\bnonce={re.escape(nonce)}\b[^>]* -->")
    else:
        open_pattern = re.compile(r"<!-- turn (.*?) -->")
        close_pattern = re.compile(r"<!-- /turn (.*?) -->")

    open_matches = list(open_pattern.finditer(content))
    close_matches = list(close_pattern.finditer(content))

    for open_m in open_matches:
        meta_str = open_m.group(1)
        close_m = next((c for c in close_matches if c.start() >= open_m.end()), None)
        if not close_m:
            continue
            
        meta_dict = {}
        for token in re.findall(r'(\w+)=(?:"([^"]*)"|(\S+))', meta_str):
            k = token[0]
            v = token[1] if token[1] else token[2]
            meta_dict[k] = v

        turn_index = int(meta_dict.get("i", 0))
        role = meta_dict.get("role", "user")
        fid_val = meta_dict.get("fidelity", "verbatim")
        fidelity = Fidelity(fid_val) if fid_val in [f.value for f in Fidelity] else Fidelity.VERBATIM
        model = meta_dict.get("model", "")
        turn_key = meta_dict.get("turn_key")
        anchor = meta_dict.get("anchor", "")
        recovered = meta_dict.get("recovered") == "true"
        char_count = int(meta_dict.get("chars", 0))
        content_hash = meta_dict.get("hash", "")
        ts_str = meta_dict.get("ts")
        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now().astimezone()

        raw_body_section = content[open_m.end():close_m.start()]
        lines = raw_body_section.strip().split("\n")
        body_lines = []
        skip_heading = True
        for line in lines:
            if skip_heading and line.startswith("## "):
                skip_heading = False
                continue
            if line.startswith("[") and line.endswith("— not archived]"):
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()

        turns.append(TurnData(
            turn_index=turn_index,
            role=role,
            body=body,
            timestamp=ts,
            model=model,
            fidelity=fidelity,
            char_count=char_count,
            content_hash=content_hash,
            recovered=recovered,
            anchor=anchor,
            turn_key=turn_key,
        ))

    return turns


def parse_page(content: str) -> tuple[ThreadMeta, list[TurnData]]:
    """Parse complete page markdown into ThreadMeta and TurnData list."""
    meta, remaining = parse_front_matter(content)
    turns = parse_turns(remaining, nonce=meta.nonce)
    return meta, turns


def format_turn(turn: TurnData, nonce: str = "") -> str:
    """Serialize a single turn slot with HTML comment delimiters.

    Opening and closing comments for robust parsing (§3.2).
    """
    body_hash = compute_content_hash_short(turn.body) if turn.body else "00000000"
    ts = turn.timestamp.isoformat()

    parts: list[str] = []

    # Build the opening comment metadata
    meta_parts = [f"i={turn.turn_index}", f"role={turn.role}"]

    if turn.role == "user":
        meta_parts.append(f"ts={ts}")
    else:
        if turn.model:
            meta_parts.append(f"model={turn.model}")

    meta_parts.append(f"fidelity={turn.fidelity.value}")
    meta_parts.append(f"chars={turn.char_count}")
    meta_parts.append(f"hash={body_hash}")

    if turn.recovered:
        meta_parts.append("recovered=true")

    if turn.attachments:
        meta_parts.append(f"attachments={len(turn.attachments)}")

    if turn.anchor and turn.role == "user":
        meta_parts.append(f'anchor="{turn.anchor[:40]}"')

    if turn.turn_key and turn.role == "user":
        meta_parts.append(f'turn_key={turn.turn_key}')

    if nonce: meta_parts.append(f"nonce={nonce}")
    parts.append(f"<!-- turn {' '.join(meta_parts)} -->")

    # Heading
    heading = "## User" if turn.role == "user" else "## Claude"
    parts.append(heading)
    parts.append("")

    # Attachment markers (before body)
    for att in turn.attachments:
        parts.append(f'[{att.type}: "{att.title}" — not archived]')
        parts.append("")

    # Body
    parts.append(turn.body)

    # Closing comment
    parts.append(f"<!-- /turn i={turn.turn_index} nonce={nonce} -->" if nonce else f"<!-- /turn i={turn.turn_index} -->")

    return "\n".join(parts)


def format_stub_body(anchor: str = "") -> str:
    """Format a stub body for a missed turn (§4.2)."""
    if anchor:
        return f'[not archived — began: "{anchor}"]'
    return "[not archived]"


def format_open_body() -> str:
    """Format an open body for the unclosed last reply (§4.3)."""
    return "[response pending — will be filled on next turn]"


def format_gap_marker(after_n: int, missing_count: int) -> str:
    """Format a gap marker (§3.4)."""
    return f"<!-- gap after i={after_n}: {missing_count} turn(s) not archived -->"


def format_thread_header(title: str) -> str:
    """Format the H1 title."""
    return f"# {title}"


def format_page_footer(next_filename: str) -> str:
    """Pagination footer."""
    return f"\n---\n\n--> Continued in [{next_filename}](./{next_filename})\n"


def format_page_header(prev_filename: str) -> str:
    """Pagination header."""
    return f"<-- Continued from [{prev_filename}](./{prev_filename})\n\n"


def format_new_page(
    meta: ThreadMeta,
    prev_filename: Optional[str] = None,
) -> str:
    """Format a complete new page file."""
    parts = [format_front_matter(meta)]
    parts.append("")
    parts.append(format_thread_header(meta.title))
    parts.append("")
    if prev_filename:
        parts.append(format_page_header(prev_filename))
    return "\n".join(parts)


def format_turn_separator() -> str:
    """Horizontal rule between turns."""
    return "\n---\n"


def extract_snippet(body: str, max_chars: int = 200) -> str:
    """Extract a preview snippet from a turn body (§I-9).

    Strips markdown formatting, limits to max_chars.
    Never returns full turn content — only a preview.
    """
    # Strip code blocks
    text = re.sub(r"```[\s\S]*?```", "[code]", body)
    # Strip inline code
    text = re.sub(r"`[^`]+`", "[code]", text)
    # Strip HTML comments
    text = re.sub(r"<!--.*?-->", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Truncate
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."
    return text
