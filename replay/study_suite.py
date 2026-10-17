"""
replay/study_suite.py — the review list of tests, run over the whole archive.

Read-only: nothing here touches the engine, the live logs or replay_out/.

Every result is reported the same way, so the rows are comparable:
  raw   signals/alerts in the slice
  ind   independent trades: same-day, same-side signals inside CLUSTER_MIN minutes
        of the last kept one collapse to one (same-bar upgrades B>A>A+ and flips
        otherwise inflate the count)
  CI    bootstrap 90% interval of the mean (pts/trade) over the independent trades
  sess+ sessions with a positive total / sessions with any trade

Signals are evaluated independently (each gets its own hypothetical trade), not
one-position-at-a-time, except the cooldown test where P&L is the question.
Exit unless stated: stop 20 / target 30, 2 pts slippage each way, forced 15:15.

Usage: BN_DRY_RUN=1 python -m replay.study_suite [--only 21,1,17] [--cache FILE]
"""

import os
import pickle
import random
import re
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta

os.environ.setdefault("BN_DRY_RUN", "1")

import replay.bt_vwap_pullback as bt  # noqa: E402
from replay.bt_vwap_pullback import CONTRACTS, load_days, signals, tradable  # noqa: E402

CLUSTER_MIN = 30
RNG = random.Random(7)
BANDS = ((0, 0.8, "<0.8"), (0.8, 1.0, "0.8-1.0"), (1.0, 1.3, "1.0-1.3"), (1.3, 1e9, ">1.3"))


# ---------------------------------------------------------------- data

def oi_regime(m1, n):
    j = n - 16
    if j < 0 or m1[j]["t"].date() != m1[n]["t"].date():
        return None
    d_oi = m1[n - 1]["oi"] - m1[j]["oi"]
    d_px = m1[n - 1]["close"] - m1[j]["close"]
    if d_oi == 0 or d_px == 0:
        return None
    if d_oi > 0:
        return "long_buildup" if d_px > 0 else "short_buildup"
    return "short_covering" if d_px > 0 else "long_unwinding"


def build():
    U, series = [], {}
    for sym in CONTRACTS:
        days = load_days(sym)
        td = tradable(sym, days)
        sigs, m1 = signals(days, td)
        idx = {r["t"]: n for n, r in enumerate(m1)}
        series[sym] = dict(td=td, m1=m1, idx=idx)
        for s in sigs:
            n = idx.get(s["ct"])
            if n is None:
                continue
            U.append(dict(s, sym=sym, i=n, day=f"{s['ct']:%Y-%m-%d}",
                          oi=oi_regime(m1, n), aplus=bt.GRADE_AP(s)))
    return U, series


# ---------------------------------------------------------------- simulation

def sim(m1, i0, side, stop=20, tgt=30, slip=2.0, max_min=None, be=None, trail=None):
    """Stop is checked before target within a bar. be: move stop to entry after +be
    pts. trail: (trigger, dist) — once +trigger is reached the stop follows the best
    price by dist. Stop changes take effect from the NEXT bar (no lookahead)."""
    entry = m1[i0]["open"]
    exit_t = datetime.combine(m1[i0]["t"].date(), bt.FORCED_EXIT)
    t_lim = m1[i0]["t"] + timedelta(minutes=max_min) if max_min else None
    sl, best = entry - side * stop, 0.0
    for r in m1[i0:]:
        if r["t"] >= exit_t or (t_lim and r["t"] >= t_lim):
            return (r["open"] - entry) * side - 2 * slip
        hit_s = r["low"] <= sl if side > 0 else r["high"] >= sl
        hit_t = r["high"] >= entry + tgt if side > 0 else r["low"] <= entry - tgt
        if hit_s:
            return (sl - entry) * side - 2 * slip
        if hit_t:
            return tgt - 2 * slip
        fav = (r["high"] - entry) if side > 0 else (entry - r["low"])
        best = max(best, fav)
        if be is not None and best >= be:
            sl = entry
        if trail is not None and best >= trail[0]:
            cand = entry + side * (best - trail[1])
            sl = max(sl, cand) if side > 0 else min(sl, cand)
    return (m1[-1]["close"] - entry) * side - 2 * slip


def run(series, items, delay=0, **kw):
    out = []
    for s in items:
        m1 = series[s["sym"]]["m1"]
        i = s["i"] + delay
        if i >= len(m1) or m1[i]["t"].date() != m1[s["i"]]["t"].date() \
                or m1[i]["t"].time() >= bt.FORCED_EXIT:
            continue
        out.append(dict(pts=sim(m1, i, s["side"], **kw), day=s["day"], side=s["side"],
                        ct=s["ct"], it=s))
    return out


