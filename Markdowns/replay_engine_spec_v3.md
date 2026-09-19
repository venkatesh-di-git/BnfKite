# Setup Replay Engine — Build Spec (rev 4, OHLC expansion, Phase 1 built)

**Supersedes `replay_engine_spec_close_only.md` and `bn_replay_engine_spec.md`.** Both deleted.

**Change in rev 4.** Rev 3 switched the tick model from close-only to OHLC expansion on the strength
of one figure — *"OHLC reproduces 10 of 13 live alerts on 14 Aug (77%), and the 10 match in time,
price, direction and setup."* **That figure was wrong.** 10 was an alert *count*, not a match count,
produced by a script that no longer existed when the claim was questioned. Rev 3 asked for
replication on 2–3 more sessions before trusting it; that replication has now run, from code in the
repo, and it disconfirms. See §1.

The switch to OHLC still stands — close-only reproduces **nothing** — but its justification, and the
whole of §0, had to be rebuilt around what the measurement actually says.

**Status: Phase 1 built and verified (§11). Phase 2 not started.**

---

## 0. Purpose

### §0a — what replay is accepted against (binding)

Replay reproduces the engine's **gate and rule behaviour** on an archived session, so a rule change —
a threshold, the latch, the cooldown — can be evaluated in seconds instead of waiting for a live
session.

The golden file (§11) is the instrument. **It is a change detector, not a correctness detector.** It
proves the engine has not drifted. It cannot prove the engine is right, and a passing golden file
must never be read as validation — that reading is exactly how §0b gets quietly abandoned.

### §0b — what replay does NOT currently do (tracked, unmet)

Reproduce the live alert stream. Measured over the four sessions that have live alerts:

| | strict | ceiling (secondary) |
|---|---|---|
| OHLC expansion | **5/56 — 9%** | 30/56 — 54% |
| close-only | **0/56 — 0%** | 11/56 — 20% |

This is an open defect, not a redefined success. It does not gate the cooldown, and it is **not**
closed by loosening the match rule.

**Replay does not score.** No MFE/MAE, win rate, R multiples, target or stop simulation, or parameter
optimisation. The alert list is the output. Outcome scoring lives in `review_session.py`, parked
pending a check of whether futures points track option P&L.

---

## 1. THE FINDING — what the replication actually showed

Match rule, fixed in advance and applied identically to every mode
([replay/compare.py](../replay/compare.py)):

> same direction, same setup, |price| ≤ 2.0pt, |time| ≤ 60s, greedy nearest-in-time, **one-to-one**

One-to-one matters: without it a single replay alert in a busy minute claims every live alert around
it. Live alerts before the live process seeded are excluded from **both** sides — replay always runs
from 09:15, while 11/12 Aug started 09:20:01 and 13 Aug started 09:58:30 after a token expiry.

| day | live | close-only | OHLC |
|---|---|---|---|
| 11 Aug | 6 | 0/6 (3 fired) | 0/6 (3 fired) |
| 12 Aug | 24 | 0/24 (8 fired) | 2/24 (14 fired) |
| 13 Aug | 13 | 0/13 (1 fired) | 0/13 (10 fired) |
| 14 Aug | 13 | 0/13 (2 fired) | 3/13 (10 fired) |
| **TOTAL** | **56** | **0/56** | **5/56** |

14 Aug alone is **3/13**, not 10/13. Three of the four "matches" rev 3 printed fail rev 3's own
criteria: 10:30:42→10:30:44 is **5.0pt**, 15:11:05→15:11:14 differs in **setup**, 09:21:18→09:22:44
is **85s**.

**Only 4 sessions have live alerts at all** — 11–14 Aug. The "faithful 11" was *candle* coverage, not
alert coverage, so there was no untouched clean day to replicate on; all four were run, including the
three with known defects.

### Close-only is dead — and that part of rev 3 was right

