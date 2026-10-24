"""
replay/bt_external.py — the five "for the archive run" tests from
Markdowns/Backtestmd/backtest_external_methods_oct2026.md, on the candle archive.

Rules are frozen as written in Markdowns/Backtestmd/bt_external.py (24 Oct):
  T1  OI state of the signal bar: OI at the 5m bar's last minute vs the previous
      5m bar's, Rising > +0.05%, Falling < -0.05%, else Flat
  T5  pierce = how far the signal bar went through VWAP (VWAP - low for longs)
  T3  c3 day type: |close - VWAP| >= 75 at the 10:00 1-min bar; direction = side
      of VWAP; outcome = 15:15 open - 10:00 close, and a +/-100 race
  T6  trade number within the day (A, one position at a time), cap 2/day
  T2  TOD relative volume: 5m volume / mean of the same slot over the prior <= 10
      front-month sessions of the contract, needs >= 5
Base signals are replay/bt_vwap_pullback.py's (identical trigger): A = 15m EMA20
slope agrees, A+ = A and volume > MA20. Execution: next 1-min open, stop 20 /
target 30, 2 pts slippage each way, one position at a time per contract.

Windows. The tests were picked on 24 Sep - 23 Oct, so only data outside that is
a test:
  OOS   Aug contract (29 Jul - 25 Aug) + Sep contract up to 23 Sep
  IS    24 Sep - 15 Oct (reference only: where the leads came from)
  NEW   16 Oct onward, once archived (also in-sample for these tests: the 24 Oct
        run included 16-23 Oct)

Pass rule, fixed before the run, judged on OOS: average > 0, bootstrap 90% CI
lower bound > 0, average above the random-direction 95th percentile, and a
positive average in Aug and in Sep separately.

Usage: python -m replay.bt_external
"""

import random
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import replay.bt_vwap_pullback as bt
from replay.bt_vwap_pullback import CONTRACTS, agg, load_days, signals, tradable
from replay.data import front_month_window
from replay.study_suite import boot, boot_sessions, independent

OOS_SEP_END = "2026-09-23"
IS_END = "2026-10-15"
RNG = random.Random(11)


def window(day):
    if day <= OOS_SEP_END:
        return "OOS"
    return "IS" if day <= IS_END else "NEW"


# ---------------------------------------------------------------- data

def vwap_by_minute(rows):
    out, pv, vv = {}, 0.0, 0.0
    for r in rows:
        pv += (r["high"] + r["low"] + r["close"]) / 3 * r["volume"]
        vv += r["volume"]
        out[r["t"]] = pv / vv if vv else r["close"]
    return out


def build():
    items, series, c3 = [], {}, {}
    for sym in CONTRACTS:
        days = load_days(sym)
        td = tradable(sym, days)
        sigs, m1 = signals(days, td)
        idx = {r["t"]: n for n, r in enumerate(m1)}
        series[sym] = dict(m1=m1, idx=idx)
        by_t = {r["t"]: r for r in m1}
        vw = {}
        for d in days:
            vw.update(vwap_by_minute(days[d]))

        # TOD slot volumes, front-month sessions only
        start, end = front_month_window(sym)
        front = [d for d in sorted(days) if start <= date.fromisoformat(d) <= end]
        slot = {d: {b["t"].time(): b["v"] for b in agg(days[d], 5)} for d in front}

        for s in sigs:
            d = f"{s['ct']:%Y-%m-%d}"
            last = by_t.get(s["ct"] - timedelta(minutes=1))       # signal bar's last minute
            prev = by_t.get(s["t"] - timedelta(minutes=1))        # previous bar's last minute
            if last is None or prev is None or s["ct"] not in idx:
                continue
            ch = (last["oi"] - prev["oi"]) / prev["oi"] if prev["oi"] else 0
            oist = "Rising" if ch > 0.0005 else "Falling" if ch < -0.0005 else "Flat"
            v = vw[last["t"]]
            bar = [by_t[s["t"] + timedelta(minutes=k)] for k in range(5) if s["t"] + timedelta(minutes=k) in by_t]
            pierce = (v - min(r["low"] for r in bar)) if s["side"] > 0 else (max(r["high"] for r in bar) - v)
            prior = [x for x in front if x < d][-10:]
            vals = [slot[x][s["t"].time()] for x in prior if s["t"].time() in slot[x]]
            vol5 = sum(r["volume"] for r in bar)
            tod = vol5 / (sum(vals) / len(vals)) if len(vals) >= 5 and sum(vals) else None
            items.append(dict(s, sym=sym, day=d, win=window(d), oist=oist, pierce=pierce, tod=tod,
                              A=s["slope15"] * s["side"] > 0, Ap=bt.GRADE_AP(s)))

        for d in sorted(td):
            rows = days[d]
            r10 = next((r for r in rows if r["t"].time() == time(10, 0)), None)
            ex = next((r for r in rows if r["t"].time() >= bt.FORCED_EXIT), None)
            if r10 is None or ex is None:
                continue
            dist = r10["close"] - vw[r10["t"]]
            race = None
            dr = 1 if dist > 0 else -1
            for r in rows:
                if r["t"].time() <= time(10, 0):
                    continue
                if dr * ((r["high"] if dr > 0 else r["low"]) - r10["close"]) >= 100:
                    race = "W"
                    break
                if dr * ((r["low"] if dr > 0 else r["high"]) - r10["close"]) <= -100:
                    race = "L"
                    break
            c3[d] = dict(sym=sym, win=window(d), dist=dist, dir=dr,
                         move=dr * (ex["open"] - r10["close"]), raw=ex["open"] - r10["close"], race=race)
    return items, series, c3