def independent(tr):
    out, last = [], {}
    for x in sorted(tr, key=lambda x: x["ct"]):
        k = (x["day"], x["side"])
        if k in last and x["ct"] - last[k] < timedelta(minutes=CLUSTER_MIN):
            continue
        last[k] = x["ct"]
        out.append(x)
    return out


def boot(p, n=2000):
    if len(p) < 2:
        return None, None
    m = sorted(sum(RNG.choices(p, k=len(p))) / len(p) for _ in range(n))
    return m[int(0.05 * n)], m[int(0.95 * n) - 1]


def summ(tr, label, w=36):
    ind = independent(tr)
    if not ind:
        print(f"  {label:<{w}} raw {len(tr):3d} ind   0")
        return None
    p = [x["pts"] for x in ind]
    lo, hi = boot(p)
    by = defaultdict(float)
    for x in ind:
        by[x["day"]] += x["pts"]
    pos = sum(1 for v in by.values() if v > 0)
    avg = sum(p) / len(p)
    ci = f"[{lo:+6.1f},{hi:+6.1f}]" if lo is not None else "[   n/a    ]"
    print(f"  {label:<{w}} raw {len(tr):3d} ind {len(ind):3d} {100 * sum(1 for v in p if v > 0) / len(p):3.0f}% "
          f"avg {avg:+6.1f} CI {ci} sess+ {pos:2d}/{len(by):2d}")
    return avg


def hdr(n, title):
    print(f"\n{'=' * 8} #{n}  {title}")


# ---------------------------------------------------------------- tests

def t21(series, U):
    hdr(21, "Random-entry benchmark (same side, same time of day)")
    slot = defaultdict(list)
    for sym, S in series.items():
        for n, r in enumerate(S["m1"]):
            if f"{r['t']:%Y-%m-%d}" in {f"{d}" for d in S["td"]}:
                slot[r["t"].hour * 60 + r["t"].minute].append((sym, n))
    for name, items in (("all VWAP-pullback signals", U),
                        ("A+ (15m slope + vol>1.0)", [s for s in U if s["aplus"]])):
        base = independent(run(series, items))
        actual = sum(x["pts"] for x in base) / len(base)
        avgs, wins = [], []
        for _ in range(300):
            p = []
            for x in base:
                sym, n = RNG.choice(slot[x["ct"].hour * 60 + x["ct"].minute])
                p.append(sim(series[sym]["m1"], n, x["side"]))
            avgs.append(sum(p) / len(p))
            wins.append(sum(1 for v in p if v > 0) / len(p))
        avgs.sort()
        pct = sum(1 for a in avgs if a < actual) / len(avgs)
        print(f"  {name:<28} n={len(base):3d}  actual avg {actual:+6.1f} | random avg "
              f"{sum(avgs) / len(avgs):+6.1f} (90%: {avgs[15]:+.1f}..{avgs[284]:+.1f}), "
              f"win {100 * sum(wins) / len(wins):.0f}% | actual beats {100 * pct:.0f}% of random runs")


def t1(series, U):
    hdr(1, "MFE / MAE after entry, then stop x target grid (independent signals)")
    base = independent(run(series, U))
    for H in (5, 15, 30):
        mfe, mae = [], []
        for x in base:
            s = x["it"]
            m1 = series[s["sym"]]["m1"]
            e = m1[s["i"]]["open"]
            end = m1[s["i"]]["t"] + timedelta(minutes=H)
            f = a = 0.0
            for r in m1[s["i"]:]:
                if r["t"] >= end or r["t"].time() >= bt.FORCED_EXIT:
                    break
                hi_, lo_ = (r["high"] - e, e - r["low"]) if s["side"] > 0 else (e - r["low"], r["high"] - e)
                f, a = max(f, hi_), max(a, lo_)
            mfe.append(f)
            mae.append(a)
        q = lambda v, k: sorted(v)[int(k * (len(v) - 1))]  # noqa: E731
        print(f"  {H:2d} min: favourable  p25 {q(mfe, .25):5.0f} median {q(mfe, .5):5.0f} p75 {q(mfe, .75):5.0f}"
              f" | adverse p25 {q(mae, .25):5.0f} median {q(mae, .5):5.0f} p75 {q(mae, .75):5.0f}")
    print("\n  avg pts/trade by stop (rows) x target (cols); 36 cells, so the best cell is optimistic")
    tg = (15, 20, 30, 40, 50, 60)
    print("  stop\\tgt " + "".join(f"{t:>8}" for t in tg))
    for st in (10, 15, 20, 25, 30, 40):
        row = ""
        for t in tg:
            p = [x["pts"] for x in independent(run(series, [x["it"] for x in base], stop=st, tgt=t))]
            row += f"{sum(p) / len(p):>+8.1f}"
        print(f"  {st:>8} " + row)


