"""
replay/bt_vwap_pullback.py — VWAP-pullback backtest per
Markdowns/backtest_vwap_pullback_oct2026.md ("Exact rules"), on the candle archive.

Rebuilt 17 Oct 2026: the original claude.ai script was never committed. On
BANKNIFTY26OCTFUT 1-15 Oct it reproduces the md's A+ trade log exactly.

Part A: the frozen rules, plus the month-end extras (candidate grid, entry
latency +0/+1/+2 min, target 25/40).
Part B: the live engine replayed per day (replay/driver.py), each alert exited
at stop 20 / target 30.

Each contract is its own price series: EMAs and the volume MA run across that
contract's archived sessions and never across a roll. Trades are taken only in
the contract's front-month window, never on its first archived session (seed).

Usage: BN_DRY_RUN=1 python -m replay.bt_vwap_pullback [--no-live] [SYMBOL ...]
"""

import os
import sys
from datetime import date, datetime, time, timedelta

os.environ.setdefault("BN_DRY_RUN", "1")

from replay.data import ARCHIVE_DIR, _read, front_month_window  # noqa: E402

CONTRACTS = ["BANKNIFTY26AUGFUT", "BANKNIFTY26SEPFUT", "BANKNIFTY26OCTFUT"]
SLIP = 2.0                      # pts per side
WINDOW = (time(9, 30), time(15, 0))   # signal bar close
FORCED_EXIT = time(15, 15)


def load_days(sym):
    d = os.path.join(ARCHIVE_DIR, sym)
    out = {}
    for f in sorted(os.listdir(d)):
        if f.endswith(".json.gz"):
            rows = _read(os.path.join(d, f))
            for r in rows:
                r["t"] = datetime.fromisoformat(r["date"]).replace(tzinfo=None)
            out[f[:10]] = rows
    return out


def tradable(sym, days):
    start, end = front_month_window(sym)
    names = sorted(days)
    return {d for d in names[1:] if start <= date.fromisoformat(d) <= end}


def agg(rows, mins):
    bars, cur = [], None
    for r in rows:
        t = r["t"]
        m = (t.hour * 60 + t.minute - 555) // mins
        st = t.replace(hour=0, minute=0) + timedelta(minutes=555 + m * mins)
        if cur is None or cur["t"] != st:
            cur = dict(t=st, end=st + timedelta(minutes=mins), o=r["open"], h=r["high"],
                       l=r["low"], c=r["close"], v=r["volume"])
            bars.append(cur)
        else:
            cur["h"] = max(cur["h"], r["high"])
            cur["l"] = min(cur["l"], r["low"])
            cur["c"] = r["close"]
            cur["v"] += r["volume"]
    return bars


def ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else e + k * (v - e)
        out.append(e)
    return out


def simulate(m1, i0, side, stop, tgt):
    """Enter at the open of m1[i0]. Stop and target in the same bar count as a stop."""
    entry = m1[i0]["open"]
    exit_t = datetime.combine(m1[i0]["t"].date(), FORCED_EXIT)
    for r in m1[i0:]:
        if r["t"] >= exit_t:
            return (r["open"] - entry) * side - 2 * SLIP, "Time", r["t"]
        hit_s = r["low"] <= entry - stop if side > 0 else r["high"] >= entry + stop
        hit_t = r["high"] >= entry + tgt if side > 0 else r["low"] <= entry - tgt
        if hit_s:
            return -stop - 2 * SLIP, "Stop", r["t"]
        if hit_t:
            return tgt - 2 * SLIP, "Target", r["t"]
    r = m1[-1]
    return (r["close"] - entry) * side - 2 * SLIP, "EOD", r["t"]


def stats(tr):
    if not tr:
        return " 0 trades"
    p = [x["pts"] for x in tr]
    w = sum(1 for x in p if x > 0)
    eq = pk = dd = 0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return (f"{len(p):3d} trades, {100 * w / len(p):3.0f}% won, avg {sum(p) / len(p):+6.1f}, "
            f"total {sum(p):+6.0f}, maxDD {dd:+5.0f}")


# ---------------- Part A ----------------

def signals(days, trade_days):
    names = sorted(days)
    m1 = [r for d in names for r in days[d]]
    b5 = [b for d in names for b in agg(days[d], 5)]
    b15 = [b for d in names for b in agg(days[d], 15)]
    e10 = ema([b["c"] for b in b5], 10)
    e20 = ema([b["c"] for b in b15], 20)
    vwap = {}       # session VWAP (typical price x volume) keyed by 1-min bar end
    for d in names:
        pv = vv = 0
        for r in days[d]:
            pv += (r["high"] + r["low"] + r["close"]) / 3 * r["volume"]
            vv += r["volume"]
            vwap[r["t"] + timedelta(minutes=1)] = pv / vv if vv else r["close"]
    sigs, k = [], 0
    for i in range(1, len(b5)):
        b, p = b5[i], b5[i - 1]
        while k + 1 < len(b15) and b15[k + 1]["end"] <= b["end"]:
            k += 1
        if f"{b['t']:%Y-%m-%d}" not in trade_days or p["t"].date() != b["t"].date():
            continue
        if not WINDOW[0] <= b["end"].time() <= WINDOW[1]:
            continue
        v_now, v_prev = vwap[b["end"]], vwap[p["end"]]
        side = 0
        if p["c"] > v_prev and b["l"] <= v_now and b["c"] > v_now and b["c"] > b["o"]:
            side = 1
        if p["c"] < v_prev and b["h"] >= v_now and b["c"] < v_now and b["c"] < b["o"]:
            side = -1
        if not side:
            continue
        prev20 = [x["v"] for x in b5[max(0, i - 20):i]]
        sigs.append(dict(t=b["t"], ct=b["end"], side=side,
                         slope15=e20[k] - e20[k - 1] if b15[k]["end"] <= b["end"] and k else 0,
                         vr=b["v"] / (sum(prev20) / len(prev20)),
                         s5=(e10[i] - e10[i - 1]) / 5))
    return sigs, m1