# ---------------------------------------------------------------- simulation

def run_seq(series, items, delay=0, rnd=None):
    out = []
    for sym in CONTRACTS:
        m1, idx, busy = series[sym]["m1"], series[sym]["idx"], None
        for s in sorted((x for x in items if x["sym"] == sym), key=lambda x: x["ct"]):
            et = s["ct"] + timedelta(minutes=delay)
            if et not in idx or (busy and et < busy) or et.time() >= bt.FORCED_EXIT:
                continue
            side = s["side"] if rnd is None else rnd.choice((1, -1))
            pts, _res, xt = bt.simulate(m1, idx[et], side, 20, 30)
            out.append(dict(pts=pts, day=s["day"], side=side, ct=s["ct"], sym=sym, it=s))
            busy = xt
    return out


def st(tr):
    ind = independent(tr)
    if not ind:
        return None
    p = [x["pts"] for x in ind]
    lo, hi = boot_sessions(ind)
    by = defaultdict(float)
    for x in ind:
        by[x["day"]] += x["pts"]
    return dict(n=len(p), win=100 * sum(1 for v in p if v > 0) / len(p), avg=sum(p) / len(p),
                lo=lo, hi=hi, pos=sum(1 for v in by.values() if v > 0), days=len(by))


def rd_band(series, items, runs=300):
    avgs = []
    for _ in range(runs):
        tr = independent(run_seq(series, items, rnd=RNG))
        if tr:
            avgs.append(sum(x["pts"] for x in tr) / len(tr))
    avgs.sort()
    return avgs[int(0.05 * len(avgs))], avgs[int(0.95 * len(avgs)) - 1]


def short(s):
    return f"{s['avg']:+6.1f} ({s['n']:2d})" if s else "   n/a    "


def row(series, items, label, delay=0):
    tr = run_seq(series, items, delay=delay)
    o = st([x for x in tr if x["it"]["win"] == "OOS"])
    aug = st([x for x in tr if x["it"]["win"] == "OOS" and x["sym"].endswith("AUGFUT")])
    sep = st([x for x in tr if x["it"]["win"] == "OOS" and x["sym"].endswith("SEPFUT")])
    is_ = st([x for x in tr if x["it"]["win"] == "IS"])
    new = st([x for x in tr if x["it"]["win"] == "NEW"])
    if o:
        lo, hi = rd_band(series, [x for x in items if x["win"] == "OOS"])
        ok = (o["avg"] > 0 and o["lo"] is not None and o["lo"] > 0 and o["avg"] > hi
              and aug and aug["avg"] > 0 and sep and sep["avg"] > 0)
        ci = f"[{o['lo']:+5.1f},{o['hi']:+5.1f}]" if o["lo"] is not None else "[  n/a  ]"
        oos = (f"{o['n']:3d} {o['win']:3.0f}% {o['avg']:+6.1f} {ci} {o['pos']:2d}/{o['days']:2d} "
               f"rnd95 {hi:+5.1f}")
        verdict = "PASS" if ok else "fail"
    else:
        oos, verdict = f"{'0 trades':>49}", "n/a"
    print(f"  {label:<34} {oos} | Aug {short(aug)} Sep {short(sep)} | IS {short(is_)} NEW {short(new)} | {verdict}")


def header(title):
    print(f"\n=== {title}")
    print(f"  {'':<34} {'OOS (Aug + Sep to 23 Sep)':<49} | {'by contract, OOS only':<30} | "
          f"{'reference (picked here)':<28} |")
    print(f"  {'':<34} {'ind win%   avg  90% CI      sess+ random':<49} |")


# ---------------------------------------------------------------- tests

