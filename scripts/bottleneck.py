#!/usr/bin/env python3
"""Bottleneck analysis for scribea pipelines.

Reads scribe.db.events to compute:
  - end-to-end session wall time
  - per-stage in-work latency (from meta.latency_ms)
  - stage-to-stage gap (from event_time deltas; conflates Ductile dispatch + plugin work)
  - per-stage sub-phase timings (from meta.timings.* when populated)
  - aggregated p50 / p95 / p99 latency per stage across N recent sessions

Usage:
    scripts/bottleneck.py                       # last 10 sessions, summary table
    scripts/bottleneck.py --session <id>        # detailed timing for one session
    scripts/bottleneck.py --agg 50              # p50/p95/p99 over last 50 sessions
    scripts/bottleneck.py --db <path>           # override DB path
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = "~/.local/state/scribea/scribe.db"


def parse_iso(s: str) -> datetime:
    # SQLite stores RFC3339Nano; Python's fromisoformat handles up to microseconds.
    # Trim to microseconds + replace trailing Z with +00:00.
    s = s.replace("Z", "+00:00")
    if "." in s and "+" in s:
        head, tz = s.rsplit("+", 1)
        if "." in head:
            base, frac = head.split(".")
            head = base + "." + frac[:6]  # microseconds
        s = head + "+" + tz
    return datetime.fromisoformat(s)


def session_events(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT event_type, event_time, meta FROM events "
        "WHERE session_id = ? ORDER BY event_time, event_id",
        (session_id,),
    ).fetchall()
    out: list[dict] = []
    for et, et_time, meta in rows:
        try:
            m = json.loads(meta) if meta else {}
        except json.JSONDecodeError:
            m = {}
        out.append({
            "event_type": et,
            "event_time": et_time,
            "worker": m.get("worker"),
            "worker_version": m.get("worker_version"),
            "latency_ms": m.get("latency_ms"),
            "model": m.get("model"),
            "timings": m.get("timings") or {},
            "tokens_in": m.get("tokens_in"),
            "tokens_out": m.get("tokens_out"),
        })
    return out


def stage_label(e: dict) -> str:
    """Compact label for a row of output: worker name + (model) suffix when present."""
    w = e.get("worker") or "ingress"
    if e.get("model") and e["model"] != "stub":
        return f"{w} ({e['model']})"
    return w


def cmd_detail(conn: sqlite3.Connection, session_id: str) -> int:
    events = session_events(conn, session_id)
    if not events:
        print(f"no events for session {session_id}", file=sys.stderr)
        return 1
    t0 = parse_iso(events[0]["event_time"])
    tlast = parse_iso(events[-1]["event_time"])
    total_wall = int((tlast - t0).total_seconds() * 1000)

    state_row = conn.execute(
        "SELECT state, template_id, started_at FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    state, template, started = state_row or (None, None, None)

    print(f"session    {session_id}")
    print(f"template   {template}")
    print(f"started    {started}")
    print(f"state      {state}")
    print(f"wall time  {total_wall} ms across {len(events)} events")
    print()
    print(f"{'+gap ms':>9}  {'event':38}  {'worker (model)':36}  {'work ms':>8}")
    print("-" * 100)
    prev_t = None
    sum_work = 0
    for e in events:
        et = parse_iso(e["event_time"])
        gap = int((et - prev_t).total_seconds() * 1000) if prev_t else 0
        latency = e["latency_ms"]
        latency_disp = f"{latency:>8}" if latency is not None else "       —"
        print(f"{gap:>9}  {e['event_type']:38}  {stage_label(e):36}  {latency_disp}")
        if latency is not None:
            sum_work += latency
        for phase, ms in e["timings"].items():
            print(f"{'':>9}  ↳ {phase:60} {ms:>8} ms")
        prev_t = et
    print("-" * 100)
    if sum_work > 0:
        ductile_gap = total_wall - sum_work
        ratio = ductile_gap / total_wall if total_wall else 0
        print(f"sum of plugin work_ms : {sum_work} ms")
        print(f"wall − plugin work    : {ductile_gap} ms ({ratio*100:.1f}% in dispatch/queue/network)")
    return 0


def cmd_latest(conn: sqlite3.Connection, limit: int) -> int:
    sessions = conn.execute(
        "SELECT session_id, template_id, state FROM sessions "
        "ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if not sessions:
        print("no sessions in scribe.db", file=sys.stderr)
        return 1
    header = f"{'session_id':38} {'template':18} {'state':12} {'events':>7} {'wall ms':>10}"
    print(header)
    print("-" * len(header))
    for sid, template, state in sessions:
        events = session_events(conn, sid)
        if not events:
            continue
        t0 = parse_iso(events[0]["event_time"])
        tlast = parse_iso(events[-1]["event_time"])
        wall = int((tlast - t0).total_seconds() * 1000)
        print(f"{sid[:36]:38} {template[:16]:18} {state[:10]:12} {len(events):>7} {wall:>10}")
    return 0


def cmd_aggregate(conn: sqlite3.Connection, n: int) -> int:
    sessions = conn.execute(
        "SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT ?", (n,),
    ).fetchall()
    if not sessions:
        print("no sessions in scribe.db", file=sys.stderr)
        return 1

    work_by_worker: dict[str, list[int]] = {}
    phase_by_worker: dict[str, dict[str, list[int]]] = {}
    for (sid,) in sessions:
        for e in session_events(conn, sid):
            w = e["worker"]
            if not w or e["latency_ms"] is None:
                continue
            work_by_worker.setdefault(w, []).append(e["latency_ms"])
            for phase, ms in e["timings"].items():
                phase_by_worker.setdefault(w, {}).setdefault(phase, []).append(ms)

    def pct(values: list[int], p: float) -> float:
        if not values:
            return 0.0
        values_sorted = sorted(values)
        if len(values_sorted) == 1:
            return values_sorted[0]
        k = max(0, min(len(values_sorted) - 1, int(round(p * (len(values_sorted) - 1)))))
        return values_sorted[k]

    print(f"aggregate latency over last {len(sessions)} sessions  ─  latency_ms (total per stage)")
    print(f"{'worker':28} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}")
    print("-" * 76)
    for w, vals in sorted(work_by_worker.items()):
        print(f"{w:28} {len(vals):>5} {pct(vals,0.50):>8.0f} {pct(vals,0.95):>8.0f} "
              f"{pct(vals,0.99):>8.0f} {max(vals):>8}")
    print()
    print("sub-phase breakdown (p50 / p95):")
    for w in sorted(phase_by_worker.keys()):
        print(f"  {w}:")
        for phase, vals in sorted(phase_by_worker[w].items()):
            print(f"    {phase:32} n={len(vals):>3}  p50={pct(vals,0.50):>6.0f}  p95={pct(vals,0.95):>6.0f}  max={max(vals):>6}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"scribe.db path (default: {DEFAULT_DB})")
    ap.add_argument("--session", help="session_id for detailed report")
    ap.add_argument("--agg", type=int, metavar="N", help="aggregate p50/p95/p99 over last N sessions")
    ap.add_argument("--limit", type=int, default=10, help="latest-summary row count (default 10)")
    args = ap.parse_args()

    db_path = Path(os.path.expanduser(args.db))
    if not db_path.exists():
        print(f"db not found: {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        if args.session:
            return cmd_detail(conn, args.session)
        if args.agg:
            return cmd_aggregate(conn, args.agg)
        return cmd_latest(conn, args.limit)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