def t3t4(series, U):
    hdr("3/4", "Time stop, breakeven, trailing (stop 20 / target 30 base)")
    for label, kw in (("baseline", {}), ("time stop 15 min", dict(max_min=15)),
                      ("time stop 30 min", dict(max_min=30)), ("breakeven after +10", dict(be=10)),
                      ("breakeven after +15", dict(be=15)), ("trail 15 / dist 10", dict(trail=(15, 10))),
                      ("trail 20 / dist 10", dict(trail=(20, 10)))):
        summ(run(series, U, **kw), label)


def t17(series, U):
    hdr(17, "Slippage break-even (per side)")
    for name, items in (("all signals", U), ("A+", [s for s in U if s["aplus"]])):
        a2 = None
        for sl in (2, 3, 4):
            a = summ(run(series, items, slip=sl), f"{name} slip {sl}")
            a2 = a if sl == 2 else a2
        if a2 is not None:
            print(f"    -> {name}: avg is zero at about {2 + a2 / 2:.1f} pts per side"
                  f"{' (negative even at 2)' if a2 < 0 else ''}")


def t16(series, U):
    hdr(16, "Entry delay by contract month")
    for name, items in (("all", U), ("A+", [s for s in U if s["aplus"]])):
        for sym in CONTRACTS + [None]:
            for dl in (0, 1, 2, 3):
                sub = [s for s in items if sym is None or s["sym"] == sym]
                summ(run(series, sub, delay=dl),
                     f"{name} {sym[9:12] if sym else 'ALL'} +{dl}min")
            print()


def t6(series, U):
    hdr(6, "Time of day")
    bk = (("09:30-10:30", 570, 630), ("10:30-13:00", 630, 780), ("13:00-15:00", 780, 901))
    for name, items in (("all", U), ("A+", [s for s in U if s["aplus"]])):
        for lab, a, b in bk:
            summ(run(series, [s for s in items if a <= s["ct"].hour * 60 + s["ct"].minute < b]),
                 f"{name} {lab}")


def t10(series, U):
    hdr(10, "OI regime over the 15 min before entry")
    for lab in ("long_buildup", "short_buildup", "short_covering", "long_unwinding"):
        summ(run(series, [s for s in U if s["oi"] == lab]), lab)
    sup = {"long_buildup", "short_covering"}
    supports = lambda s: (s["oi"] in sup) == (s["side"] > 0)  # noqa: E731
    summ(run(series, [s for s in U if s["oi"] and supports(s)]), "OI regime supports the side")
    summ(run(series, [s for s in U if s["oi"] and not supports(s)]), "OI regime opposes the side")
    summ(run(series, [s for s in U if s["oi"] is None]), "no OI reading")


def t5(series, U):
    hdr(5, "5m EMA10 slope in the trade direction (pts/min), buckets around 4.3")
    for lab, a, b in (("against (<0)", -1e9, 0), ("0 - 2", 0, 2), ("2 - 4.3", 2, 4.3), (">4.3 (live)", 4.3, 1e9)):
        summ(run(series, [s for s in U if a <= s["s5"] * s["side"] < b]), lab)


# ---- live-engine replay (cached)

def _replay_once(series, cooldown, slope=None):
    import alert_engine
    import config
    from replay.driver import replay_day
    old = config.EMA_SLOPE_HYSTERESIS_POINTS
    alert_engine.FLIP_COOLDOWN_MINUTES = cooldown
    if slope is not None:
        config.EMA_SLOPE_HYSTERESIS_POINTS = slope
    al, near = [], []
    try:
        for sym in CONTRACTS:
            for d in sorted(series[sym]["td"]):
                res = replay_day(sym, date.fromisoformat(d), write_signal_log=False)
                for a in res.alerts:
                    if a.direction and a.grade in ("A+", "A", "B"):
                        al.append(dict(sym=sym, ts=a.timestamp.replace(tzinfo=None),
                                       direction=a.direction, grade=a.grade, setup=a.setup,
                                       reasons=list(a.reason_list)))
                # Only single-blocker rows are ever read; the full list is millions.
                near += [dict(r, sym=sym) for r in res.nearmiss if r["blocker_count"] == 1]
    finally:
        config.EMA_SLOPE_HYSTERESIS_POINTS = old
    return dict(alerts=al, near=near)


