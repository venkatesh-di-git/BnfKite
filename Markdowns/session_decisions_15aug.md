# BN Smart Assistant — Session Decisions (15 Aug 2026)

Hand this to a fresh context to resume. Everything below was decided or measured
in this session; nothing is speculative unless marked.

**Amended later the same evening.** Four items were completed after this was first
written and are marked ✅ DONE in place — do not rebuild them. Two statements were
wrong and are corrected in place, in §3 and §5. The amendments are the only
authority where they disagree with the surrounding text.

---

## 1. Data archive — DECIDED, and it is time-critical

**Decision: archive daily, keep everything, no retention limit.**

Sizing settled it: ~375 rows per session, 40–60 KB as JSON, under 10 KB gzipped.
~1 MB/month, ~12 MB/year. Storage is never the constraint, so a retention policy
would only recreate the "not enough sessions" wall hit repeatedly this month.

**Why daily, not a batch pull at expiry:**
- Kite's documented minute window is 60 days per request, but there is an
  unresolved report of only ~4 trading days returning per request. A bulk pull
  would silently return partial data with no way to detect the gap.
- The contract expires on the 25th. A batch job that fails that day loses the
  entire cycle permanently — the token drops out of `kite.instruments("NFO")`
  the moment it settles, so there is no retry.
- Daily has no cliff: miss a day, fetch it tomorrow, still within the
  front-month window.

**Shape:** one call after close, keyed `(date, tradingsymbol)`, gzipped, never
deleted. Include a gap-check that reports missing dates so a silent failure
surfaces next day rather than at expiry.

**DO THIS BEFORE 25 AUGUST:** backfill 31 Jul – 14 Aug in one shot. That is the
current cycle's history and it becomes permanently unreachable at rollover.

### ✅ DONE, 15 Aug — and wider than specified

Backfilled **all 56 sessions, 27 May – 14 Aug**, not just the 11 front-month ones.
The faithful-corpus boundary is a *replay* constraint, not an archive one, and §1's
own principle is keep everything: the far-month portion is the only record of it
that will ever exist. Cost of the extra 45 sessions: about 350 KB.

```
BANKNIFTY26AUGFUT   56 sessions   444K   no gaps
375 candles/session before 03 Aug, 385 after; OI non-null on all but 3 rows
```

Built as **`replay/data.py`**, which is deliberately both the archive *and* the
replay engine's data source — §1 here and §5 of the replay spec described the same
artifact twice, and two implementations would mean two copies of every candle and
two gap checks that drift. This therefore also completes step 1 of §6's build order.

Keyed on **tradingsymbol**, not instrument_token, per §2's reasoning: the token
stops resolving the moment the contract settles.

Re-running fetches nothing (56 cached, 0 API calls), so it is safe as a daily job.
Copies on both the VM and Windows via `deploy/pull-archive.sh` — the VM is a
trial-tier e2-micro and must never hold the only copy. `deploy/sync.sh` now also
carries `replay/*.py`, and deliberately does **not** sync `archive/` in either
direction, so a partial VM copy can never overwrite the durable one.

**Still to do:** the daily timer (`kite-archive.timer`, after the close, modelled
on `kite-healthcheck.timer`). The backfill was the deadline; the timer has days of
slack because any missed day can be re-fetched inside the front-month window.

---

## 2. Why the archive is necessary — the contract problem

Verified against the API this session, not assumed:

- `BANKNIFTY26JULFUT` → **empty result**. Expired contracts are dropped from the
  instrument master entirely.
- `BANKNIFTY26AUGFUT` → resolves, token 14865154, `active: true`.
- Every instrument returned carries `active: true`. The master holds live
  contracts only.

Since `get_historical_data` takes a token and there is no way to obtain an
expired one, **there is no route to July's candles.**

**The faithful corpus is 31 Jul – 14 Aug — 11 sessions**, growing to ~17 by
expiry. Before 31 July, August was the *far* month (18k–70k contracts/session vs
240k–880k once front). Those candles exist and are fetchable, but they reproduce
a session the engine never saw — the volume gate would compare against a
far-month baseline, the volume profile would be built from thin trade, and OI
would be moving for rollover reasons rather than conviction. Nothing errors; the
output is confidently wrong.

