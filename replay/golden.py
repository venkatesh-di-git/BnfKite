"""
replay/golden.py — freeze one replayed session and detect when it changes.

WHAT THIS IS, AND WHAT IT IS NOT. It is a CHANGE detector: it proves the engine
did not drift. It is NOT a correctness detector and cannot become one — replay
reproduces 9% of live alerts strictly and 54% at the ceiling, so a green golden
file says "the engine still does what it did on 16 Aug", never "the engine is
right". Reading a passing golden file as validation is precisely how the fidelity
gap (spec §0b) would get quietly abandoned.

Its actual job is to make the next engine change auditable: ship the flip cooldown,
re-run, and the golden file MUST fail with exactly the alerts the cooldown
suppresses and nothing else. That test only exists because the freeze happened
first.

`id` IS EXCLUDED FROM THE FROZEN ROWS. AlertRecord.id is a fresh uuid4 per alert
(alert_engine.py:141), so raw output is never byte-identical between runs. The
spec's "same day replayed twice is byte-identical" cannot hold while that is true,
and making it hold would mean editing alert_engine.py — a hashed module — which
would move engine_version for a change that alters no rule. Every other field is
deterministic, verified by replaying twice in one process.
"""

import argparse
import csv
import os
import sys
from datetime import date
from typing import List, Optional

import config
from engine import engine_version
from replay.driver import replay_day
from replay.ticks import TICK_MODE

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

# Deliberately not AlertRecord.id — see the module docstring.
FIELDS = ["timestamp", "type", "direction", "grade", "confidence", "setup",
          "current_price", "reason_list", "engine_version", "tick_mode"]

GOLDEN_SYMBOL = "BANKNIFTY26AUGFUT"
GOLDEN_DAY = date(2026, 8, 14)
# 14 Aug is the only session at this engine version that ran clean: seeded at
# 09:15:00.000362 with 36 prior-session bars, no restarts, no token expiry, ran to
# the close. 11 and 12 Aug started 09:20:01, and 13 Aug seeded at 09:58:30 after a
# token expiry cost it the first 43 minutes.


def golden_path(day: date = GOLDEN_DAY, version: Optional[str] = None,
                tick_mode: str = TICK_MODE) -> str:
    version = version or engine_version()
    return os.path.join(GOLDEN_DIR, f"replay_{day:%Y-%m-%d}_{version}_{tick_mode}.csv")


def rows_for(day: date = GOLDEN_DAY, symbol: str = GOLDEN_SYMBOL,
             tick_mode: str = TICK_MODE) -> List[dict]:
    if not config.DRY_RUN:
        raise RuntimeError("golden file work requires BN_DRY_RUN=1")
    version = engine_version()
    result = replay_day(symbol, day, write_signal_log=False, tick_mode=tick_mode)
    return [{"timestamp": a.timestamp.isoformat(), "type": a.type,
             "direction": a.direction or "", "grade": a.grade or "",
             "confidence": "" if a.confidence is None else a.confidence,
             "setup": a.setup or "",
             "current_price": "" if a.current_price is None else a.current_price,
             "reason_list": "|".join(a.reason_list or []),
             "engine_version": version, "tick_mode": result.tick_mode}
            for a in result.alerts]


def read_golden(path: str) -> List[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def freeze(day: date = GOLDEN_DAY, symbol: str = GOLDEN_SYMBOL,
           tick_mode: str = TICK_MODE, overwrite: bool = False) -> str:
    path = golden_path(day, tick_mode=tick_mode)
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(
            f"{path} already exists. A golden file is frozen ONCE per "
            f"(day, engine_version, tick_mode); re-freezing over a failing one "
            f"destroys the only record of what the engine used to do. Pass "
            f"--overwrite only if you are deliberately rebaselining.")
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    rows = rows_for(day, symbol, tick_mode)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)
    return path


def diff(day: date = GOLDEN_DAY, symbol: str = GOLDEN_SYMBOL,
         tick_mode: str = TICK_MODE, against: Optional[str] = None):
    """(only_in_golden, only_in_fresh). Empty pair means the engine has not moved.

    `against` names an EARLIER engine_version to compare the current engine
    against — the one deliberate cross-version comparison the design otherwise
    forbids. Normally the golden path is keyed on the current version, so a
    versioned change makes comparison impossible rather than misleading (§10).
    That default is right, and it also blocks the single case where a
    cross-version diff is the entire point: ship a rule change, then prove the
    frozen file fails with EXACTLY the alerts that change should remove.

    So it has to be asked for by name. Passing a version you have not thought
    about will silently compare two unrelated engines and call the difference a
    result — which is the failure mode the keying exists to prevent.

    `engine_version` is ignored when comparing across versions; it differs on
    every row by construction and would swamp the real diff.
    """
    path = golden_path(day, version=against, tick_mode=tick_mode)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no golden file at {path}; run `freeze` first")
    fields = [f for f in FIELDS if f != "engine_version"] if against else FIELDS
    frozen = [tuple(str(r[k]) for k in fields) for r in read_golden(path)]
    fresh = [tuple(str(r[k]) for k in fields) for r in rows_for(day, symbol, tick_mode)]
    fset, rset = set(frozen), set(fresh)
    return ([r for r in frozen if r not in rset], [r for r in fresh if r not in fset])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Freeze or check the replay golden file.")
    p.add_argument("command", choices=["freeze", "check"])
    p.add_argument("--date", default=GOLDEN_DAY.isoformat())
    p.add_argument("--symbol", default=GOLDEN_SYMBOL)
    p.add_argument("--mode", default=TICK_MODE)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--against", metavar="VERSION",
                   help="compare the current engine against an EARLIER version's "
                        "frozen file — the acceptance test for a deliberate rule change")
    a = p.parse_args(argv)
    day = date.fromisoformat(a.date)

    if a.command == "freeze":
        path = freeze(day, a.symbol, a.mode, a.overwrite)
        rows = read_golden(path)
        print(f"froze {len(rows)} alerts -> {os.path.basename(path)}")
        for r in rows:
            print(f"  {r['timestamp'][11:19]} {r['grade']:3} {r['direction']:5} "
                  f"{r['setup']:9} {r['current_price']}")
        return 0

    gone, added = diff(day, a.symbol, a.mode, against=a.against)
    if a.against:
        print(f"CROSS-VERSION diff: frozen {a.against}  ->  current {engine_version()}"
              f"   ({a.mode})\n  '-' withheld by the change, '+' newly produced by it")
    if not gone and not added:
        print(f"golden file MATCHES  ({engine_version()}, {a.mode})")
        return 0
    print(f"golden file DIFFERS  ({engine_version()}, {a.mode})")
    for r in gone:
        print(f"  -  {r[0][11:19]} {r[3]:3} {r[2]:5} {r[5]:9} {r[6]}")
    for r in added:
        print(f"  +  {r[0][11:19]} {r[3]:3} {r[2]:5} {r[5]:9} {r[6]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