def run(book, filt, stop=20, tgt=30, delay=0):
    """book: list of (sigs, m1) per contract. One position at a time."""
    out = []
    for sigs, m1 in book:
        idx = {r["t"]: n for n, r in enumerate(m1)}
        busy = None
        for s in sigs:
            if not filt(s):
                continue
            et = s["ct"] + timedelta(minutes=delay)
            if (busy and et < busy) or et not in idx:
                continue
            pts, res, xt = simulate(m1, idx[et], s["side"], stop, tgt)
            out.append(dict(s=s, entry=m1[idx[et]]["open"], pts=pts, res=res))
            busy = xt
    return out


GRADE_A = lambda s: s["slope15"] * s["side"] > 0          # noqa: E731
GRADE_AP = lambda s: GRADE_A(s) and s["vr"] > 1.0          # noqa: E731


def part_a(books):
    allb = [b for bk in books.values() for b in bk]
    print("=== PART A: frozen rules ===")
    for sym, bk in books.items():
        print(f"{sym}: A+ {stats(run(bk, GRADE_AP))} | A {stats(run(bk, GRADE_A))}")
    print("\nPooled:")
    for name, f in (("A+", GRADE_AP), ("A ", GRADE_A)):
        for st, tg in ((20, 30), (20, 25), (20, 40)):
            print(f"  {name} stop {st}/tgt {tg}: {stats(run(allb, f, st, tg))}")
    print("\nA+ trade log (stop 20 / target 30):")
    for x in run(allb, GRADE_AP):
        s = x["s"]
        print(f"  {s['t']:%d %b} {s['t'] + timedelta(minutes=4):%H:%M} "
              f"{'Long ' if s['side'] > 0 else 'Short'} {x['entry']:>9} {x['res']:6} "
              f"{x['pts']:+5.0f}   vol x{s['vr']:.2f} slope15 {s['slope15']:+.1f}")
    print("\nLatency (stop 20 / target 30):")
    for dl in (0, 1, 2):
        print(f"  +{dl} min  A+: {stats(run(allb, GRADE_AP, delay=dl))}")
        print(f"          A : {stats(run(allb, GRADE_A, delay=dl))}")
    print("\nCandidate grid (stop 20 / target 30):")
    trends = (("None", lambda s: True), ("15m EMA20 slope", GRADE_A),
              ("5m EMA10 slope sign", lambda s: s["s5"] * s["side"] > 0),
              ("5m EMA10 slope > 4.3", lambda s: s["s5"] * s["side"] > 4.3))
    vols = (("no vol", lambda s: True), ("vol>=1.0", lambda s: s["vr"] >= 1.0),
            ("vol>=1.3", lambda s: s["vr"] >= 1.3))
    for tn, tf in trends:
        for vn, vf in vols:
            print(f"  {tn:22} {vn:9}: "
                  f"{stats(run(allb, lambda s, tf=tf, vf=vf: tf(s) and vf(s)))}")


# ---------------- Part B ----------------

def part_b(contracts):
    from replay.driver import replay_day
    print("\n=== PART B: live engine replay -> stop 20 / target 30 ===")
    trades, fired = [], 0
    for sym, (days, trade_days, m1) in contracts.items():
        idx = {r["t"]: n for n, r in enumerate(m1)}
        for d in sorted(trade_days):
            res = replay_day(sym, date.fromisoformat(d), write_signal_log=False)
            busy = None
            for a in res.alerts:
                if not a.direction or a.grade not in ("A+", "A", "B"):
                    continue
                fired += 1
                ts = a.timestamp.replace(tzinfo=None)
                et = ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
                if (busy and et < busy) or et not in idx or et.time() >= FORCED_EXIT:
                    continue
                side = 1 if a.direction.lower() == "long" else -1
                pts, r, xt = simulate(m1, idx[et], side, 20, 30)
                trades.append(dict(pts=pts, grade=a.grade, setup=a.setup, sym=sym))
                busy = xt
    print(f"alerts fired: {fired}; taken (one position at a time): {len(trades)}")
    print(f"  all             : {stats(trades)}")
    for g in ("A+", "A", "B"):
        print(f"  grade {g:3}       : {stats([x for x in trades if x['grade'] == g])}")
    print(f"  A/A+ (Telegram) : {stats([x for x in trades if x['grade'] in ('A', 'A+')])}")
    for st in sorted({str(x['setup']) for x in trades}):
        print(f"  setup {st:9} : {stats([x for x in trades if str(x['setup']) == st])}")
    for sym in contracts:
        print(f"  {sym}: {stats([x for x in trades if x['sym'] == sym])}")


def main(argv):
    syms = [a for a in argv if not a.startswith("--")] or CONTRACTS
    contracts, books = {}, {}
    for sym in syms:
        days = load_days(sym)
        td = tradable(sym, days)
        sigs, m1 = signals(days, td)
        contracts[sym] = (days, td, m1)
        books[sym] = [(sigs, m1)]
        print(f"{sym}: {len(td)} sessions traded, {min(td)} .. {max(td)}")
    print()
    part_a(books)
    if "--no-live" not in argv:
        part_b(contracts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
