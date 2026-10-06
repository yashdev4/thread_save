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


def format_export_status(export_status: dict) -> list[str]:
    """Format GitHub export status including conflicts and dead letters."""
    lines = []
    conflicts = export_status.get("conflicts", [])
    dead_letters = export_status.get("dead_letters", [])

    if conflicts or dead_letters:
        lines.append("\n  GitHub Archive Export Status")
        lines.append("  " + "─" * 60)

    if conflicts:
        lines.append(f"    Active Conflicts (Human Edits Detected): {len(conflicts)}")
        for c in conflicts:
            lines.append(f"      - {c}")

    if dead_letters:
        lines.append(f"    Dead-Lettered Threads (Push Protection): {len(dead_letters)}")
        for dl in dead_letters:
            lines.append(f"      - {dl}")

    return lines


def print_report(results: dict, export_status: Optional[dict] = None) -> None:
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

    if export_status:
        for line in format_export_status(export_status):
            print(line)

    print()


# ── PostgreSQL Deployed Store Coverage Diagnostics (Milestone D3) ────────

FIDELITY_NAMES = {
    0: "open",
    1: "stub",
    2: "truncated",
    3: "abridged",
    4: "reported",
    5: "verbatim",
}


async def generate_postgres_thread_report(conn, thread_id: str) -> dict:
    """Generate per-turn breakdown and totals for a thread stored in PostgreSQL."""
    t_row = await conn.fetchrow(
        "SELECT id, title, created_at, updated_at FROM threads WHERE id = $1",
        thread_id,
    )
    if not t_row:
        return {"error": f"Thread {thread_id} not found in database"}

    rows = await conn.fetch(
        """
        SELECT n, role, fidelity, recovered, chars, created_at
        FROM turns
        WHERE thread_id = $1
        ORDER BY n ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END ASC
        """,
        thread_id,
    )

    turns_report = []
    first_save_turn = None
    total_slots = len(rows)
    recorded_slots = 0
    recovered_slots = 0

    user_turns_total = 0
    user_turns_recorded = 0

    for r in rows:
        n = r["n"]
        role = r["role"]
        fid_val = r["fidelity"]
        fid_name = FIDELITY_NAMES.get(fid_val, f"unknown({fid_val})")
        recovered = r["recovered"]
        is_stub = (fid_val == 1)

        status = "stub" if is_stub else "recorded"
        if is_stub:
            save_type = "stub"
        elif recovered:
            save_type = "backfill"
        else:
            save_type = "routine save"
            if first_save_turn is None:
                first_save_turn = n

        if not is_stub:
            recorded_slots += 1
        if recovered:
            recovered_slots += 1

        if role == "user":
            user_turns_total += 1
            if not is_stub:
                user_turns_recorded += 1

        turns_report.append({
            "n": n,
            "role": role,
            "status": status,
            "save_type": save_type,
            "fidelity": fid_name,
            "recovered": recovered,
        })

    coverage_pct = round((recorded_slots / total_slots * 100), 1) if total_slots > 0 else 0.0
    user_coverage_pct = round((user_turns_recorded / user_turns_total * 100), 1) if user_turns_total > 0 else 0.0
    recovered_pct = round((recovered_slots / total_slots * 100), 1) if total_slots > 0 else 0.0

    return {
        "thread_id": thread_id,
        "title": t_row["title"],
        "created_at": t_row["created_at"].isoformat() if t_row["created_at"] else "",
        "updated_at": t_row["updated_at"].isoformat() if t_row["updated_at"] else "",
        "turns": turns_report,
        "totals": {
            "total_slots": total_slots,
            "recorded_slots": recorded_slots,
            "recovered_slots": recovered_slots,
            "coverage_pct": coverage_pct,
            "user_coverage_pct": user_coverage_pct,
            "recovered_pct": recovered_pct,
            "first_save_turn": first_save_turn,
        },
    }


def print_postgres_report(reports: list[dict]) -> None:
    """Format and print coverage diagnostics report for Postgres threads."""
    for rep in reports:
        if "error" in rep:
            print(f"\n[ERROR] {rep['error']}")
            continue
        print("=" * 70)
        print(f"Thread Diagnostic Report: {rep['thread_id']}")
        print(f"Title: {rep['title']}")
        print("=" * 70)
        print(f"{'Turn':<6} {'Role':<11} {'Status':<11} {'Save Type':<15} {'Fidelity':<12}")
        print("-" * 70)
        for t in rep["turns"]:
            print(f"{t['n']:<6} {t['role']:<11} {t['status']:<11} {t['save_type']:<15} {t['fidelity']:<12}")
        print("-" * 70)
        tot = rep["totals"]
        print("Totals:")
        print(f"  Coverage       : {tot['coverage_pct']}% ({tot['recorded_slots']}/{tot['total_slots']} recorded)")
        print(f"  User Coverage  : {tot['user_coverage_pct']}%")
        print(f"  Recovered      : {tot['recovered_pct']}% ({tot['recovered_slots']}/{tot['total_slots']} recovered)")
        print(f"  First Save Turn: Turn {tot['first_save_turn'] if tot['first_save_turn'] is not None else 'None'}")
        print()


async def run_postgres_diagnostics(
    dsn: str,
    thread_id: Optional[str] = None,
    latest: int = 5,
) -> list[dict]:
    """Execute coverage diagnostics against Postgres database."""
    import asyncpg
    conn = await asyncpg.connect(dsn)
    try:
        if thread_id:
            rep = await generate_postgres_thread_report(conn, thread_id)
            return [rep]
        else:
            rows = await conn.fetch(
                "SELECT id FROM threads ORDER BY updated_at DESC LIMIT $1",
                latest,
            )
            reports = []
            for r in rows:
                rep = await generate_postgres_thread_report(conn, r["id"])
                reports.append(rep)
            return reports
    finally:
        await conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ThreadVault Coverage and Telemetry Reporter")
    parser.add_argument("--dsn", help="PostgreSQL connection DSN for deployed store diagnostics")
    parser.add_argument("--thread", help="Specific thread ID to diagnose")
    parser.add_argument("--latest", type=int, default=5, help="Number of latest threads to diagnose (default 5)")
    args = parser.parse_args()

    if args.dsn:
        import asyncio
        reports = asyncio.run(
            run_postgres_diagnostics(args.dsn, thread_id=args.thread, latest=args.latest)
        )
        print_postgres_report(reports)
    else:
        config = load_config()
        events = load_events(config.events_path)
        results = aggregate(events)
        print_report(results)


if __name__ == "__main__":
    main()