**Rejected:** `continuous=true`. Kite's version back-adjusts, which shifts every
historical price by an offset. The engine reads VWAP, volume profile and S/R —
absolute price levels — so back-adjustment corrupts exactly the inputs the gates
depend on. Industry guidance splits on this: adjustment on for algorithmic
backtesting, off for anything reading real historical levels. This project is
the second case.

**What the archive actually builds** is a non-back-adjusted continuous series:
real prices, correct instrument per date, gaps at rollovers. The rollover gaps
that make this bad for multi-year backtests are irrelevant here — replay runs
one session at a time and never spans a roll.

**Rollover boundary should be volume-based, not calendar-based** — matches both
the existing rollover addendum and industry practice. Worth measuring where
volume actually crossed in late July; may add 2–3 usable sessions per cycle.

**Free-data alternatives investigated and rejected:** OpenChart
(marketcalls/openchart) resolves F&O by symbol rather than broker token and
supports 1-minute data, so it may sidestep the expiry problem — but its output
is OHLCV only, **no OI field**, and OI is one of the six gates. Shoonya's free
dataset is options-only, Bank Nifty from Feb 2026. StockMojo is options chain,
no bulk download. The GitHub NSE-Data repos are equities/indices 2017–2020.

---

## 3. Regime detection — CLOSED after sixteen attempts

**Do not open this again without new evidence.** Every attempt to detect regime
from bar geometry, participation, or positioning has failed.

Tested and negative (all non-overlapping windows, per-session, 10 sessions):

| Test | Result |
|---|---|
| VWAP distance / rolling stdev | 2/10 sessions positive, pooled −0.24 |
| VWAP distance / rolling ATR | 4/10 positive, pooled −0.15 |
| ADX (14, simplified) | pooled +0.06, per-session −0.72 to +0.41 |
| OI z-score → \|forward move\| | ~~8/10 positive, pooled +0.39~~ — **superseded, see below** |
| OI-elevated + direction → continuation | 3/10 positive, pooled −0.21 |

Plus the earlier eleven: 15-min EMA slope, opening range, prior-day value area,
grade filters, session bias, overlap ratio, value-area width, coverage-as-
predictor, relative volume, volume trend, volume concentration.

**Mean reversion is WITHDRAWN.** It looked real (39–46% continuation at every
horizon, negative on all 7 sessions) and collapsed under non-overlapping
windows: 54% continued, +3.9 median — opposite sign. Overlapping windows share
5 of 6 bars, so one reversal was counted six times. 460 "observations" were
really ~56. A 44-session re-run confirmed only one cell (L=3/F=1) survives, and
it fails horizon and lookback consistency — noise, not effect.

**Standing rule for any future test: non-overlapping windows, reported per
session, sign consistency checked before pooled correlation.**

### OI — re-run properly, 15 Aug. Confirmed dead.

The +0.39 above was the same overlap artifact as mean reversion. Re-run on **56
sessions, 224 non-overlapping windows** (L=12, F=6, stride 18 — a shorter baseline
buys 4 windows per session instead of 2, which is worth more than 40 extra
sessions), with the required control:

| | r | 95% CI |
|---|---|---|
| corr(z, forward) | +0.058 | [−0.07, +0.19] |
| corr(\|z\|, forward) | **+0.029** | [−0.10, +0.16] |
| corr(range, forward) — control | **+0.027** | [−0.10, +0.16] |

OI and the control are indistinguishable, and both cross zero. It also fails at
L=20, where the control *wins* (+0.216 vs +0.106), and the 5/7 block consistency
is p=0.23.

The overlapping run on the same data is the clean demonstration of the mechanism:
it inflates the **control** from +0.027 to +0.195, CI [+0.16, +0.23]. A variable
known to be nothing but volatility clustering looks decisive once each event is
counted eighteen times.

One residue, explicitly not a result: in the 28 highest-volume sessions,
`corr(|z|, fwd)` is +0.160 against a control of +0.006 — the only place OI beats
it. CI [−0.03, +0.34], crosses zero, and roughly ten cells were examined. If ever
revisited, test it **prospectively** on the September contract once it goes front
month, not by re-slicing this sample.

---

## 4. Coverage — works as a label, FAILS as a gate

