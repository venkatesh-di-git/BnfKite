"""
replay/compare.py — score a replay run against the live alert log.

THIS EXISTS BECAUSE THE NUMBERS THAT DROVE A SPEC REVISION CAME FROM A THROWAWAY
SCRIPT. A tick-model switch was justified by "OHLC reproduces 10 of 13 on 14 Aug",
which was an alert COUNT, not a match count; scored properly the same run is 3 of
13. The script that produced the original figure no longer existed by the time the
claim was questioned. Every number in the spec now has to come from here.

THE MATCH RULE IS FIXED IN ADVANCE AND APPLIED IDENTICALLY TO EVERY MODE:

    same direction, same setup, |price| <= 2.0pt, |time| <= 60s,
    greedy nearest-in-time, one-to-one

One-to-one matters: without it one replay alert can claim several live alerts and
inflate the score. Live alerts before the live process actually started are
excluded from BOTH sides — replay always runs from 09:15, and on 11/12/13 Aug the
live session started later, so counting that window would charge replay for alerts
live had no chance to emit.

THE CEILING IS SECONDARY AND LABELLED AS SUCH. It drops price and setup, keeping
direction within 90s, and answers a different question: did replay produce an alert
there AT ALL. The gap between the two is not slack to be reclaimed — a replay
alert's price can only ever be one of the four O/H/L/C vertices, so on a 20-50
point bar it cannot agree with a live tick price to 2 points at ANY minute-
resolution tick model. Loosening the strict rule to close that gap would mean
setting the bar after seeing the result.

See Markdowns/replay_engine_spec_v3.md §0a/§0b.
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import config
from engine import engine_version
from replay.driver import replay_day
from replay.ticks import BUILDERS, CLOSE_ONLY_MODE, OHLC_MODE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _live_dir() -> str:
    """`vm-csv/` on the workstation, `csv/` on the VM — the same log either way.
    See replay.cooldown_study.live_log_path for why this resolves rather than
    shipping the VM a copy of its own alert log."""
    pulled = os.path.join(ROOT, "vm-csv")
    if os.path.exists(os.path.join(pulled, "alert_log.csv")):
        return pulled
    return os.path.join(ROOT, "csv")


LIVE_DIR = _live_dir()

PRICE_TOLERANCE = 2.0
TIME_TOLERANCE = timedelta(seconds=60)
CEILING_TOLERANCE = timedelta(seconds=90)


def _naive(t: datetime) -> datetime:
    return t.replace(tzinfo=None) if t.tzinfo else t


def load_live_alerts(day: date, live_dir: str = LIVE_DIR) -> List[dict]:
    path = os.path.join(live_dir, "alert_log.csv")
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["timestamp"][:10] != day.isoformat() or r["type"] != "Trading":
                continue
            out.append({"t": _naive(datetime.fromisoformat(r["timestamp"])),
                        "direction": (r["direction"] or "").lower(),
                        "setup": r["setup"] or "",
                        "price": float(r["current_price"]),
                        "grade": r["grade"] or "",
                        "version": r["engine_version"]})
    return sorted(out, key=lambda a: a["t"])


def live_versions(days: List[date], live_dir: str = LIVE_DIR) -> set:
    out = set()
    for d in days:
        out |= {a["version"] for a in load_live_alerts(d, live_dir)}
    return out


def version_warning(days: List[date], live_dir: str = LIVE_DIR) -> Optional[str]:
    """A reproduction score is only meaningful while the live log and the running
    engine are the same engine.

    The live log is a fixed historical record; the engine moves. Once a rule change
    ships, replay is being scored against alerts a DIFFERENT engine produced, and
    the score drops for the entirely legitimate reason that the rules changed — not
    because fidelity got worse. The flip cooldown did exactly this: it withheld
    09:56:44 B Long on 14 Aug, which had been one of the three strict matches, so
    the day fell from 3/13 to 2/13 while nothing about replay got worse.

    Warned rather than refused, unlike golden.diff(): the comparison is still worth
    running, it just must not be quoted as a fidelity figure without this caveat.
    """
    live = live_versions(days, live_dir)
    current = engine_version()
    if not live or live == {current}:
        return None
    return (f"VERSION MISMATCH — live log was produced by {sorted(live)}, this "
            f"engine is {current}.\n  Differences below include the deliberate "
            f"effects of every rule change since, not just replay fidelity.\n"
            f"  Do NOT quote these as the §0b reproduction score.")


def live_session_start(day: date, live_dir: str = LIVE_DIR) -> Optional[datetime]:
    """When the live process actually seeded, from run_manifest.csv.

    Returns None if the day is not in the manifest, in which case no window
    restriction is applied and the caller should treat the comparison as unbounded.
    """
    path = os.path.join(live_dir, "run_manifest.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["timestamp"][:10] == day.isoformat():
                return _naive(datetime.fromisoformat(r["timestamp"]))
    return None


def replay_alerts_as_rows(result) -> List[dict]:
    out = []
    for a in result.alerts:
        if a.type != "Trading":
            continue
        out.append({"t": _naive(a.timestamp), "direction": (a.direction or "").lower(),
                    "setup": a.setup or "", "price": float(a.current_price),
                    "grade": a.grade or ""})
    return sorted(out, key=lambda a: a["t"])


def _greedy(live: List[dict], replay: List[dict], tolerance: timedelta,
            strict: bool) -> Tuple[List[Tuple[dict, dict]], List[dict], List[dict]]:
    used: set = set()
    pairs = []
    for lv in live:
        best, best_i = None, None
        for i, rp in enumerate(replay):
            if i in used or rp["direction"] != lv["direction"]:
                continue
            if strict and rp["setup"] != lv["setup"]:
                continue
            if strict and abs(rp["price"] - lv["price"]) > PRICE_TOLERANCE:
                continue
            gap = abs(rp["t"] - lv["t"])
            if gap > tolerance:
                continue
            if best is None or gap < best:
                best, best_i = gap, i
        if best_i is not None:
            used.add(best_i)
            pairs.append((lv, replay[best_i]))
    matched_live = {id(p[0]) for p in pairs}
    return (pairs,
            [lv for lv in live if id(lv) not in matched_live],
            [rp for i, rp in enumerate(replay) if i not in used])


def match(live: List[dict], replay: List[dict]):
    """The binding rule: direction + setup + price + time."""
    return _greedy(live, replay, TIME_TOLERANCE, strict=True)


def ceiling(live: List[dict], replay: List[dict]):
    """SECONDARY. Direction + time only — 'did an alert happen here at all'."""
    return _greedy(live, replay, CEILING_TOLERANCE, strict=False)


def compare_day(tradingsymbol: str, day: date, modes: List[str],
                live_dir: str = LIVE_DIR) -> dict:
    start = live_session_start(day, live_dir)
    live = [a for a in load_live_alerts(day, live_dir)
            if start is None or a["t"] >= start]
    row = {"day": day, "live_start": start, "live": live, "modes": {}}
    for mode in modes:
        result = replay_day(tradingsymbol, day, write_signal_log=False, tick_mode=mode)
        rep = [a for a in replay_alerts_as_rows(result)
               if start is None or a["t"] >= start]
        pairs, missed, extra = match(live, rep)
        c_pairs, _, _ = ceiling(live, rep)
        row["modes"][mode] = {"fired": len(rep), "pairs": pairs, "missed": missed,
                              "extra": extra, "ceiling": len(c_pairs),
                              "ticks": result.ticks, "result": result}
    return row


def _print_day(row: dict, verbose: bool) -> None:
    start = row["live_start"]
    print(f"\n{'='*78}\n{row['day']}   live window from "
          f"{start.strftime('%H:%M:%S') if start else '(unbounded)'}"
          f"   live alerts: {len(row['live'])}\n{'='*78}")
    for mode, m in row["modes"].items():
        print(f"\n  {mode:30} {m['ticks']:5} ticks -> {m['fired']:2} alerts   "
              f"strict {len(m['pairs'])}/{len(row['live'])}   "
              f"ceiling {m['ceiling']}/{len(row['live'])}")
        if not verbose:
            continue
        for lv, rp in m["pairs"]:
            print(f"      MATCH  live {lv['t']:%H:%M:%S} {lv['grade']:2} "
                  f"{lv['direction']:5} {lv['setup']:8} {lv['price']:>9}  <-  "
                  f"replay {rp['t']:%H:%M:%S} {rp['grade']:2} {rp['price']:>9}  "
                  f"({abs((rp['t']-lv['t']).total_seconds()):.0f}s, "
                  f"{abs(rp['price']-lv['price']):.1f}pt)")
        for lv in m["missed"]:
            print(f"      MISS   live {lv['t']:%H:%M:%S} {lv['grade']:2} "
                  f"{lv['direction']:5} {lv['setup']:8} {lv['price']:>9}")
        for rp in m["extra"]:
            print(f"      EXTRA  repl {rp['t']:%H:%M:%S} {rp['grade']:2} "
                  f"{rp['direction']:5} {rp['setup']:8} {rp['price']:>9}")


def _print_summary(rows: List[dict], modes: List[str]) -> None:
    print(f"\n\n{'='*78}\nSUMMARY   strict = same direction, same setup, "
          f"<={PRICE_TOLERANCE:.0f}pt, <={int(TIME_TOLERANCE.total_seconds())}s"
          f"\n          ceiling = direction only, "
          f"<={int(CEILING_TOLERANCE.total_seconds())}s  (SECONDARY)\n{'='*78}")
    header = f"{'day':12} {'live':>5}"
    for mode in modes:
        header += f"   {mode[:22]:>22}"
    print(header)
    totals = {m: [0, 0] for m in modes}
    live_total = 0
    for r in rows:
        line = f"{str(r['day']):12} {len(r['live']):5}"
        live_total += len(r["live"])
        for mode in modes:
            m = r["modes"][mode]
            totals[mode][0] += len(m["pairs"])
            totals[mode][1] += m["ceiling"]
            line += f"   {len(m['pairs']):>9}/{len(r['live']):<3}{m['ceiling']:>5}/{len(r['live']):<3}"
        print(line)
    line = f"{'TOTAL':12} {live_total:5}"
    for mode in modes:
        line += f"   {totals[mode][0]:>9}/{live_total:<3}{totals[mode][1]:>5}/{live_total:<3}"
    print(line)
    print(f"\n{'mode':32} {'strict':>10} {'ceiling':>10}")
    for mode in modes:
        s, c = totals[mode]
        print(f"{mode:32} {s/live_total:>9.0%} {c/live_total:>10.0%}"
              if live_total else f"{mode:32}        n/a        n/a")


def blockers_at(result, when: datetime, direction: str) -> Dict[str, List[str]]:
    """Every gate blocking `direction` at the snapshot nearest `when`, per setup.

    Reads result.nearmiss, which records ALL failing gates rather than only
    single-blocker bars. That completeness is the point: the single-blocker view
    reported rejection ONCE on the run where rejection had blocked 11 of 13 live
    alerts, because it fails together with pullback. A correlated pair is invisible
    to that view, and a correlated pair is exactly what this function looks for.
    """
    rows = [r for r in result.nearmiss if r["direction"] == direction]
    if not rows:
        return {}
    target = min({r["timestamp"] for r in rows},
                 key=lambda ts: abs(_naive(datetime.fromisoformat(ts)) - when))
    out: Dict[str, List[str]] = {}
    for r in rows:
        if r["timestamp"] == target:
            out.setdefault(r["setup"], []).append(r["blocking_gate"])
    return {k: sorted(v) for k, v in out.items()}


def dump_blockers(rows: List[dict], mode: str) -> None:
    """Phase 1 step 5. For every live alert replay did NOT produce at all, print the
    full failing-gate set at that instant. Report only — no thresholds are changed
    on the basis of it."""
    print(f"\n\n{'='*78}\nUNREPRODUCED LIVE ALERTS — ALL BLOCKING GATES  [{mode}]\n"
          f"{'='*78}\nLive alerts with NO replay alert of the same direction within "
          f"{int(CEILING_TOLERANCE.total_seconds())}s.\n")
    from collections import Counter
    per_gate, per_setup_size, total = Counter(), Counter(), 0
    for row in rows:
        m = row["modes"][mode]
        _, unreproduced, _ = ceiling(row["live"], replay_alerts_as_rows(m["result"]))
        unreproduced = [lv for lv in unreproduced
                        if row["live_start"] is None or lv["t"] >= row["live_start"]]
        if not unreproduced:
            continue
        print(f"  {row['day']}  ({len(unreproduced)} of {len(row['live'])})")
        for lv in unreproduced:
            total += 1
            blocked = blockers_at(m["result"], lv["t"], lv["direction"])
            print(f"    live {lv['t']:%H:%M:%S} {lv['grade']:2} {lv['direction']:5} "
                  f"{lv['setup']:8} {lv['price']:>9}")
            for setup, gates in sorted(blocked.items()):
                per_setup_size[len(gates)] += 1
                for g in gates:
                    per_gate[g] += 1
                print(f"        {setup:10} blocked by: {', '.join(gates)}")
            if not blocked:
                print("        (no blocked setup recorded at that snapshot)")
        print()
    print(f"  {total} unreproduced live alerts")
    print(f"  gate appearances : {dict(per_gate.most_common())}")
    print(f"  blockers per setup: {dict(sorted(per_setup_size.items()))}")
    print("\n  A gate near the total, appearing mostly alongside others, is a "
          "correlated\n  blocker — the shape the single-blocker metric cannot see.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Score replay output against the live alert log.")
    p.add_argument("--days", nargs="+", required=True, help="YYYY-MM-DD ...")
    p.add_argument("--symbol", default="BANKNIFTY26AUGFUT")
    p.add_argument("--modes", nargs="+", default=[OHLC_MODE, CLOSE_ONLY_MODE],
                   choices=sorted(BUILDERS))
    p.add_argument("--verbose", action="store_true",
                   help="per-alert MATCH / MISS / EXTRA lines")
    p.add_argument("--blockers", action="store_true",
                   help="dump every failing gate for live alerts replay never produced")
    a = p.parse_args(argv)

    if not config.DRY_RUN:
        raise RuntimeError("compare requires BN_DRY_RUN=1; it drives replay_day()")

    days = [date.fromisoformat(d) for d in a.days]
    warning = version_warning(days)
    if warning:
        print(f"\n{'!'*78}\n  {warning}\n{'!'*78}")
    rows = [compare_day(a.symbol, d, a.modes) for d in days]
    for row in rows:
        _print_day(row, a.verbose)
    _print_summary(rows, a.modes)
    if warning:
        print(f"\n{'!'*78}\n  {warning}\n{'!'*78}")
    if a.blockers:
        dump_blockers(rows, a.modes[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
