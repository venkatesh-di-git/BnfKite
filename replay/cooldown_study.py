"""
replay/cooldown_study.py — what a flip cooldown would actually have suppressed.

PURE LOG REPLAY. Reads the live alert log and re-runs the suppression rule over
rows that were already delivered. No engine, no candles, no tick model, no
engine_version movement. It answers one question before any code is written:
how many alerts does a given cooldown remove, and how many direction flips does it
actually catch.

WHY THIS EXISTS. The cooldown was justified by "4 of 56 alerts suppressed, 11
flips", from a dataset described at the time as not present in the repo. That is
the same provenance as the "10 of 13" figure which, when finally re-derived from
repo code, turned out to be 3 of 13. The log IS present — 56 Trading rows plus one
Error across 11-14 Aug — so the figure was checkable, and was checked before
anything was built. Outcome: 4-of-56 reproduced exactly; "11 flips" did not (there
are 18), and the 12-minute duration turned out to sit one minute past a step edge.

RULE MODELLED (spec §11 step 10):
  - a candidate is suppressed if it arrives within COOLDOWN of the last DELIVERED
    alert; `_last_alert` advances only on delivery, so a suppressed alert does not
    extend its own cooldown
  - grades in ESCAPING_GRADES are never suppressed
  - state resets on date change

TWO SCOPES, because "flip cooldown" is ambiguous and the difference is large:
  flip   - only suppress when the direction CHANGES (the name's reading)
  any    - suppress any alert inside the window (the broader reading)
Both are reported. Picking one is a decision for the spec, not for this file.
"""

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def live_log_path() -> str:
    """`vm-csv/` on the workstation, `csv/` on the VM.

    On Windows `vm-csv/` is a pulled copy of the VM's `csv/`; on the VM that same
    log IS `csv/`. Resolving both means one code path and one set of tests run
    identically on either machine, instead of the VM needing a copy of its own
    logs shipped back to it.
    """
    pulled = os.path.join(ROOT, "vm-csv", "alert_log.csv")
    return pulled if os.path.exists(pulled) else os.path.join(ROOT, "csv", "alert_log.csv")


LIVE_LOG = live_log_path()

DEFAULT_MINUTES = 12
ESCAPING_GRADES = ("A", "A+")
SWEEP = (5, 10, 12, 15, 20, 30)

# The window every figure in the spec was derived over. Pinned, because the live
# log GROWS: without a bound, "56 alerts, 18 flips" silently becomes a different
# claim the first time a new session lands, and a regression test written against
# it fails for the one reason that is not a regression.
CORPUS_START, CORPUS_END = "2026-08-11", "2026-08-14"