**The methodological correction that produced this:** all sixteen prior tests
used forward-looking labels ("does X predict what happens next"). That was never
the assignment. The actual question was backward-looking classification: is the
last 30 minutes choppy, right now.

```
coverage = sum(high[i] - low[i] over last 6 bars) / (max(high) - min(low) over those 6)
```

**As a descriptive label — 75% agreement with blind human judgment:**
- Batch 1 (12 stretches, 4 sessions): 10/12 (83%)
- Batch 2 (12 stretches, 6 held-out sessions, threshold fixed at 2.30
  in advance): 8/12 (67%)
- Combined: 18/24 (75%)

Batch 2's four misses were all the same direction (predicted chop, actual trend)
and all the same time slot (~12:45) — a possible early-afternoon blind spot,
untested.

Method note: blind labelling is what makes this valid. Charts rendered with no
dates, times or metric values; human calls chop/trend first; metrics revealed
only afterwards.

**As an alert gate — disqualified.** Applied to the 34 historical alerts at
threshold 2.30: **53% suppressed, including 4 A+ and 6 A.** Same failure shape
as the 15-min EMA slope filter that deleted all three 12 Aug winners.

**Mechanism:** coverage measures bar overlap, and a pullback *is* overlapping
bars. The strategy is pullback-entry, so coverage cannot distinguish the setup
from the noise it is meant to remove.

**Verdict:** fine as a logged context column. Not a filter.

---

## 5. The alert log in the uploaded zip is pre-VWAP-fix

34 trading alerts, **33 Short, exactly one Long, zero direction flips.** This
predates the VWAP fix, from when the Trend gate made Long structurally
unreachable.

**Consequence:** the 56-alert statistics referenced in planning came from a later
dataset. ~~That dataset is not in the project.~~

### Corrected, 15 Aug — both halves of this were wrong

**The newer log IS in the project**, at `vm-csv/alert_log.csv`, pulled from the VM
the same day: 56 trading alerts across 11–14 Aug, **29 Short / 27 Long**, nothing
like the zip log's 33:1 skew. Cooldown validation is not blocked on a missing
dataset. (Also: 18 direction flips total, of which 11 are under 15 minutes — the
"11 flips" figure was fast flips, not all flips.)

**And "replay will mostly reproduce the pre-fix era" is backwards.** Replay runs
**today's** engine over old candles — that is the entire point of it. What the log
happened to contain at the time does not propagate into replay output. Replaying
June would produce what the *current, post-VWAP-fix* engine would have fired on
June's tape.

The real limit on replaying June is the one in §2 — it was a far-month instrument
then — not the engine version.

---

## 6. Replay engine — build order

Spec: `replay_engine_spec_close_only.md` — now **Rev 3**, a single consolidated
file. ✅ `bn_replay_engine_spec.md` deleted and the supersession line fixed.

**Two open prerequisites — ✅ BOTH CLOSED, 15 Aug. Do not rebuild these.**

1. **Dry-run mode** — ✅ implemented. `BN_DRY_RUN` read once at import in
   `config.py`, redirecting `CSV_DIR` to `csv-dryrun/`; `session_runner.py` wires
   `enable_telegram=not config.DRY_RUN`; mode shown on `main.py`'s *iteration*
   line, not just at startup. 17 tests in `test_dry_run.py`. Deployed 22:03.

   One addition beyond the spec: `config.LIVE_OUTPUT_FILE`, always the project-root
   heartbeat whatever the flag says, which `healthcheck.py` now stats. Without it
   the guard depended on `healthcheck.py` running as a separate systemd unit that
   happens not to inherit the scanner's environment — set `BN_DRY_RUN` in
   `~/.profile` and the probe would have watched the file the dry run was
   refreshing. Every check green, nothing delivered: the 10 Aug shape exactly.
2. **`now=`** — ✅ done, at `session_runner.py:256` (not 243; comments shifted it).
   Passes `now.replace(tzinfo=None)`; the tz strip keeps `alert_log.csv` timestamps
   naive, as every existing row is.

Neither touched a hashed module. `engine_version` verified **`b3620733`** on both
machines afterwards, which is the evidence they landed where intended.

**Then, in order:**
0. ~~Clear the two prerequisites~~ ✅ done
1. ✅ **`data.py` + cache — done**, as `replay/data.py`; it is the archive from §1.
   56 sessions on disk, OI non-null, hermetic reads via `allow_api=False`