0/56 strict, 20% ceiling, 1–3 alerts a day against 13–24 live. The mechanism was predicted, just not
its size: a five-minute bar assembled from minute **closes** has no wicks by construction, so the
range-derived gates cannot fire. At each of 14 Aug's 13 live alerts, **rejection blocked 11 and
pullback blocked 10**. Rev 2 called pullback "mildly affected"; it is not mild.

It is **kept as a selectable mode** ([replay/ticks.py](../replay/ticks.py)). 5/56 only means something
next to a 0/56 measured the same way, and every comparison this project got wrong, it got wrong by
losing its control.

### Why the near-miss log hid it — a standing limitation

Near-miss recorded only bars where **exactly one** gate blocked. Rejection and pullback fail
*together* under close-only, so rejection appeared **once**. The metric under-reported precisely the
gate that had broken the run.

Now fixed: every failing gate is recorded, with `blocker_count`, and the single-blocker view is a
filter rather than the only view. **The limitation generalises** — any two gates that become
correlated are invisible to a single-blocker metric, whatever the tick model.

### Why 9% and not 54% — and why the gap is not slack to reclaim

Two distinct causes, which rev 3 conflated:

1. **26 of 56 live alerts have no replay counterpart within 90s.** A real fidelity defect (§0b).
2. The rest fire near-simultaneously and fail on **price** — structurally:

```
live 12:43:52  57750.0   minute [57735.0-57783.6]   replay 57735.0  = the LOW
live 13:41:45  57761.0   minute [57730.0-57798.0]   replay 57730.0  = the LOW
live 09:33:20  57730.2   minute [57686.4-57749.0]   replay 57749.0  = the HIGH
```

A replay alert's price can only ever be one of the four O/H/L/C vertices; live's is whatever tick was
trading. On a 20–50 point bar, ±2pt is unreachable **at any minute-resolution tick model**. Loosening
the rule to close that gap would be setting the bar after seeing the result.

Ruled out as an explanation: live upgrade re-fires account for only 11 of 56 rows (56 raw → 45
distinct `(bar, direction)`; 35 of 45 groups are singletons).

### What the blocker dump found

The full failing-gate set at each of the 26, read off the completed near-miss record:

```
gate appearances  : pullback 33, rejection 29, volume 28, trend 22, level 4, open_interest 2
blockers per setup: {1: 15, 2: 11, 3: 15, 4: 9}
```

**No single systematic gate defect.** The divergence is distributed across the range-derived gates
*and* volume *and* trend, which means accumulated state drift rather than one broken rule. Notably
`trend` blocks 22 times and sometimes alone — an EMA slope over a 60s lookback sees a different sample
at 4 ticks/minute than at live tick density. There is no one fix, which is why §0b is tracked work
rather than a blocker on Phase 2.

### Correction to the rebuttal — kept from rev 3, and strengthened

"The gates evaluate bar state, not path" is true only of a **completed** bar. This system alerts
intra-bar, so gates read the forming bar's running high/low, and that partial state does depend on
tick order within the minute currently forming.

Rev 3 said fabrication is confined to a narrow, decaying window — true, and it understated the
consequence: because an alert fires at whichever synthetic offset carried a vertex, **the timestamp
and price stamped on a replay alert are themselves artifacts of the assumed ordering.** That is
precisely why price agreement fails by 5–35 points.

---

## 2. Models considered

| | Model 1: 5-min closed | **Model 2: OHLC expansion** | Model 3: 1-min open+close |
|---|---|---|---|
| Evaluations per 5-min bar | 1 | ~150 (2s cadence) | ~12 |
| Pullback / rejection | cannot fire | fires | under-fires |
| Volume projection | **never runs** | runs | functional |
| Latch / upgrade sequence | **cannot occur** | exercised | partial |
| Reproduction, 56 live alerts | n/a | **5 strict / 30 ceiling** | untested |
| close-only, measured | — | **0 strict / 11 ceiling** | — |

