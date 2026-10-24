"""External-methods tests (24 Oct 2026). Exploratory: rules written before the run, no tuning after.
Data: BANKNIFTY26OCTFUT 1-min with OI (1 Sep - 23 Oct), NIFTY BANK spot 1-min (1 Sep - 23 Oct).
Trading sessions 24 Sep - 23 Oct. Base signals copied from bt_vwap_pullback.py / bt_trendday.py.
Usage: python3 bt_external.py fut.json spot.json
"""
import json, sys, random, statistics as st, math
from collections import defaultdict

FUT, SPOT = sys.argv[1], sys.argv[2]
FIRST, IS_END = "2026-09-24", "2026-10-15"
SLIP = 2.0
FRONT = "2026-10-01"          # Oct contract is front month from here (Sep expiry 30 Sep)

def load(path):
    seen = {}
    for b in json.load(open(path)):
        d, t = b['date'][:10], b['date'][11:16]
        if "09:15" <= t <= "15:29":
            seen[(d, t)] = dict(day=d, t=t, o=b['open'], h=b['high'], l=b['low'], c=b['close'],
                                v=b.get('volume', 0), oi=b.get('oi') or 0)
    return [seen[k] for k in sorted(seen)]
bars = load(FUT); sbars = load(SPOT)
days = sorted({b['day'] for b in bars})
byday = defaultdict(list)
for b in bars: byday[b['day']].append(b)
sday = defaultdict(list)
for b in sbars: sday[b['day']].append(b)
def mins(t): return int(t[:2]) * 60 + int(t[3:])
def hm(m): return f"{m // 60:02d}:{m % 60:02d}"
for d in days:
    pv = vv = pv2 = 0.0
    for b in byday[d]:
        tp = (b['h'] + b['l'] + b['c']) / 3; vol = max(b['v'], 1)
        pv += tp * vol; vv += vol; pv2 += tp * tp * vol
        b['vwap'] = pv / vv; b['sd'] = math.sqrt(max(pv2 / vv - b['vwap'] ** 2, 0))
idx1 = {(b['day'], b['t']): k for k, b in enumerate(bars)}
tdays = [d for d in days if d >= FIRST]