def load(path: Optional[str] = None, start: Optional[str] = None,
         end: Optional[str] = None) -> List[dict]:
    out = []
    with open(path or live_log_path(), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["type"] != "Trading":
                continue
            day = r["timestamp"][:10]
            if (start and day < start) or (end and day > end):
                continue
            out.append({"t": datetime.fromisoformat(r["timestamp"]).replace(tzinfo=None),
                        "day": day,
                        "direction": r["direction"] or "",
                        "grade": r["grade"] or "",
                        "setup": r["setup"] or "",
                        "price": r["current_price"],
                        "version": r["engine_version"]})
    return sorted(out, key=lambda a: a["t"])


def load_corpus() -> List[dict]:
    """The pinned 11-14 Aug window — what every published figure refers to."""
    return load(start=CORPUS_START, end=CORPUS_END)


def flips(alerts: List[dict]) -> List[dict]:
    """Alerts whose direction differs from the previous alert THAT SAME DAY."""
    out, prev_day, prev_dir = [], None, None
    for a in alerts:
        if a["day"] != prev_day:
            prev_day, prev_dir = a["day"], a["direction"]
            continue
        if a["direction"] != prev_dir:
            out.append(a)
        prev_dir = a["direction"]
    return out


def apply_cooldown(alerts: List[dict], minutes: int, scope: str,
                   escaping: Tuple[str, ...] = ESCAPING_GRADES) -> List[dict]:
    """Returns the suppressed alerts. `scope` is 'flip' or 'any'."""
    window = timedelta(minutes=minutes)
    suppressed = []
    last: Optional[dict] = None
    day = None
    for a in alerts:
        if a["day"] != day:            # reset on date change
            day, last = a["day"], None
        if last is not None and a["t"] - last["t"] < window:
            is_flip = a["direction"] != last["direction"]
            in_scope = is_flip if scope == "flip" else True
            if in_scope and a["grade"] not in escaping:
                suppressed.append({**a, "since_last": a["t"] - last["t"],
                                   "blocked_by": last, "was_flip": is_flip})
                continue               # not delivered -> `last` does not advance
        last = a
    return suppressed


def _fmt(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"{s//60:2}m{s%60:02}s"


def report(alerts: List[dict], minutes: int, escaping: Tuple[str, ...]) -> None:
    all_flips = flips(alerts)
    days = sorted({a["day"] for a in alerts})
    print(f"LIVE LOG   {len(alerts)} Trading alerts, {len(days)} days "
          f"({days[0]} .. {days[-1]})")
    print(f"           directions {dict(Counter(a['direction'] for a in alerts))}")
    print(f"           versions   {sorted({a['version'] for a in alerts})}")
    print(f"           grades     {dict(Counter(a['grade'] for a in alerts).most_common())}")
    print(f"           same-day direction flips: {len(all_flips)}")
    print(f"\nRULE       cooldown {minutes}m, escaping grades {escaping}, "
          f"reset on date change\n")

    for scope in ("flip", "any"):
        sup = apply_cooldown(alerts, minutes, scope, escaping)
        caught = [s for s in sup if s["was_flip"]]
        print(f"  scope={scope:5}  suppressed {len(sup):2}/{len(alerts)} alerts   "
              f"flips caught {len(caught)}/{len(all_flips)}")
        for s in sup:
            print(f"      {s['day']} {s['t']:%H:%M:%S} {s['grade']:2} "
                  f"{s['direction']:5} {s['setup']:9} {s['price']:>9}   "
                  f"{_fmt(s['since_last'])} after "
                  f"{s['blocked_by']['grade']:2} {s['blocked_by']['direction']:5}"
                  f"{'   FLIP' if s['was_flip'] else ''}")
        print()

    print(f"\n{'':12}{'scope=flip':>26}{'scope=any':>26}")
    print(f"{'cooldown':12}{'suppressed':>13}{'flips':>13}{'suppressed':>13}{'flips':>13}")
    for m in SWEEP:
        row = f"{str(m)+'m':12}"
        for scope in ("flip", "any"):
            sup = apply_cooldown(alerts, m, scope, escaping)
            row += f"{len(sup):>10}/{len(alerts):<3}{len(([s for s in sup if s['was_flip']])):>9}/{len(all_flips):<3}"
        print(row + ("   <- proposed" if m == minutes else ""))

    print(f"\n{'':12}{'without grade escaping (all grades suppressible)':>60}")
    print(f"{'cooldown':12}{'scope=flip':>26}{'scope=any':>26}")
    for m in SWEEP:
        row = f"{str(m)+'m':12}"
        for scope in ("flip", "any"):
            sup = apply_cooldown(alerts, m, scope, escaping=())
            row += f"{len(sup):>17}/{len(alerts):<8}"
        print(row)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Re-derive what a flip cooldown suppresses, from the live log.")
    p.add_argument("--minutes", type=int, default=DEFAULT_MINUTES)
    p.add_argument("--log", default=None)
    p.add_argument("--from", dest="start", default=CORPUS_START,
                   help="YYYY-MM-DD; defaults to the pinned corpus start")
    p.add_argument("--to", dest="end", default=CORPUS_END,
                   help="YYYY-MM-DD; defaults to the pinned corpus end")
    p.add_argument("--no-escaping", action="store_true",
                   help="suppress A and A+ too")
    a = p.parse_args(argv)
    report(load(a.log, a.start, a.end), a.minutes,
           () if a.no_escaping else ESCAPING_GRADES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