**Model 1 rejected — and not for coarseness.** It tests a *different system*. `elapsed_seconds` is
always 300, so the volume projection is silently bypassed; one evaluation per bar means no B→A→A+
upgrade sequence can ever occur. Validate the flip cooldown against Model 1 and it looks flawless —
because Model 1 never generates the rapid successive alerts the cooldown exists to suppress. A clean
pass would be false confidence in a mechanism that was never exercised. *This argument is about
mechanism and is untouched by §1's finding.*

**Model 3 rejected as dominated.** Kite does not return open/close without high/low in the same row,
so Model 3 needs the identical call and parsing as Model 2, then discards two of four real data points
for nothing, inheriting the missing-wick defect now measured at 0/56.

---

## 3. Prerequisites — all closed

| blocker | status |
|---|---|
| `enable_telegram` flag | done — `AlertEngine.__init__` |
| `bar_start` on `process_decision` | done |
| `SessionRunner` extracted, NiceGUI-free | done |
| `now=` passed by the live caller | **done** — `session_runner.py:256`, deployed |
| Dry-run mode | **done** — deployed, spec in `dry_run_section.md` |

Neither of the last two touched a hashed module. `engine_version` is **`b3620733`** and stays there
for all of Phase 1; a change means an edit landed in `signal_engine.py`, `decision_engine.py`,
`alert_engine.py` or a versioned constant by mistake.

---

## 4. Data

- **1-minute Kite candles with `oi=true`.** Confirmed working for NFO futures.
- **One day per request.** The documented 60-day window is unreliable in practice — one report of
  only ~4 trading days returning, and for an archive with a hard deadline a silent gap is the one
  failure that cannot be repaired later.
- Archive at **`archive/<tradingsymbol>/<YYYY-MM-DD>.json.gz`**, gzipped, written atomically via
  temp-and-rename, `.empty` markers for holidays. On hit, no API call.
- **Keyed on `tradingsymbol`, not `instrument_token`.** The token dies with the contract and becomes
  unresolvable; the symbol still means something afterwards. *(Rev 3 specified `replay_cache/<token>/`,
  which contradicted its own §5.)*
- Archived files are golden-test fixtures — written once, never silently rewritten. There is **no**
  checksum verification; rev 3 claimed one that was never built.

56 sessions archived, 27 May – 14 Aug, no gaps.

---

## 5. Contract window — implemented, and the boundary was wrong

Expired contracts are dropped from `kite.instruments("NFO")` entirely, and `historical_data` needs a
token, so a contract's candles become permanently unreachable once it settles. **BANKNIFTY26AUGFUT
settles 25 Aug 2026** — everything it has had to be on disk before then, and is.

**The front-month boundary is 29 Jul, not 31 Jul.** NSE index F&O expire on the **last Tuesday** —
verified, not assumed: 25 Aug 2026 is a Tuesday and the last Thursday is the 27th. That puts July's
expiry at Tue **28 Jul**, so the August contract became front month on **29 Jul**. The archive's OI
agrees independently: it ramps through 28 Jul (×1.29, to 2.18M) then sits flat near 2.1M from 29 Jul.

**The faithful corpus is 13 sessions, 29 Jul – 14 Aug** — not 11 from 31 Jul.

`assert_front_month()` ([replay/data.py](../replay/data.py)) refuses any day outside the window,
derived from the symbol alone so the check stays hermetic and keeps working after settlement. A *seed*
day outside the window **warns** rather than raises — that is the known "first session on a new
contract" hazard, where a far-month-thin volume baseline inflates relative volume all morning.

**A volume threshold would not have worked.** Over 22–30 Jul the August contract already traded
239k–876k while still the far month. Rev 3's "18k–70k far month against 240k–880k once front"
characterisation was wrong; the expiry boundary is the only clean signal.

**Do not use `continuous=true`.** Kite back-adjusts, shifting every historical price by an offset,
corrupting VWAP, the volume profile and S/R — the absolute levels the gates depend on.

