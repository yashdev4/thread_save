"""Telemetry event logging (§7).

Appends one JSON line per tool call to vault/_index/events.jsonl.
Local only, never synced. Rotated at 10 MB.

Event fields:
  ts, tool, thread_id, n, mode, binding, chunk, gap_size,
  missing_reported, recovered, fidelity, bytes, latency_ms,
  ok, code, model_hint, nudge
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("thread_save.telemetry")


class EventLogger:
    """Append-only JSON Lines event logger."""

    def __init__(self, events_path: Path | Any, max_bytes: int = 10_000_000):
        if hasattr(events_path, "events_path"):
            self._path = events_path.events_path
            self._max_bytes = getattr(events_path, "events_max_bytes", max_bytes)
        else:
            self._path = Path(events_path)
            self._max_bytes = max_bytes

    def _rotate_if_needed(self) -> None:
        """Rotate the events file if it exceeds max_bytes."""
        try:
            if self._path.exists() and self._path.stat().st_size > self._max_bytes:
                rotated = self._path.with_suffix(".jsonl.1")
                # Keep only one rotation
                if rotated.exists():
                    rotated.unlink()
                self._path.rename(rotated)
        except OSError as e:
            logger.warning("Event rotation failed: %s", e)

    def log(
        self,
        tool: str,
        thread_id: str | None = None,
        n: int | None = None,
        mode: str = "turn_start",
        binding: str = "unknown",
        chunk: bool = False,
        gap_size: int = 0,
        missing_reported: int = 0,
        recovered: int = 0,
        fidelity: str = "verbatim",
        content_bytes: int = 0,
        latency_ms: float = 0.0,
        ok: bool = True,
        code: str = "",
        model_hint: str = "",
        nudge: str = "off",
        **extra: Any,
    ) -> None:
        """Append a single event line.

        Never raises — failures are logged to stderr and swallowed.
        """
        try:
            self._rotate_if_needed()
            self._path.parent.mkdir(parents=True, exist_ok=True)

            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": tool,
                "thread_id": thread_id,
                "n": n,
                "mode": mode,
                "binding": binding,
                "chunk": chunk,
                "gap_size": gap_size,
                "missing_reported": missing_reported,
                "recovered": recovered,
                "fidelity": fidelity,
                "bytes": content_bytes,
                "latency_ms": round(latency_ms, 2),
                "ok": ok,
                "code": code,
                "model_hint": model_hint,
                "nudge": nudge,
            }
            if extra:
                event.update(extra)

            line = json.dumps(event, ensure_ascii=False) + "\n"

            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)

        except Exception as e:
            logger.warning("Event logging failed: %s", e)


class LatencyTimer:
    """Context manager for measuring tool call latency."""

    def __init__(self) -> None:
        self.start_ns: int = 0
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "LatencyTimer":
        self.start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, *_: Any) -> None:
        elapsed_ns = time.perf_counter_ns() - self.start_ns
        self.elapsed_ms = elapsed_ns / 1_000_000
