"""
replay/outputs.py — the two CSVs a replay run produces.

Every row carries BOTH engine_version and tick_mode. Either one moving changes the
output, so a file that records only the first turns a tick-model change into an
unexplained diff, and a file that records neither is unreadable six weeks later.

See Markdowns/replay_engine_spec_v3.md §10.
"""

import csv
import os
from collections import Counter
from datetime import date
from typing import Iterable, List, Optional

from engine import engine_version

ALERT_FIELDS = ["id", "timestamp", "type", "direction", "grade", "confidence",
                "setup", "current_price", "reason_list", "engine_version",
                "tick_mode"]

# `snapshots` is how many 2s evaluations that (bar, setup, gate) blocker persisted
# for. Rows are collapsed to one per distinct blocker rather than written raw
# because the raw stream is change-keyed: a gate that flickers on and off produces
# far more rows than one that blocks solidly for a whole bar, so raw counts are
# biased toward UNSTABLE near-misses. Keeping the count preserves that information
# without letting it distort the totals.
NEARMISS_FIELDS = ["bar_start", "direction", "setup", "blocking_gate",
                   "blocker_count", "snapshots", "first_seen", "last_seen",
                   "engine_version", "tick_mode"]


def _write(path: str, fields: List[str], rows: Iterable[dict]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)
    return path


def _stamp(day: date, kind: str, tick_mode: str, version: str) -> str:
    return f"replay_{kind}_{day:%Y-%m-%d}_{version}_{tick_mode}.csv"


def write_alerts(out_dir: str, result, version: Optional[str] = None) -> str:
    """`alert_log.csv` columns plus tick_mode, so replay output diffs against live
    output without a column-mapping step."""
    version = version or engine_version()
    rows = []
    for a in result.alerts:
        rows.append({
            "id": a.id, "timestamp": a.timestamp.isoformat(), "type": a.type,
            "direction": a.direction, "grade": a.grade, "confidence": a.confidence,
            "setup": a.setup, "current_price": a.current_price,
            "reason_list": "|".join(a.reason_list or []),
            "engine_version": version, "tick_mode": result.tick_mode,
        })
    return _write(os.path.join(out_dir, _stamp(result.day, "alerts",
                                               result.tick_mode, version)),
                  ALERT_FIELDS, rows)


def collapse_nearmiss(records: Iterable[dict]) -> List[dict]:
    """Raw per-snapshot blocker rows -> one row per (bar, direction, setup, gate)."""
    agg = {}
    for r in records:
        key = (r["bar_start"], r["direction"], r["setup"], r["blocking_gate"])
        cur = agg.get(key)
        if cur is None:
            agg[key] = {"bar_start": r["bar_start"], "direction": r["direction"],
                        "setup": r["setup"], "blocking_gate": r["blocking_gate"],
                        "blocker_count": r["blocker_count"], "snapshots": 1,
                        "first_seen": r["timestamp"], "last_seen": r["timestamp"]}
        else:
            cur["snapshots"] += 1
            cur["last_seen"] = r["timestamp"]
            # Keep the FEWEST simultaneous blockers seen for this gate on this bar.
            # That is the moment the setup came closest to firing, which is the
            # question near-miss exists to answer.
            cur["blocker_count"] = min(cur["blocker_count"], r["blocker_count"])
    return sorted(agg.values(), key=lambda r: (str(r["bar_start"]), r["direction"],
                                               r["setup"], r["blocking_gate"]))


def write_nearmiss(out_dir: str, result, version: Optional[str] = None) -> str:
    version = version or engine_version()
    rows = collapse_nearmiss(result.nearmiss)
    for r in rows:
        r["engine_version"], r["tick_mode"] = version, result.tick_mode
    return _write(os.path.join(out_dir, _stamp(result.day, "nearmiss",
                                               result.tick_mode, version)),
                  NEARMISS_FIELDS, rows)


def blocker_summary(result, sole_blocker_only: bool = False,
                    direction: Optional[str] = None) -> Counter:
    """Distinct (bar_start, direction, setup, blocking_gate) per gate.

    NOT distinct (bar_start, gate), which the spec originally called for. That key
    SATURATES and is worth understanding before anyone reverts it: every bar
    evaluates a long setup and a short setup, at most one of which can be valid, so
    trend blocks the other on essentially every bar. Measured on 14 Aug it put
    pullback, rejection, trend and volume all at exactly 76 — the bar count — which
    ranks nothing. Keying on the setup as well as the bar restores the distinction.

    `sole_blocker_only` recovers the OLD single-blocker view as a filter. It is
    offered, and it must never be the only view: on 14 Aug close-only, rejection
    blocked 11 of the 13 live alerts and appeared in the single-blocker histogram
    ONCE, because rejection and pullback fail together when a bar has no wicks.
    Correlated gates are invisible to that filter by construction.
    """
    seen = set()
    counts = Counter()
    for r in collapse_nearmiss(result.nearmiss):
        if sole_blocker_only and r["blocker_count"] != 1:
            continue
        if direction and r["direction"] != direction:
            continue
        key = (str(r["bar_start"]), r["direction"], r["setup"], r["blocking_gate"])
        if key not in seen:
            seen.add(key)
            counts[r["blocking_gate"]] += 1
    return counts


def write_all(out_dir: str, result, version: Optional[str] = None) -> List[str]:
    return [write_alerts(out_dir, result, version),
            write_nearmiss(out_dir, result, version)]