---

## 6. Tick model

```python
TICK_MODE       = "ohlc4-distance-from-open-v1"   # default, pinned in the golden file
CLOSE_ONLY_MODE = "close-only-v1"                 # the control, kept deliberately
```

The name encodes the **ordering rule**, not just "ohlc4" — the ordering is what would silently change
every result if revised, so it belongs in the pin. *(Rev 3 called this `EXPANSION_MODE`; the constant
already existed as `TICK_MODE`.)*

Four ticks per minute at **+14s, +29s, +44s, +59s**. The last is +59s, not +60s: a +60s stamp pushes
the last minute of every five-minute bar into the next bucket, and the bar boundaries still look
right while the volume sits in the wrong bar.

**Ordering: distance from open.** High nearer the open than the low → O→H→L→C, otherwise O→L→H→C.
This is TradingView's broker-emulator heuristic. It is **not** "up candle → O→H→L→C" — that inverts on
exactly the trending bars that matter, since a strong up candle opens near its low.

**Volume: equal quarters**, the fourth absorbing rounding so the running total is exact at every
minute boundary. Not loaded onto the last tick: live volume accrues continuously, and loading it at
+59s leaves three ticks with zero delta and makes projected relative volume sawtooth — and the volume
gate is one of the six things replay exists to measure.

**`apply_tick` takes cumulative volume.** The stream carries a running session total matching
`KiteTicker`'s day-cumulative `volume_traded`. Per-minute figures produce near-zero deltas and a dead
volume gate, and **nothing crashes**. This is the failure most likely to go unnoticed.

OI from the minute candle applies to all four of its ticks.

### Fidelity statement

- **Exact — the inputs:** open, high, low, close, volume, OI of every minute.
- **Inferred:** order of O/H/L/C within the minute currently forming.
- **NOT exact — the outputs:** a replay alert's **timestamp and price** are artifacts of the assumed
  ordering, because the alert fires at whichever synthetic offset carried a vertex. Measured
  disagreement against live: **5–35 points**.
- **Unknowable at any resolution:** true tick path, order flow, sub-minute execution. One minute is
  Kite's floor and that ceiling is identical for every model considered.

Replay alert count is an **estimate**, not a lower bound.

---

## 7. Signatures — verified against source

```python
LiveFiveMinuteSession.apply_tick(price, cumulative_volume, oi, timestamp)
LiveFiveMinuteSession.advance(timestamp)
LiveFiveMinuteSession.snapshot()                       # no args, elapsed is bar-derived
SessionRunner.tick(now)                                # drives everything
SessionRunner(alert_engine=None)                       # kwarg exists, session_runner.py:64
```

---

## 8. Modules

```
replay/
  data.py      archive + hermetic cache + front-month assert
  ticks.py     minute candles -> tick stream, two modes
  driver.py    replay_day(); the loop; CLI
  outputs.py   alert + near-miss CSV writers
  compare.py   match rule, ceiling, blocker dump; CLI
  golden.py    freeze / check the frozen session; CLI
  golden/      the frozen files
```

Nothing under `replay/` is imported by live code. There is no `__main__.py`; each module carries its
own CLI.

---

## 9. `driver.py` — the loop

The loop drives `SessionRunner.tick()`. It does **not** reassemble signal → decision → alert: before
the 12 Aug extraction the engine existed twice, and rebuilding it here would make replay the fourth
copy — the thing the extraction removed.

- **Drain ticks before `runner.tick(t)`** so a tick stamped inside the window lands in its own bar.
- **Never read a candle later than `t`.** Lookahead is what makes a backtest look brilliant and a live
  system lose money.
- **`session.last_cumulative_volume = 0.0`** before the first tick, or the guard at `engine.py:270`
  drops the first tick's volume because there is nothing to diff against.