2. `ticks.py` — close-only, one tick per minute
3. `driver.py` — calls `SessionRunner.tick()`, never rebuilds the loop
4. `outputs.py` — alerts + near-miss CSVs
5. **Golden file — freeze BEFORE shipping the cooldown.** The cooldown moves
   `engine_version` off `b3620733`; the cross-check compares against 14 Aug live
   alerts generated at `b3620733`. Ship the cooldown first and you are diffing
   two engines and calling it a replay defect.

**Settled sub-decisions:**
- **Session end: 15:25**, not 15:35. Every hand analysis this week trimmed
  post-close bars; matching `MARKET_CLOSE_*` would silently mismatch every
  cross-check on the hardest bars to notice.
- **Seed warm-up: assert and refuse**, not skip. `assert len(session.seed) ==
  SEED_BARS`. Skipping N sessions is a silent policy someone has to remember;
  asserting fails loudly on the day it matters.
- **Contract resolution:** resolve once at replay start, hard-assert the date
  range is inside the front-month window. Not a per-date map — only one cycle is
  ever replayable without the archive.
- **Tick model: close-only, one per minute.** Confirmed not synthetic. Every price
  fed actually traded. (Note: the older spec's synthetic rule was also wrong on its
  own terms; TradingView's heuristic is distance-from-open, which inverts on
  trending bars.)

  **But "a floor you can trust" is too strong** — corrected against Rev 3 §2, which
  traces it field by field. **Exact:** close (the last minute-close in a window *is*
  the 5-minute close), volume, OI, EMA10. **Narrowed:** high/low, since
  intra-minute extremes that never became a close are invisible. **Approximate:**
  open, and VWAP — `session_price_volume / session_volume` accumulates `price ×
  volume` per tick (`engine.py:240`), so close-only yields a minute-close-weighted
  VWAP rather than a trade-weighted one.

  The floor therefore holds **only for the range-derived gates** — rejection and
  the POC/VAH/VAL proximity checks can only undercount. It does *not* hold for
  Trend, which needs close on the correct side of VWAP: that error has no
  guaranteed sign, so replay can in principle invent an alert. Distrust any replay
  alert sitting within a point or two of VWAP.
- **Read `rejection` counts sceptically** — wicks are intra-minute extremes,
  exactly what close-only discards. Volume and OI unaffected and exact; Trend
  affected only through VWAP, per above; pullback mildly.
- **Near-miss: no action needed.** `GateResults.failed` is a property and
  `Decision` carries all four gate sets. `record_nearmiss` works as written.

**Corpus reality:** Q1–Q3 answerable on 11 sessions. Q4–Q6 want ~20 and will not
be answered this cycle without the archive.

---

## 7. Still parked

- **`review_session.py`** — parked pending a one-off check of whether futures
  points track option P&L. That check is itself deferred until the tool moves
  from alerts to placing trades. Consequence to carry consciously: every outcome
  number in the project is futures points, on a corpus that is mostly far-month.
- **ATM strike logging** — considered and dropped. Speculative infrastructure
  for a test not committed to. One line to add later if the CE/PE question
  reopens.
- **Flip cooldown** (12 min, A/A+ escapes) — still the only intervention that has
  survived measurement. Ship as config with a `suppressed_by_cooldown` marker
  rather than a silent drop. Validate on replay, after the golden file.
- **HMM / Markov regime engine** — rejected for now. Same inputs as sixteen
  failed detectors, 11 usable sessions, judged in an instrument you do not trade.
  Its *evaluation protocol* (base vs base+X, walk-forward, remove if it does not
  earn its place) is worth keeping and applying to the cooldown.

---

## 8. Housekeeping

- Delete `__pycache__/` (contains an orphaned `temp.py.pyc` for a deleted file)
  and `.pytest_cache/`; confirm both are gitignored.
- Delete `Markdowns/bn_replay_engine_spec.md`.
- Move `timer_probe.py` out of the import root.
- `csv/` rotated logs: archive outside the zip rather than shipping every time.
- Entry points still have zero test coverage — `app.py`, `main.py`,
  `healthcheck.py`, `token_check.py`, `kite_auth.py`. Both incidents this month
  lived there.
