"""Gap tracking per thread (§5).

Tracks missing turns, controls the once-only reporting rule (§I-6),
and manages gap lifecycle: open → recovered | lost.

Gaps are reported in vault_save_turn results. Once reported,
they are not reported again (requested=True). After 24 hours
unreported and unrecovered gaps are marked lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class GapState(str, Enum):
    """Lifecycle state of a gap."""
    OPEN = "open"           # Detected, not yet recovered
    RECOVERED = "recovered" # Filled by backfill
    LOST = "lost"           # Too old to recover (outside context window)


@dataclass
class GapEntry:
    """Single gap record."""
    n: int
    first_seen: datetime
    requested: bool = False     # True once reported in a missing list
    state: GapState = GapState.OPEN


class GapTracker:
    """Per-thread gap lifecycle manager.

    Thread-safe within a single event loop (access serialised by
    the writer's per-file lock).
    """

    def __init__(self, lost_hours: int = 24) -> None:
        self._lost_hours = lost_hours
        self._threads: dict[str, dict[int, GapEntry]] = {}

    def _get_gaps(self, thread_id: str) -> dict[int, GapEntry]:
        if thread_id not in self._threads:
            self._threads[thread_id] = {}
        return self._threads[thread_id]

    def register_gaps(
        self,
        thread_id: str,
        gap_ns: list[int],
        now: datetime | None = None,
    ) -> None:
        """Register newly detected gaps (idempotent — skips existing)."""
        now = now or datetime.now(timezone.utc)
        gaps = self._get_gaps(thread_id)
        for n in gap_ns:
            if n not in gaps:
                gaps[n] = GapEntry(n=n, first_seen=now)

    def report_missing(
        self,
        thread_id: str,
        limit: int = 10,
    ) -> list[int]:
        """Return unreported gap numbers, up to limit (§I-6).

        Sets requested=True on each returned gap so it won't be
        reported again. Gaps beyond the limit are immediately marked lost.

        Returns:
            List of gap n values to include in the result's 'missing' field.
        """
        gaps = self._get_gaps(thread_id)
        unreported = sorted(
            [g for g in gaps.values() if g.state == GapState.OPEN and not g.requested],
            key=lambda g: g.n,
        )

        # Report up to limit (cap 10, older ones marked lost per D2)
        if len(unreported) > limit:
            older = unreported[:-limit]
            for g in older:
                g.state = GapState.LOST
            to_report = unreported[-limit:]
        else:
            to_report = unreported

        for g in to_report:
            g.requested = True

        return [g.n for g in to_report]

    def recover(self, thread_id: str, n: int) -> None:
        """Mark a gap as recovered (filled by backfill)."""
        gaps = self._get_gaps(thread_id)
        if n in gaps:
            gaps[n].state = GapState.RECOVERED

    def mark_requested(self, thread_id: str, n: int) -> None:
        """Mark a gap as already handled so it is never reported (restore, not_logged)."""
        gaps = self._get_gaps(thread_id)
        if n in gaps:
            gaps[n].requested = True

    def mark_lost(self, thread_id: str, n: int) -> None:
        """Explicitly mark a gap as lost."""
        gaps = self._get_gaps(thread_id)
        if n in gaps:
            gaps[n].state = GapState.LOST

    def expire_old(
        self,
        thread_id: str,
        now: datetime | None = None,
    ) -> list[int]:
        """Mark gaps older than lost_hours as lost. Returns newly lost n values."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self._lost_hours)
        gaps = self._get_gaps(thread_id)
        newly_lost = []
        for g in gaps.values():
            if g.state == GapState.OPEN and g.first_seen < cutoff:
                g.state = GapState.LOST
                newly_lost.append(g.n)
        return newly_lost

    def get_summary(self, thread_id: str) -> dict:
        """Summary stats for vault_stats."""
        gaps = self._get_gaps(thread_id)
        open_gaps = [g.n for g in gaps.values() if g.state == GapState.OPEN]
        recovered = [g.n for g in gaps.values() if g.state == GapState.RECOVERED]
        lost = [g.n for g in gaps.values() if g.state == GapState.LOST]
        return {
            "gaps_open": sorted(open_gaps),
            "gaps_recovered": sorted(recovered),
            "gaps_lost": sorted(lost),
            "gaps_open_count": len(open_gaps),
            "gaps_recovered_count": len(recovered),
            "gaps_lost_count": len(lost),
        }

    def all_gap_ns(self, thread_id: str) -> list[int]:
        """All known gap numbers for front matter."""
        gaps = self._get_gaps(thread_id)
        return sorted(gaps.keys())