def main(argv):
    items, series, c3 = build()
    A = [x for x in items if x["A"]]
    Ap = [x for x in items if x["Ap"]]
    wins = defaultdict(set)
    for x in items:
        wins[x["win"]].add(x["day"])
    print(f"signals {len(items)}; A {len(A)}; A+ {len(Ap)}. Sessions with signals: "
          + ", ".join(f"{k} {len(v)}" for k, v in sorted(wins.items())))
    if "NEW" not in wins:
        print("NOTE: no sessions after 15 Oct in the archive yet; NEW column is empty.")

    header("BASELINE")
    row(series, A, "A (VWAP pullback + 15m slope)")
    row(series, Ap, "A+ (A + vol > MA20)")

    header("T1  OI state of the signal bar (live +/-0.05% rule)")
    for o in ("Rising", "Flat", "Falling"):
        row(series, [x for x in A if x["oist"] == o], f"A, OI {o}")
    row(series, [x for x in A if x["oist"] != "Flat"], "A, block Flat (lead)")
    row(series, [x for x in A if x["oist"] != "Flat"], "A, block Flat, +1 min", delay=1)
    row(series, [x for x in A if x["oist"] != "Falling"], "A, block Falling (live logic)")
    row(series, [x for x in Ap if x["oist"] == "Rising"], "A+ and OI Rising")

    header("T5  Pullback depth (VWAP pierce, pts)")
    for lo, hi in ((0, 10), (10, 20), (20, 1e9)):
        row(series, [x for x in Ap if lo <= x["pierce"] < hi], f"A+ pierce {lo}-{'' if hi > 1e8 else hi}")
    row(series, [x for x in Ap if x["pierce"] < 10], "A+ pierce <10, +1 min", delay=1)
    for lo, hi in ((0, 10), (10, 20), (20, 1e9)):
        row(series, [x for x in A if lo <= x["pierce"] < hi], f"A pierce {lo}-{'' if hi > 1e8 else hi}")

    header("T6  Trades per day (A, one position at a time)")
    tr = run_seq(series, A)
    seq = defaultdict(int)
    for t in tr:
        seq[t["day"]] += 1
        t["it"] = dict(t["it"], seq=seq[t["day"]])
    keyed = {(t["it"]["sym"], t["it"]["ct"]): t["it"]["seq"] for t in tr}
    for lab, f in (("trade #1 of day", lambda k: k == 1), ("trade #2 of day", lambda k: k == 2),
                   ("trade #3+ of day", lambda k: k >= 3), ("cap 2/day (lead)", lambda k: k <= 2)):
        row(series, [x for x in A if (x["sym"], x["ct"]) in keyed and f(keyed[(x["sym"], x["ct"])])], lab)

    header("T2  Time-of-day relative volume vs MA20 (signals with a TOD baseline)")
    At = [x for x in A if x["tod"] is not None]
    print(f"  A signals with a TOD baseline: {len(At)} of {len(A)}")
    row(series, At, "A (same signals)")
    row(series, [x for x in At if x["vr"] > 1.0], "A+ by MA20 > 1.0")
    row(series, [x for x in At if x["tod"] > 1.0], "A+ by TOD > 1.0")
    for lo, hi in ((0, 0.8), (0.8, 1.0), (1.0, 1.3), (1.3, 1e9)):
        row(series, [x for x in At if lo <= x["tod"] < hi], f"A, TOD {lo}-{'' if hi > 1e8 else hi}")

    print("\n=== T3  c3 day type: |close - VWAP| >= 75 at 10:00, traded to 15:15 in VWAP's direction")
    for w in ("OOS", "IS", "NEW"):
        for scope, f in (("c3 days", lambda c: abs(c["dist"]) >= 75), ("all days", lambda c: True)):
            ds = [d for d, c in sorted(c3.items()) if c["win"] == w and f(c)]
            if not ds:
                continue
            mv = [c3[d]["move"] for d in ds]
            mean = sum(mv) / len(mv)
            lo, hi = boot(mv) if len(mv) > 1 else (None, None)
            up = sum(1 for d in ds if c3[d]["raw"] > 0)
            ci = f"[{lo:+5.0f},{hi:+5.0f}]" if lo is not None else ""
            print(f"  {w:<3} {scope:<9} days {len(ds):2d}  avg move {mean:+6.0f} {ci:<14} hit "
                  f"{100 * sum(1 for m in mv if m > 0) / len(mv):3.0f}%  race W/L "
                  f"{sum(1 for d in ds if c3[d]['race'] == 'W')}/{sum(1 for d in ds if c3[d]['race'] == 'L')}"
                  f"  ({up} up / {len(ds) - up} down closes vs 10:00)")
        for sym in CONTRACTS:
            ds = [d for d, c in c3.items() if c["win"] == w and c["sym"] == sym and abs(c["dist"]) >= 75]
            if ds and w == "OOS":
                mv = [c3[d]["move"] for d in ds]
                print(f"      {sym[9:12]} c3 days {len(ds):2d}  avg {sum(mv) / len(mv):+6.0f}  "
                      f"hit {100 * sum(1 for m in mv if m > 0) / len(mv):3.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