def replay_all(series, cache_path, keys):
    """keys: an int cooldown (minutes, slope as configured) or ("slope", h) which
    replays at the live 5-minute cooldown with hysteresis h."""
    cache = {}
    if os.path.exists(cache_path):
        cache = pickle.load(open(cache_path, "rb"))
    for k in [k for k in keys if k not in cache]:
        cache[k] = (_replay_once(series, k) if isinstance(k, int)
                    else _replay_once(series, 5, slope=k[1]))
        print(f"  replayed {k}: {len(cache[k]['alerts'])} alerts", flush=True)
        pickle.dump(cache, open(cache_path, "wb"))
    return cache


def near_events(series, near, gate, vwap=True):
    ev = {}
    for r in near:
        if r["blocker_count"] != 1 or r["blocking_gate"] != gate or ("vwap" in r["setup"]) != vwap:
            continue
        ts = datetime.fromisoformat(r["timestamp"]).replace(tzinfo=None)
        ev.setdefault((r["sym"], r["bar_start"], r["direction"]), ts)
    out = []
    for (sym, _b, dirn), ts in ev.items():
        i = series[sym]["idx"].get(ts.replace(second=0, microsecond=0) + timedelta(minutes=1))
        if i is not None:
            out.append(dict(sym=sym, i=i, day=f"{ts:%Y-%m-%d}", ct=ts,
                            side=1 if dirn == "long" else -1))
    return out