- **Session end 15:30** (`MARKET_CLOSE_*`), giving a last bar starting **15:25**. Kite returns 385
  minute candles running to 15:39, aggregating to 77 bars ending 15:35; the trailing ones are dropped.
  *(Rev 3 said matching `MARKET_CLOSE_*` would mismatch — inverted; matching it is what produces the
  wanted 15:25.)*
- **Seed from the ARCHIVE, not the live API.** Rev 3 called
  `fetch_session_five_minute_candles(kite, token, …)`, which needs a live token and would break §12's
  "a cache hit issues no API call". `build_seed()` derives the seed from archived prior-session candles
  instead. Same contract as live: EMA10 and the volume baseline only, never VWAP or the profile.
- **Assert `len(seed) == SEED_BARS` and refuse.** A short seed is the 11 Aug failure shape: nothing
  errors, output fires on schedule, and the volume gate measures against a one-bar average all day.
  Skipping short days is a policy someone must remember; asserting fails on the day it matters.
- **Fresh instances per replayed day.** The latch dict, the signal log's change key and
  `_last_decision_key` all carry state; leaking them silently swallows day 2's first alerts.
- **Refuses to run without `BN_DRY_RUN`.**

---

## 10. `outputs.py`

- `replay_alerts_<date>_<version>_<mode>.csv` — `alert_log.csv` columns plus `tick_mode`
- `replay_nearmiss_<date>_<version>_<mode>.csv` — bar_start, direction, setup, blocking_gate,
  `blocker_count`, `snapshots`, first/last seen

**Every row carries both `engine_version` and `tick_mode`.** Either one moving changes the output, so
a file recording only the first turns a tick-model change into an unexplained diff.

**Near-miss records ALL blockers**, not single-blocker bars. `blocker_count` recovers the old view as
a filter, which must never again be the only view.

**Rows collapse to one per `(bar, direction, setup, gate)`** with a `snapshots` count. Raw rows are
change-keyed, so a flickering gate produces far more of them than one blocking solidly — raw counts
are biased toward *unstable* near-misses.

**Summaries key on `(bar_start, direction, setup, gate)` — NOT `(bar_start, gate)`.** The latter,
which rev 3 specified, **saturates**: every bar evaluates a long and a short setup, at most one of
which can be valid, so trend blocks the other on essentially every bar. Measured on 14 Aug it put
pullback, rejection, trend and volume all at exactly **76** — the bar count — ranking nothing.

**A run spanning an `engine_version` boundary must refuse or split, never average.** Enforced
structurally: the golden path is keyed on the version, so changing a versioned constant makes the
comparison *impossible* rather than misleading.

---

## 11. Build order — two phases

The split is drawn on the one line this project already uses to tell the two kinds of work apart:

| | Phase 1 | Phase 2 |
|---|---|---|
| touches a hashed module | **no** | **yes** (`alert_engine.py`) |
| `engine_version` | **must stay `b3620733`** | **must move** — that is the proof it landed |
| changes what fires live | no | yes |
| reversible | entirely | needs a VM re-baseline |

Phase 1 is measurement and tooling: it can be wrong without costing anything, because nothing it
produces reaches a live session. Phase 2 changes what the tool tells you to trade on. Running them
together would mean a moving fingerprint could no longer distinguish "the cooldown landed" from "the
replay build leaked into the engine" — which is the entire value of the check.

### Phase 1 — tooling and the measurement record — **DONE**

0. Prerequisites closed; `b3620733` confirmed.
1. `data.py` + archive — 56 sessions, no gaps.
2. `ticks.py` — both modes, distance-from-open ordering, quartered volume.
3. `data.py` front-month assert — window 29 Jul – 25 Aug.
4. `driver.py` — the loop; near-miss records all blockers; mode selectable.
5. `outputs.py` — both CSVs.
6. `compare.py` — the match rule as repo code, plus the ceiling and the blocker dump.
7. **Blocker dump for the 26** — run, reported in §1. No systematic gate defect; Phase 2 unblocked.
8. **Golden file frozen** — 14 Aug, `b3620733` + `ohlc4-distance-from-open-v1`, 10 alerts.

