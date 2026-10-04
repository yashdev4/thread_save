"""ThreadVault report aggregator (§7).

CLI: python -m thread_save.report

Reads events.jsonl and aggregates:
- coverage = non-stub user turns / total user turns
- recovery = recovered gaps / all gaps
- split rate = threads created by anchor miss / all threads
- dup no-op rate = no-op upserts / calls
- p95 latency
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from thread_save.config import load_config


def load_events(events_path: Path) -> list[dict]:
    """Load all events from JSONL file."""
    events = []
    if not events_path.exists():
        return events
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def aggregate(events: list[dict]) -> dict:
    """Aggregate events into summary metrics."""
    if not events:
        return {"error": "No events found"}

    # Group by (model_hint, mode, nudge)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in events:
        key = (
            e.get("model_hint", "unknown"),
            e.get("mode", "turn_start"),
            e.get("nudge", "off"),
        )
        groups[key].append(e)

    results = {}
    for key, group_events in groups.items():
        model, mode, nudge = key
        save_events = [e for e in group_events if e["tool"] == "vault_save_turn"]
        backfill_events = [e for e in group_events if e["tool"] == "vault_backfill"]

        total_calls = len(save_events)
        ok_calls = sum(1 for e in save_events if e.get("ok", True))
        failed_calls = total_calls - ok_calls

        # Binding types
        new_threads = sum(1 for e in save_events if e.get("binding") == "new")
        id_bound = sum(1 for e in save_events if e.get("binding") == "id")
        anchor_bound = sum(1 for e in save_events if e.get("binding") == "anchor")

        # Split rate
        total_threads = new_threads + id_bound + anchor_bound
        split_rate = round(
            (new_threads / total_threads * 100) if total_threads > 0 else 0, 1,
        )

        # Gaps
        total_missing = sum(e.get("missing_reported", 0) for e in save_events)
        total_recovered = sum(e.get("recovered", 0) for e in backfill_events)

        # Latency
        latencies = [e.get("latency_ms", 0) for e in group_events if e.get("latency_ms")]
        p50 = round(statistics.median(latencies), 1) if latencies else 0
        p95 = round(
            sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0, 1,
        )

        results[f"{model}/{mode}/{nudge}"] = {
            "total_calls": total_calls,
            "ok_calls": ok_calls,
            "failed_calls": failed_calls,
            "new_threads": new_threads,
            "id_bound": id_bound,
            "anchor_bound": anchor_bound,
            "split_rate_pct": split_rate,
            "gaps_reported": total_missing,
            "gaps_recovered": total_recovered,
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
        }

    return results


def print_report(results: dict) -> None:
    """Pretty-print the aggregated report."""
    print("=" * 70)
    print("ThreadVault Telemetry Report")
    print("=" * 70)

    if "error" in results:
        print(f"\n  {results['error']}")
        return

    for group_key, metrics in results.items():
        print(f"\n  {group_key}")
        print(f"  {'─' * 60}")
        for k, v in metrics.items():
            print(f"    {k:30s} {v}")
    print()


def main():
    config = load_config()
    events = load_events(config.events_path)
    results = aggregate(events)
    print_report(results)


if __name__ == "__main__":
    main()