def resample(tf):
    out = []
    for d in days:
        g2 = defaultdict(list)
        for b in byday[d]: g2[(mins(b['t']) - 555) // tf].append(b)
        for k in sorted(g2):
            g = g2[k]
            out.append(dict(day=d, t=g[0]['t'], tclose=g[-1]['t'], o=g[0]['o'], h=max(x['h'] for x in g),
                            l=min(x['l'] for x in g), c=g[-1]['c'], v=sum(x['v'] for x in g),
                            vwap=g[-1]['vwap'], sd=g[-1]['sd'], oi=g[-1]['oi']))
    return out
def ema(vals, n):
    k = 2 / (n + 1); e = None; o = []
    for x in vals: e = x if e is None else e + k * (x - e); o.append(e)
    return o
htf = resample(15)
for b, e in zip(htf, ema([x['c'] for x in htf], 20)): b['e20'] = e
for i, b in enumerate(htf): b['slope'] = b['e20'] - htf[i - 1]['e20'] if i else 0
def htf_slope(day, tclose):
    s = 0
    for b in htf:
        if (b['day'], b['tclose']) <= (day, tclose): s = b['slope']
        else: break
    return s

tb5 = resample(5)
for i, b in enumerate(tb5):
    w = tb5[max(0, i - 20):i]
    b['vma'] = sum(x['v'] for x in w) / len(w) if w else b['v']
    b['prev_oi'] = tb5[i - 1]['oi'] if i else None
# Wilder ADX(14) on 5m, continuous
def adx(series, n=14):
    tr_s = pdm_s = ndm_s = None; adx_v = None; dxs = []
    for i, b in enumerate(series):
        if i == 0: b['adx'] = None; b['pdi'] = b['ndi'] = None; continue
        p = series[i - 1]
        tr = max(b['h'] - b['l'], abs(b['h'] - p['c']), abs(b['l'] - p['c']))
        up, dn = b['h'] - p['h'], p['l'] - b['l']
        pdm = up if up > dn and up > 0 else 0; ndm = dn if dn > up and dn > 0 else 0
        if tr_s is None: tr_s, pdm_s, ndm_s = tr, pdm, ndm
        else: tr_s = tr_s - tr_s / n + tr; pdm_s = pdm_s - pdm_s / n + pdm; ndm_s = ndm_s - ndm_s / n + ndm
        pdi = 100 * pdm_s / tr_s if tr_s else 0; ndi = 100 * ndm_s / tr_s if tr_s else 0
        dx = 100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0
        if adx_v is None:
            dxs.append(dx)
            if len(dxs) == n: adx_v = sum(dxs) / n
        else: adx_v = (adx_v * (n - 1) + dx) / n
        b['adx'], b['pdi'], b['ndi'] = adx_v, pdi, ndi
adx(tb5)
five = defaultdict(list)
for b in tb5: five[b['day']].append(b)
# time-of-day slot baseline (front-month sessions only, prior 10, need >=5)
slotvol = defaultdict(dict)
for b in tb5: slotvol[b['day']][b['t']] = b['v']
fdays = [d for d in days if d >= FRONT]
for b in tb5:
    prior = [d for d in fdays if d < b['day']][-10:]
    vals = [slotvol[d][b['t']] for d in prior if b['t'] in slotvol[d]]
    b['tod'] = (b['v'] / (sum(vals) / len(vals))) if len(vals) >= 5 and sum(vals) else None

# prior-day value area (futures, 10-pt bins)
def value_area(dbars):
    vol = defaultdict(float)
    for b in dbars:
        lo, hi = int(b['l'] // 10), int(b['h'] // 10); n = hi - lo + 1
        for k in range(lo, hi + 1): vol[k] += b['v'] / n
    poc = max(vol, key=vol.get); tot = sum(vol.values()); lo = hi = poc; acc = vol[poc]
    while acc < 0.7 * tot:
        a, c = vol.get(hi + 1, 0), vol.get(lo - 1, 0)
        if a == 0 and c == 0: break
        if a >= c: hi += 1; acc += a
        else: lo -= 1; acc += c
    return poc * 10 + 5, hi * 10 + 10, lo * 10
prior = {}
for i in range(1, len(days)):
    p = byday[days[i - 1]]; poc, vah, val = value_area(p)
    prior[days[i]] = dict(pdc=p[-1]['c'], poc=poc, vah=vah, val=val)

# ---------------- base signals: VWAP pullback (identical trigger), optional trend filter ----------------
def vwap_signals(trend='htf'):
    out = []
    for d in tdays:
        d5 = five[d]
        for i in range(1, len(d5)):
            b, p = d5[i], d5[i - 1]
            if not ("09:30" <= b['tclose'] <= "15:00"): continue
            if p['c'] > p['vwap'] and b['l'] <= b['vwap'] < b['c'] and b['c'] > b['o']: dr = 1
            elif p['c'] < p['vwap'] and b['h'] >= b['vwap'] > b['c'] and b['c'] < b['o']: dr = -1
            else: continue
            hs = htf_slope(d, b['tclose'])
            if trend == 'htf' and not (dr * hs > 0): continue
            oich = (b['oi'] - b['prev_oi']) / b['prev_oi'] if b['prev_oi'] else 0
            oist = 'Rising' if oich > 0.0005 else 'Falling' if oich < -0.0005 else 'Flat'
            pierce = (b['vwap'] - b['l']) if dr == 1 else (b['h'] - b['vwap'])
            out.append(dict(day=d, i=i, tclose=b['tclose'], dir=dr, vr=b['v'] / b['vma'] if b['vma'] else 0,
                            tod=b['tod'], oist=oist, pierce=pierce, hs=hs, adx=b['adx'],
                            di=(1 if (b['pdi'] or 0) > (b['ndi'] or 0) else -1), hi=b['h'], lo=b['l']))
    return out

# ---------------- simulation ----------------
def run(sigs, exitm='f20_30', lat=0, entry='mkt', slip=SLIP, rnd=None):
    """exitm: f20_30 | hold (stop 20, exit 15:15) | vwapx (stop 20, exit on 5m close beyond VWAP against)
       | be_vwapx (as vwapx, stop to breakeven after +20).  entry: mkt | stop (stop order beyond signal bar)"""
    out = []; busy = None
    for s in sorted(sigs, key=lambda s: (s['day'], s['tclose'])):
        dr = s['dir'] if rnd is None else rnd.choice((1, -1))
        k0 = idx1.get((s['day'], hm(mins(s['tclose']) + 1 + lat)))
        if k0 is None or (busy is not None and k0 <= busy): continue
        if entry == 'mkt':
            k = k0; e = bars[k]['o']
        else:  # 'stop' (pessimistic: fill bar's full range counts) or 'stop_opt' (checks start next bar)
            lvl = (s['hi'] + 1) if dr == 1 else (s['lo'] - 1); k = None
            for j in range(k0, min(k0 + 15, len(bars))):
                x = bars[j]
                if x['day'] != s['day']: break
                if (x['h'] >= lvl) if dr == 1 else (x['l'] <= lvl):
                    k = j; e = max(lvl, x['o']) if dr == 1 else min(lvl, x['o']); break
            if k is None: continue
        stp = e - dr * (60 if exitm == 'vwap_only' else 20); tgt = e + dr * 30; j = k + (1 if entry == 'stop_opt' else 0)
        mfe = 0; be = False; trail_on = exitm in ('vwapx', 'be_vwapx', 'vwap_only')
        while j < len(bars) and bars[j]['day'] == s['day']:
            x = bars[j]
            if x['t'] >= "15:15": px, why = x['o'], 'time'; break
            if (x['l'] <= stp) if dr == 1 else (x['h'] >= stp): px, why = stp, 'stop'; break
            if exitm == 'f20_30' and ((x['h'] >= tgt) if dr == 1 else (x['l'] <= tgt)): px, why = tgt, 'target'; break
            mfe = max(mfe, dr * ((x['h'] if dr == 1 else x['l']) - e))
            if exitm == 'be_vwapx' and not be and mfe >= 20: stp = e; be = True
            if exitm == 't30_trail' and not be and mfe >= 30: stp = e + dr * 10; be = True; trail_on = True
            if trail_on and (mins(x['t']) - 554) % 5 == 0 and j > k:
                if dr * (x['c'] - x['vwap']) < 0: px, why = x['c'], 'vwap'; j += 0; break
            j += 1
        else:
            j -= 1; px, why = bars[j]['c'], 'eod'
        net = dr * (px - e) - 2 * slip
        out.append(dict(day=s['day'], sig=s['tclose'], dir=dr, entry=e, exit=px, why=why, net=round(net, 1),
                        mfe=round(mfe, 1)))
        busy = j
    return out
def stats(tr):
    if not tr: return "  n=0"
    n = [t['net'] for t in tr]; w = sum(1 for x in n if x > 0)
    eq = pk = dd = 0
    for x in n: eq += x; pk = max(pk, eq); dd = min(dd, eq - pk)
    return f"n={len(n):3d} win={100 * w / len(n):5.1f}% avg={sum(n) / len(n):+6.1f} tot={sum(n):+7.0f} DD={dd:+6.0f}"
def win(tr, w):
    if w == 'IS': return [t for t in tr if t['day'] <= IS_END]
    if w == 'OOS': return [t for t in tr if t['day'] > IS_END]
    return tr
def show(label, tr, splits=True):
    line = f"  {label:42s} ALL {stats(tr)}"
    if splits: line += f" | IS {stats(win(tr, 'IS'))[:26]} | OOS {stats(win(tr, 'OOS'))[:26]}"
    print(line)
def rnd_bench(sigs, runs=300, **kw):
    r = random.Random(11); avgs = []; wins = []
    for _ in range(runs):
        tr = run(sigs, rnd=r, **kw)
        if tr: avgs.append(sum(t['net'] for t in tr) / len(tr)); wins.append(100 * sum(t['net'] > 0 for t in tr) / len(tr))
    avgs.sort(); wins.sort()
    return f"random-direction: win {st.mean(wins):.0f}% avg {st.mean(avgs):+.1f} (90% band {avgs[int(.05 * len(avgs))]:+.1f}..{avgs[int(.95 * len(avgs))]:+.1f})"

if __name__ == '__main__':
    print(f"sessions {tdays[0]}..{tdays[-1]} n={len(tdays)}  (IS <= {IS_END}, OOS after)")
    A = vwap_signals('htf'); Ap = [s for s in A if s['vr'] > 1.0]
    print("\n=== BASELINE (should match earlier docs: A 33 tr / A+ IS 8 tr +108) ===")
    show("A (VWAP pullback + 15m slope)", run(A)); show("A+ (A + vol > MA20)", run(Ap))

    print("\n=== T1  OI IN THE GRADE (signal bar OI vs previous 5m bar, live +/-0.05% rule) ===")
    for st_ in ('Rising', 'Flat', 'Falling'):
        show(f"A with OI {st_}", run([s for s in A if s['oist'] == st_]), False)
    show("A+ (vol>MA20) -- no OI", run(Ap)); show("A+ AND OI Rising", run([s for s in Ap if s['oist'] == 'Rising']))
    show("A+ AND OI not Rising", run([s for s in Ap if s['oist'] != 'Rising']))
    hv = [s for s in A if s['vr'] >= 1.3]
    show("live-like: vol>=1.3 -- no OI", run(hv)); show("live-like: vol>=1.3 AND OI Rising (live A+)", run([s for s in hv if s['oist'] == 'Rising']))
    qd = lambda s: ('build' if s['oist'] == 'Rising' else 'unwind' if s['oist'] == 'Falling' else 'flat')
    for q in ('build', 'unwind', 'flat'):
        show(f"A, OI {q} (trade-direction agnostic)", run([s for s in A if qd(s) == q]), False)

    print("\n=== T2  TIME-OF-DAY RELATIVE VOLUME (same 5m slot, prior <=10 front-month sessions, need >=5) ===")
    At = [s for s in A if s['tod'] is not None]
    print(f"  signals with a TOD baseline: {len(At)} of {len(A)} (from {min(s['day'] for s in At)})")
    show("A (same days)", run(At), False)
    show("A+ by MA20 > 1.0 (same days)", run([s for s in At if s['vr'] > 1.0]), False)
    show("A+ by TOD > 1.0", run([s for s in At if s['tod'] > 1.0]), False)
    for lo, hi in ((0, 0.8), (0.8, 1.0), (1.0, 1.3), (1.3, 99)):
        show(f"A, TOD band {lo}-{hi}", run([s for s in At if lo <= s['tod'] < hi]), False)
    for lo, hi in ((0, 0.8), (0.8, 1.0), (1.0, 1.3), (1.3, 99)):
        show(f"A, MA20 band {lo}-{hi}", run([s for s in At if lo <= s['vr'] < hi]), False)
    ob = [b for d in fdays if d >= min(s['day'] for s in At) for b in five[d] if b['tod'] is not None]
    for lab, f in (("09:15-09:40", lambda b: b['t'] <= "09:40"), ("11:00-13:00", lambda b: "11:00" <= b['t'] <= "13:00")):
        g = [b for b in ob if f(b)]
        print(f"  share of 5m bars reading > 1.3:  {lab}  MA20 {100 * sum(b['v'] / b['vma'] > 1.3 for b in g) / len(g):4.0f}%   TOD {100 * sum(b['tod'] > 1.3 for b in g) / len(g):4.0f}%   (n={len(g)})")

    print("\n=== T3  DAY TYPE AT 10:00 (one reading per day; outcome 10:00 -> 15:15 and +/-100 race) ===")
    def outcome(d, dr):
        db = byday[d]; s0 = next(b for b in db if b['t'] == "10:00"); px = s0['c']
        close = next(b['o'] for b in db if b['t'] >= "15:15"); res = None
        for b in db:
            if b['t'] <= "10:00": continue
            if dr * (b['h' if dr == 1 else 'l'] - px) >= 100: res = 'W'; break
            if dr * (b['l' if dr == 1 else 'h'] - px) <= -100: res = 'L'; break
        return dr * (close - px), res
    feats = defaultdict(dict)
    for d in tdays:
        if d not in prior: continue
        db = byday[d]; b10 = next(b for b in db if b['t'] == "10:00"); op = db[0]['o']
        if abs(b10['c'] - b10['vwap']) >= 75: feats[d]['c3_vwap75'] = 1 if b10['c'] > b10['vwap'] else -1
        sd_ = sday.get(d); sp = [x for x in sday if x < d][-14:]
        if sd_ and len(sp) >= 10:
            mv = next(b['c'] for b in sd_ if b['t'] == "10:00") - sd_[0]['o']
            base = st.mean(abs(next(b['c'] for b in sday[x] if b['t'] == "10:00") - sday[x][0]['o']) for x in sp)
            z = mv / base; feats[d]['_z'] = z
            if abs(z) >= 1.0: feats[d]['noise_band'] = 1 if z > 0 else -1
        e = [b for b in db if b['t'] <= "09:44"]; c = e[-1]['c']
        up, dn = max(b['h'] for b in e) - op, op - min(b['l'] for b in e)
        if dn <= 10 and c - op >= 50: feats[d]['open_drive'] = 1
        elif up <= 10 and op - c >= 50: feats[d]['open_drive'] = -1
        elif dn >= 50 and c - op >= 10: feats[d]['open_rej_rev'] = 1
        elif up >= 50 and op - c >= 10: feats[d]['open_rej_rev'] = -1
        else: feats[d]['_open_auction'] = 1
        gap = op - prior[d]['pdc']; c929 = next(b['c'] for b in db if b['t'] == "09:29")
        if abs(gap) >= 100 and (1 if gap > 0 else -1) * (c929 - op) >= 100: feats[d]['gap_extension'] = 1 if gap > 0 else -1
    for f in ('c3_vwap75', 'noise_band', 'open_drive', 'open_rej_rev', 'gap_extension'):
        ds = [d for d in feats if f in feats[d]]
        if not ds: print(f"  {f:14s} days 0"); continue
        oc = [outcome(d, feats[d][f]) for d in ds]
        mv = [o[0] for o in oc]; r = [o[1] for o in oc]
        print(f"  {f:14s} days {len(ds):2d}  avg move to 15:15 {st.mean(mv):+6.0f}  hit {100 * sum(m > 0 for m in mv) / len(mv):4.0f}%  race W/L {r.count('W')}/{r.count('L')}  [{', '.join(d[5:] + ('+' if feats[d][f] > 1e-9 else '-') for d in ds)}]")
    oa = [d for d in feats if '_open_auction' in feats[d]]
    rng = lambda d: max(b['h'] for b in byday[d] if b['t'] > "09:45") - min(b['l'] for b in byday[d] if b['t'] > "09:45")
    for lab, ds in (("open-auction days", oa), ("open-drive days", [d for d in feats if 'open_drive' in feats[d]]),
                    ("rej-reverse days", [d for d in feats if 'open_rej_rev' in feats[d]])):
        if ds: print(f"  range after 09:45 on {lab:18s} n={len(ds):2d} median {st.median(rng(d) for d in ds):.0f}")
    def dir_of(d):
        v = [feats[d][f] for f in ('noise_band', 'open_drive', 'gap_extension') if f in feats[d]]
        return (1 if sum(v) > 0 else -1 if sum(v) < 0 else 0), len(v)
    print("  combined (noise_band + open_drive + gap_extension):")
    for need in (1, 2):
        ds = [d for d in feats if dir_of(d)[1] >= need and dir_of(d)[0] != 0]
        if ds:
            oc = [outcome(d, dir_of(d)[0]) for d in ds]; mv = [o[0] for o in oc]; r = [o[1] for o in oc]
            print(f"    >= {need} feature(s) on: days {len(ds):2d} avg {st.mean(mv):+6.0f} hit {100 * sum(m > 0 for m in mv) / len(mv):4.0f}% race {r.count('W')}/{r.count('L')}")
    print("  A / A+ trades split by noise-band direction:")
    for lab, S in (("A", A), ("A+", Ap)):
        ag = [s for s in S if feats[s['day']].get('noise_band') == s['dir']]
        op_ = [s for s in S if feats[s['day']].get('noise_band') == -s['dir']]
        nn = [s for s in S if 'noise_band' not in feats[s['day']]]
        show(f"{lab} agrees with noise band", run(ag), False); show(f"{lab} against noise band", run(op_), False)
        show(f"{lab} no noise-band day", run(nn), False)

    print("\n=== T4  STOP-ENTRY (beyond signal bar +1, valid 15 min) vs MARKET ===")
    for lab, S in (("A", A), ("A+", Ap)):
        for lat in (0, 1):
            show(f"{lab} market, latency {lat}m", run(S, lat=lat), False)
            show(f"{lab} stop-entry, latency {lat}m (pessimistic)", run(S, lat=lat, entry='stop'), False)
            show(f"{lab} stop-entry, latency {lat}m (optimistic)", run(S, lat=lat, entry='stop_opt'), False)

    print("\n=== T5  PULLBACK DEPTH and SECOND ENTRY ===")
    for lab, S in (("A", A), ("A+", Ap)):
        for lo, hi in ((0, 10), (10, 20), (20, 999)):
            show(f"{lab} pierce {lo}-{hi}", run([s for s in S if lo <= s['pierce'] < hi]), False)
    def leg_index(S):
        for s in S:
            d5 = five[s['day']]; i = s['i']; dr = s['dir']
            ext_i = max(range(i), key=lambda j: d5[j]['h']) if dr == 1 else min(range(i), key=lambda j: d5[j]['l'])
            s['nth'] = 1 + sum(1 for x in S if x['day'] == s['day'] and x['dir'] == dr and ext_i < x['i'] < i)
    leg_index(A)
    show("A first signal since last extreme", run([s for s in A if s['nth'] == 1]), False)
    show("A second+ signal since last extreme", run([s for s in A if s['nth'] >= 2]), False)

    print("\n=== T6  TRADE COUNT PER DAY (A, taken sequence) ===")
    tr = run(A); seq = defaultdict(int)
    for t in tr: seq[t['day']] += 1; t['seq'] = seq[t['day']]
    for k in (1, 2, 3):
        g = [t for t in tr if (t['seq'] == k if k < 3 else t['seq'] >= 3)]
        print(f"  trade #{k}{'+' if k == 3 else ' '} of day: {stats(g)}")
    print(f"  trades per day: {dict(sorted(defaultdict(int, {n: sum(1 for v in seq.values() if v == n) for n in set(seq.values())}).items()))}")
    for cap in (1, 2, 3):
        print(f"  cap {cap}/day: {stats([t for t in tr if t['seq'] <= cap])}")
    kept = [];
    for d in sorted(seq):
        losses = 0
        for t in [x for x in tr if x['day'] == d]:
            if losses >= 2: break
            kept.append(t); losses += t['net'] <= 0
    print(f"  stop after 2 losses: {stats(kept)}")

    print("\n=== T7  TREND FILTER: ADX(14, 5m) vs 15m EMA20 slope (VWAP pullback with no filter as base) ===")
    N = vwap_signals('none')
    show("no trend filter", run(N), False)
    show("15m EMA20 slope agrees (= A)", run([s for s in N if s['dir'] * s['hs'] > 0]), False)
    for th in (20, 25, 30):
        show(f"ADX > {th} and DI agrees", run([s for s in N if (s['adx'] or 0) > th and s['di'] == s['dir']]), False)
    show("ADX > 25 (any DI)", run([s for s in N if (s['adx'] or 0) > 25]), False)
    show("ADX < 20", run([s for s in N if s['adx'] is not None and s['adx'] < 20]), False)

    print("\n=== S1  DYNAMIC EXITS (separate track; same entries, market +0) ===")
    for lab, S in (("A", A), ("A+", Ap)):
        for ex in ('f20_30', 'hold', 'vwapx', 'be_vwapx', 'vwap_only', 't30_trail'):
            tr = run(S, ex)
            print(f"      exits by reason: { {w: sum(t['why'] == w for t in tr) for w in ('stop', 'target', 'vwap', 'time', 'eod')} }")
            gb = [t['mfe'] - (t['net'] + 2 * SLIP) for t in tr if t['mfe'] > 0]
            kept_ = [(t['net'] + 2 * SLIP) / t['mfe'] for t in tr if t['mfe'] >= 20]
            show(f"{lab} exit {ex}", tr)
            print(f"      giveback: median {st.median(gb) if gb else 0:.0f} pts from best point; kept {100 * st.median(kept_) if kept_ else 0:.0f}% of MFE on trades that reached +20 (n={len(kept_)});  {rnd_bench(S, exitm=ex)}")
        print(f"      {lab} f20_30 at 3 pts slippage: {stats(run(S, slip=3))};  vwapx at 3 pts: {stats(run(S, 'vwapx', slip=3))}")

    print("\n=== S2  VWAP 2-SIGMA FADE, BALANCE DAYS ONLY (separate strategy) ===")
    def fade_signals(only_balance):
        out = []
        for d in tdays:
            if d not in prior: continue
            db = byday[d]; b1030 = next(b for b in db if b['t'] == "10:30"); b945 = next(b for b in db if b['t'] == "09:45")
            bal = abs(b1030['vwap'] - b945['vwap']) <= 20 and prior[d]['val'] <= b1030['c'] <= prior[d]['vah']
            if only_balance is not None and bal != only_balance: continue
            for i, b in enumerate(five[d]):
                if not ("10:35" <= b['tclose'] <= "14:30"): continue
                up, lo = b['vwap'] + 2 * b['sd'], b['vwap'] - 2 * b['sd']
                if b['h'] >= up and b['c'] < up and b['c'] < b['o']: dr = -1
                elif b['l'] <= lo and b['c'] > lo and b['c'] > b['o']: dr = 1
                else: continue
                out.append(dict(day=d, i=i, tclose=b['tclose'], dir=dr, hi=b['h'], lo=b['l']))
        return out
    nb = sum(1 for d in tdays if d in prior and (lambda db: abs(next(b for b in db if b['t'] == "10:30")['vwap'] - next(b for b in db if b['t'] == "09:45")['vwap']) <= 20 and prior[d]['val'] <= next(b for b in db if b['t'] == "10:30")['c'] <= prior[d]['vah'])(byday[d]))
    print(f"  balance days (|VWAP 10:30 - 09:45| <= 20 and 10:30 price inside prior value): {nb} of {len(tdays)}")
    for lab, ob_ in (("balance days", True), ("non-balance days", False), ("all days", None)):
        S = fade_signals(ob_)
        show(f"fade 20/30, {lab}", run(S), False)
        if ob_: print(f"      {rnd_bench(S)}")