**14 Aug is the golden day** because it is the only session at this version that ran clean: seeded
09:15:00.000362 with 36 prior-session bars, no restarts, no token expiry, ran to the close. 11 and 12
Aug started 09:20:01; 13 Aug seeded 09:58:30 after a token expiry cost it the first 43 minutes. 12 Aug
is worth a second golden file later — 24 live alerts, the largest sample.

**`id` is excluded from the frozen rows.** `AlertRecord.id` is a fresh `uuid4` (`alert_engine.py:141`),
so output is never byte-identical between runs. Rev 3's test 8 ("replayed twice is byte-identical")
cannot hold, and making it hold would mean editing a hashed module for a change that alters no rule.

### Phase 2 — the flip cooldown — **DONE**, `engine_version` now `c6635eac`

9. **Re-derive the cooldown's evidence** from `vm-csv/alert_log.csv` *before writing any code*.
    **DONE** — [replay/cooldown_study.py](../replay/cooldown_study.py), 16 tests. Results in §15.
    The log holds **56 Trading rows + 1 Error** (13 Aug's token failure), 29 Short / 27 Long, and
    **18** same-day direction flips, all stamped `b3620733`.
10. **Shipped** — **5 minutes** (not 12, see §15), `("A","A+")` escaping. Constants live in
    `alert_engine.py` beside `ALERT_GRADE_RANK` so `engine_version()` fingerprints a retune
    automatically. The check sits **between the latch's read and its write**: writing the latch first
    would let a *withheld* alert raise the bar's ceiling, so a genuine upgrade later in the same bar
    would be compared against a grade that never went out and silently dropped. `_last_alert`
    advances only on delivered alerts — a withheld one must not extend its own window. Resets on date
    change. `FLIP_COOLDOWN_MINUTES = 0` restores pre-cooldown behaviour exactly.
11. **Withheld alerts are recorded, never silently dropped** — `csv/suppressed_log.csv`, a separate
    file from `alert_log.csv`. Mixing undelivered rows into the alert log would change every count
    ever taken from it. Since the cooldown ships *without* evidence that suppressing helps, this file
    **is** the evidence collection: every withholding records what blocked it and by how much, so the
    question is answerable from data later rather than re-argued.
12. **Acceptance test — passed.** The Phase 1 golden file fails with exactly one row:

    ```
    BN_DRY_RUN=1 python -m replay.golden check --against b3620733
      -  09:56:44 B   Long  vwap      57770.0
    ```

    One alert withheld, none added — precisely what §15 predicted. This required adding an explicit
    `--against VERSION` flag: the default keying makes cross-version comparison *impossible* (§10),
    which is right everywhere except this one controlled case, so it must be asked for by name.
13. **Re-baseline** — golden file re-frozen at `c6635eac` (9 alerts). Still to do: update
    `vm_handover.md` §6 and re-baseline on the VM.

**A consequence worth stating.** The live log is a fixed record produced by `b3620733`; the engine is
now `c6635eac`. So §0b's reproduction score is a cross-version comparison, and 14 Aug fell from
3/13 to 2/13 purely because the cooldown withholds an alert that *had* matched live — not because
fidelity worsened. `replay.compare` now prints a **VERSION MISMATCH** banner rather than letting that
be quoted as a fidelity figure. Warned rather than refused, because the comparison remains useful.

Separately, blocking neither phase: the rest of the §0b fidelity investigation.

### Verification

```bash
BN_DRY_RUN=1 python -m pytest -q                             # 271 passing
python -c "import engine; print(engine.engine_version())"    # b3620733 in Phase 1; MOVES at step 10
BN_DRY_RUN=1 python -m replay.driver --date 2026-08-14
BN_DRY_RUN=1 python -m replay.compare --days 2026-08-11 2026-08-12 2026-08-13 2026-08-14 --blockers
BN_DRY_RUN=1 python -m replay.golden check
```

`csv/` mtimes must not change — every write belongs in `csv-dryrun/`. **A moved fingerprint in Phase 1
is a stop condition, not a re-baseline.**

---

## 12. Tests — 271 passing

1. Cumulative volume monotonically non-decreasing, both modes
2. Quarter split sums to the minute candle's volume exactly, at every boundary
3. Ordering: high-closer-to-open → O→H→L→C; low-closer → O→L→H→C; ties to the high
4. A minute's OI carried by all four of its ticks
5. No tick price outside its candle's real range, both modes
6. Ticks land in their own five-minute bucket; post-close candles dropped (375 / 1500)
7. `last_cumulative_volume = 0.0` priming — the first tick's volume is counted
8. Seed bars reach EMA and the volume baseline but **not** VWAP or the profile
9. Two consecutive days in one process produce independent alert sets
10. Same day replayed twice agrees on every field **except `id`** — see §11
11. No tick with `timestamp > t` is ever applied
12. A full `replay_day` writes nothing under `csv/` and sends no Telegram
13. Alert timestamps come from `t`, not the wall clock
14. Cache hit issues no API call
15. Date outside the front-month window → assert fires; faithful corpus is 13 sessions
16. Match rule: price, setup, time and direction each reject; grade does not; one-to-one holds
17. Near-miss: a correlated gate is visible in the all-blockers view and invisible to the sole filter
18. Golden file matches; a versioned-constant change makes it **refuse** rather than mis-compare

---

## 13. Not included, ever

Entry / SL / T1 / T2 (not carried by `Decision`); outcome measurement of any kind; parameter sweeping;
P&L simulation; order placement; tick recording.

---

## 14. What replay can and cannot answer

**Can, on the 13-session faithful corpus:**
- What would this rule change have fired, relative to what the current rules fire?
- Which gate blocks most often, and by how much — using the all-blockers view.
- Where in the bar do alerts land?

**Cannot:**
- Reproduce the live alert stream (§0b): 9% strict, 54% ceiling.
- Anything wanting ~20 sessions, including flip-cooldown validation at scale. The corpus grows one
  session per trading day and resets at each rollover unless candles are archived as they trade.

**Live-log status, corrected.** Rev 3 said the repo held 34 alerts — 33 Short, 1 Long, zero flips —
from before the VWAP fix, and that the newer dataset was absent. Both wrong: `vm-csv/alert_log.csv`
holds **56 Trading rows plus 1 Error across 11–14 Aug, 29 Short / 27 Long, 18 same-day direction
flips**, all stamped `b3620733`. It is in the repo and it is what §15 uses.

*(An intermediate note in this review said "57 Trading rows, 19 flips". That counted the Error row —
13 Aug's `Incorrect api_key` stream failure — as an alert, and turned its blank direction into a
nineteenth flip. 56 and 18 are the figures.)*

---

## 15. Flip cooldown — what the live log actually says

Re-derived by [replay/cooldown_study.py](../replay/cooldown_study.py) over the 56 Trading alerts, 16
tests. Rule modelled exactly as §11 step 10 specifies it, including `_last_alert` advancing only on
delivered alerts.

**The planning figure reproduces.** 12 min, flip scope, `("A","A+")` escaping → **4 of 56
suppressed**. Unlike the 10/13, this one survived re-derivation.

**But the figure that was never stated is the one that matters: it catches 4 of 18 flips — 22%.**
The mechanism exists to damp direction flips and leaves fourteen of them untouched, because 10 of the
18 are graded A or A+ and escape by design.

### The rule reduces to something narrower than its name

With `("A","A+")` escaping, every suppressible flip is B-grade. The cooldown is therefore exactly
*"suppress B-grade direction flips arriving within N minutes of the last delivered alert"* — and it
catches 4 of the 8 B-grade flips.

### Duration has almost no leverage, and 12 minutes is the least stable choice in the range

Suppression count against cooldown length, flip scope:

```
 1m  0/56           11m  4/56  <- step        23m  6/56  <- step
 2m  2/56  <- step  12m  4/56   (proposed)
 3m  3/56  <- step  13m-16m  4/56             29m  7/56  <- step
 4m-10m  3/56       17m  5/56  <- step
```

The eight B-grade flip gaps are **1, 2, 2, 10, 11, 12, 17, 29** minutes — clustered at the bottom,
then a hole. So there is an **8-minute plateau from 3m to 10m** where every value gives an identical
result, and **12m sits one minute past a step edge**, in the densest part of the distribution. A
single alert arriving thirty seconds differently changes the outcome at 12m; nothing changes it
anywhere from 3m to 10m.

**5 minutes is the defensible constant** — mid-plateau, three minutes clear of the step below and
five clear of the step above. That was the original instinct in the 15 Aug discussion; the 12-minute
recommendation that replaced it put the threshold on a cliff.

### Scope is undecided, and matters more than duration

"Flip cooldown" is ambiguous, and the readings diverge:

| at 12 min | suppressed | flips caught |
|---|---|---|
| `flip` — only on direction change | 4/56 | 4/18 |
| `any` — any alert in the window | 7/56 | 4/18 |

`any` removes three additional *same-direction* alerts and catches no extra flips. The name says
`flip`; nothing in the log argues for `any`.

### What this study cannot say

**Whether suppressing those alerts is an improvement.** That needs outcome data, and
`review_session.py` is parked pending the futures-points-vs-option-P&L check. This measures what a
cooldown removes, not whether removing it helps. Shipping on this evidence means accepting a
mechanism justified by plausibility rather than measured benefit — the same standing the
mean-reversion finding had before it was tested.

---

## Appendix A — decision record

Why each open question was resolved the way it was. Kept because the reasoning, not the conclusion, is
what a later reader needs in order to overturn one of these safely.

**Q1 — What does §0's acceptance criterion become?** Split into §0a/§0b rather than weakening one
criterion. The alternative — redefining acceptance as the 54% ceiling — was rejected because it sets
the bar after seeing the result. The cost of the split is a written admission that replay does not
reproduce live alerts; that admission is the correct output of the measurement, and burying it is how
the mean-reversion mistake happened.

**Q2 — Golden file day, and does it still gate the cooldown?** 14 Aug, self-referential, ordering
kept. Under §0a the freeze needs no fidelity investigation, so it stopped being a long pole. The
ordering is kept not because the inputs expire — both sides are permanently archived — but because
freezing first turns the cooldown's arrival into its own acceptance test.

**Q3 — Do we chase the 26 missing alerts?** The full investigation is separate tracked work, but its
first step ran *before* Phase 2, in case it revealed a systematic gate defect: shipping a cooldown onto
a gate that is already wrongly blocking would stack two suppressions and make the second much harder to
attribute. It did not — see §1.

**Q4 — Keep close-only or delete it?** Keep, as a selectable mode. **The principle generalises: keep
the losing arm.** Deleting it destroys the evidence for the switch, and every comparison in this
project that went wrong went wrong by losing its control — overlapping windows had no control, the OI
study's control moved the effect from +0.195 to +0.027, and the 10/13 figure had no control at all.

**Q5 — The front-month assert did not exist.** Built (§5). It was the one genuine unimplemented safety
item in rev 3.

**Q6 — `TICK_MODE` or `EXPANSION_MODE`?** Keep the existing constant name, take rev 3's better value.

**Q7 — Where does the comparison code live?** `replay/compare.py`. Every number in rev 3 came from
throwaway scripts that no longer existed — which is exactly the criticism that applies to the 10/13
figure the revision was built on.

**Q8 — Where does the cooldown's own evidence come from?** `vm-csv/alert_log.csv`, re-derived before
shipping (§11 step 9). Same provenance problem, now checkable.