def t_trendgate(series, cache):
    hdr("trend-gate", "VWAP-setup events blocked ONLY by the trend gate vs VWAP alerts that fired")
    blocked = near_events(series, cache[5]["near"], "trend")
    fired = [x for x in live_items(series, cache[5]["alerts"]) if "vwap" in x["setup"]]
    close = lambda x, pool: any(p["day"] == x["day"] and p["side"] == x["side"] and  # noqa: E731
                                abs((p["ct"] - x["ct"]).total_seconds()) < CLUSTER_MIN * 60 for p in pool)
    apart = [x for x in blocked if not close(x, fired)]
    print(f"  blocked-only events {len(blocked)}; {len(blocked) - len(apart)} sit within "
          f"{CLUSTER_MIN} min of a fired VWAP alert (same side); {len(apart)} do not")
    print("  -- exits")
    for lab, kw in (("stop 20 / tgt 30", {}), ("stop 10 / tgt 20", dict(stop=10, tgt=20)),
                    ("stop 20 / tgt 40", dict(stop=20, tgt=40)), ("stop 30 / tgt 30", dict(stop=30, tgt=30))):
        summ(run(series, blocked, **kw), f"BLOCKED  {lab}")
        summ(run(series, fired, **kw), f"FIRED    {lab}")
    print("  -- by contract month")
    for sym in CONTRACTS:
        summ(run(series, [x for x in blocked if x["sym"] == sym]), f"BLOCKED  {sym[9:12]}")
        summ(run(series, [x for x in fired if x["sym"] == sym]), f"FIRED    {sym[9:12]}")
    print("  -- by direction")
    for sd, nm in ((1, "long"), (-1, "short")):
        summ(run(series, [x for x in blocked if x["side"] == sd]), f"BLOCKED  {nm}")
        summ(run(series, [x for x in fired if x["side"] == sd]), f"FIRED    {nm}")
    print("  -- time-split halves (by date)")
    ds = sorted({x["day"] for x in blocked})
    mid = ds[len(ds) // 2]
    for lab, f in (("first half", lambda x: x["day"] < mid), ("second half", lambda x: x["day"] >= mid)):
        summ(run(series, [x for x in blocked if f(x)]), f"BLOCKED  {lab}")
        summ(run(series, [x for x in fired if f(x)]), f"FIRED    {lab}")
    print("  -- only blocked events NOT near a fired alert")
    summ(run(series, apart), "BLOCKED  (apart from fired)")
    b = [x["pts"] for x in independent(run(series, blocked))]
    f = [x["pts"] for x in independent(run(series, fired))]
    d = sorted(sum(RNG.choices(b, k=len(b))) / len(b) - sum(RNG.choices(f, k=len(f))) / len(f)
               for _ in range(2000))
    print(f"  difference (blocked - fired) avg {sum(b) / len(b) - sum(f) / len(f):+.1f}, "
          f"90% CI [{d[100]:+.1f}, {d[1899]:+.1f}], blocked better in "
          f"{100 * sum(1 for v in d if v > 0) / len(d):.0f}% of resamples")


def book_pts(series, al):
    trades, busy = [], {}
    for it in sorted(live_items(series, al), key=lambda x: x["ct"]):
        m1 = series[it["sym"]]["m1"]
        if (it["day"] in busy and m1[it["i"]]["t"] < busy[it["day"]]) \
                or m1[it["i"]]["t"].time() >= bt.FORCED_EXIT:
            continue
        trades.append(sim(m1, it["i"], it["side"]))
        busy[it["day"]] = _exit_time(m1, it["i"], it["side"])
    return trades


def t_slope(series, cache):
    hdr("slope", "Trend hysteresis threshold (live engine replayed at each value, cooldown 5)")
    for h in (3.0, 3.7, 4.3, 5.0):
        al = cache[5]["alerts"] if h == 4.3 else cache[("slope", h)]["alerts"]
        L = live_items(series, al)
        bk = book_pts(series, al)
        print(f"  --- hysteresis {h}{'  (live)' if h == 4.3 else ''}: {len(al)} alerts, "
              f"one-at-a-time {len(bk)} trades, {sum(bk):+.0f} pts, avg {sum(bk) / max(len(bk), 1):+.1f}")
        summ(run(series, L), "all grades")
        summ(run(series, [x for x in L if x["grade"] in ("A", "A+")]), "A / A+ (Telegram)")
        summ(run(series, [x for x in L if "vwap" in x["setup"]]), "setups containing vwap")


def live_items(series, alerts):
    out = []
    for a in alerts:
        S = series[a["sym"]]
        et = a["ts"].replace(second=0, microsecond=0) + timedelta(minutes=1)
        i = S["idx"].get(et)
        if i is None:
            continue
        vol = None
        for r in a["reasons"]:
            m = re.search(r"relative volume ([\d.]+)", r)
            if m:
                vol = float(m.group(1))
        out.append(dict(sym=a["sym"], i=i, day=f"{a['ts']:%Y-%m-%d}", ct=a["ts"],
                        side=1 if a["direction"].lower() == "long" else -1,
                        grade=a["grade"], setup=a["setup"] or "", vol=vol))
    return out


def t12(series, L):
    hdr("12/13/14", "Does the live grading add value? Grade, volume band, setup (live alerts, cooldown=5)")
    for g in ("A+", "A", "B"):
        summ(run(series, [x for x in L if x["grade"] == g]), f"grade {g}")
    summ(run(series, L), "all grades")
    print()
    for a, b, lab in BANDS:
        summ(run(series, [x for x in L if x["vol"] is not None and a <= x["vol"] < b]), f"projected vol ratio {lab}")
    summ(run(series, [x for x in L if x["vol"] is None]), "volume ratio not parsed")
    print()
    for st in sorted({x["setup"] for x in L}):
        summ(run(series, [x for x in L if x["setup"] == st]), f"setup {st}")
    print()
    for g in ("A+", "A", "B"):
        for st in sorted({x["setup"] for x in L}):
            sub = [x for x in L if x["grade"] == g and x["setup"] == st]
            if sub:
                summ(run(series, sub), f"grade {g} x setup {st}")


def t11(series, near, L):
    hdr(11, "Gate value: outcome if the ONLY failing gate had passed vs alerts that fired")
    ev = {}
    for r in near:
        if r["blocker_count"] != 1:
            continue
        ts = datetime.fromisoformat(r["timestamp"]).replace(tzinfo=None)
        ev.setdefault((r["sym"], r["bar_start"], r["direction"], r["setup"], r["blocking_gate"]), (ts, r))
    groups = defaultdict(list)
    for (sym, _bar, dirn, setup, gate), (ts, _r) in ev.items():
        S = series[sym]
        i = S["idx"].get(ts.replace(second=0, microsecond=0) + timedelta(minutes=1))
        if i is None:
            continue
        groups[(setup.replace("long_", "").replace("short_", "") or "ema", gate)].append(
            dict(sym=sym, i=i, day=f"{ts:%Y-%m-%d}", ct=ts, side=1 if dirn == "long" else -1))
    print("  (alerts that fired, for reference)")
    for st in ("ema", "vwap"):
        summ(run(series, [x for x in L if st in x["setup"]]), f"FIRED {st}")
    print("  (a gate whose blocked events still win is a gate that may be costing signals)")
    for (setup, gate), items in sorted(groups.items()):
        summ(run(series, items), f"blocked by {gate} only [{setup or 'ema'}]", w=44)


def t_cooldown(series, cache):
    hdr("cooldown", "5-min flip cooldown re-tested on the full corpus (one position at a time)")
    base = cache[0]["alerts"]
    key = lambda a: (a["ts"], a["direction"], a["grade"])  # noqa: E731
    bset = {key(a) for a in base}
    print(f"  {'cooldown':>9} {'alerts':>7} {'removed':>8} {'added':>6} | taken  win%    avg   total"
          f" | removed-alert outcome")
    for m in sorted(k for k in cache if isinstance(k, int)):
        al = cache[m]["alerts"]
        mset = {key(a) for a in al}
        removed = [a for a in base if key(a) not in mset]
        added = [a for a in al if key(a) not in bset]
        trades, busy = [], {}
        for it in sorted(live_items(series, al), key=lambda x: x["ct"]):
            S = series[it["sym"]]
            if it["day"] in busy and S["m1"][it["i"]]["t"] < busy[it["day"]]:
                continue
            if S["m1"][it["i"]]["t"].time() >= bt.FORCED_EXIT:
                continue
            pts = sim(S["m1"], it["i"], it["side"])
            trades.append(pts)
            # approximate: block the day until a typical resolution; recomputed exactly below
            busy[it["day"]] = _exit_time(S["m1"], it["i"], it["side"])
        w = 100 * sum(1 for p in trades if p > 0) / max(len(trades), 1)
        rem = [x["pts"] for x in run(series, live_items(series, removed))]
        remtxt = (f"n={len(rem)} avg {sum(rem) / len(rem):+.1f}" if rem else "-")
        print(f"  {str(m) + 'm':>9} {len(al):>7} {len(removed):>8} {len(added):>6} | {len(trades):>5} {w:>4.0f}% "
              f"{sum(trades) / max(len(trades), 1):>+6.1f} {sum(trades):>+7.0f} | {remtxt}")


def _exit_time(m1, i0, side, stop=20, tgt=30):
    entry = m1[i0]["open"]
    exit_t = datetime.combine(m1[i0]["t"].date(), bt.FORCED_EXIT)
    for r in m1[i0:]:
        if r["t"] >= exit_t:
            return r["t"]
        hs = r["low"] <= entry - stop if side > 0 else r["high"] >= entry + stop
        ht = r["high"] >= entry + tgt if side > 0 else r["low"] <= entry - tgt
        if hs or ht:
            return r["t"]
    return m1[-1]["t"]


# ---------------------------------------------------------------- main

def main(argv):
    only = None
    cache_path = os.path.join(tempfile.gettempdir(), "bn_study_cache.pkl")
    for k, a in enumerate(argv):
        if a == "--only":
            only = set(argv[k + 1].split(","))
        if a == "--cache":
            cache_path = argv[k + 1]
    want = lambda n: only is None or n in only  # noqa: E731

    U, series = build()
    days = {s["day"] for s in U}
    print(f"{len(U)} VWAP-pullback signals over {len(days)} sessions; "
          f"A+ = {sum(1 for s in U if s['aplus'])}")
    # sanity: this module's simulator must equal the backtest's on the default exit
    for s in U[:40]:
        m1 = series[s["sym"]]["m1"]
        if m1[s["i"]]["t"].time() < bt.FORCED_EXIT:
            assert abs(sim(m1, s["i"], s["side"]) - bt.simulate(m1, s["i"], s["side"], 20, 30)[0]) < 1e-9

    if want("21"): t21(series, U)
    if want("1"): t1(series, U)
    if want("17"): t17(series, U)
    if want("16"): t16(series, U)
    if want("3"): t3t4(series, U)
    if want("6"): t6(series, U)
    if want("10"): t10(series, U)
    if want("5"): t5(series, U)
    if any(want(n) for n in ("11", "12", "cooldown")):
        cache = replay_all(series, cache_path, (0, 3, 5, 10, 12, 15))
        L = live_items(series, cache[5]["alerts"])
        if want("12"): t12(series, L)
        if want("11"): t11(series, cache[5]["near"], L)
        if want("cooldown"): t_cooldown(series, cache)
    if want("trendgate"):
        t_trendgate(series, replay_all(series, cache_path, (5,)))
    if want("slope"):
        t_slope(series, replay_all(series, cache_path, (5, ("slope", 3.0), ("slope", 3.7), ("slope", 5.0))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
