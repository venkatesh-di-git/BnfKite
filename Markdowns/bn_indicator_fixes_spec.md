# Implementation Spec — BN Smart Assistant Indicator Fixes

**Project:** `Bnf_kite`
**Contains two independent implementations.** Ship them as separate commits.

| | Part | Fixes | Files touched |
|---|---|---|---|
| **A** | Projected bar volume + bar-derived elapsed | Volume signal biased by position within the bar; 300× false spike at every bar close | `config.py`, `engine.py`, `signal_engine.py`, `app.py`, `main.py` |
| **B** | Prior-day seeding for EMA10 and volume baseline | EMA10 and SMA20-volume both start cold at 09:15 every session | `config.py`, `engine.py`, `app.py`, `main.py` |

---

## How the two parts relate

They are **independent** — either can ship alone and the suite stays green.

They are **not order-neutral in effect**. Part A computes
`projected_volume / sma20_volume`. Part B fixes what `sma20_volume` *is* during
the first 100 minutes of a session. Running A without B means the projection is
accurate but measured against a baseline built from as few as one bar.

**Recommended order: B, then A.** B is smaller, is a plain defect fix, and
establishes a trustworthy denominator before A starts dividing by it.

**Two shared touch points** — if implementing both, expect these to merge:

1. `LiveFiveMinuteSession.snapshot()` — A adds `elapsed_seconds`; B changes what
   `average_bar_volume` draws from. Different keys in the same returned dict.
2. `_average_recent_volume()` — B rewrites it. A only consumes its output.

No conflict beyond ordinary merge mechanics.

---
---

# PART A — Projected Bar Volume with Bar-Derived Elapsed Time

**Scope:** Volume signal only. No other signal, engine, or UI behaviour changes.
**Sub-changes A1 and A2 MUST ship together** — see A.2.

## A.1 Background — what exists today

`SignalEngine._evaluate_volume()` compares the **forming** bar's raw accumulated
volume against the average volume of completed bars:

```python
# signal_engine.py, current
def _evaluate_volume(self, candle, ind):
    if candle is None or not ind.sma20_volume:
        return VolumeState.UNKNOWN, "Insufficient data for Volume"
    relative_volume = candle.volume / ind.sma20_volume
    if relative_volume >= config.VOLUME_HIGH_THRESHOLD:      # 1.30
        return VolumeState.HIGH, ...
    if relative_volume >= config.VOLUME_LOW_THRESHOLD:       # 0.80
        return VolumeState.NORMAL, ...
    return VolumeState.LOW, ...
```

This compares a **partial** quantity against a **full-bar** average, so the
reading is biased by position within the bar. Same bar, same eventual volume,
`sma20_volume = 100_000`:

| Point in bar | Raw volume | Ratio | Signal |
|---|---|---|---|
| 1/5 | 20,000 | 0.20 | Low |
| 3/5 | 60,000 | 0.60 | Low |
| 4/5 | 80,000 | 0.80 | Normal |
| 5/5 | 100,000 | 1.00 | Normal |

Because `Volume ∈ {High, Normal}` is a **mandatory gate** in
`DecisionEngine._mandatory_conditions_pass()`, `ENTRY` is structurally
near-impossible during the first ~3 minutes of every bar. Setups that form and
fade early are never surfaced — a false-negative class the user cannot see.

**Fix (A1):** project the forming bar's volume to a full-bar equivalent before
comparing.

## A.2 Why A1 and A2 are one change

Projection needs an "elapsed seconds" input. If that input comes from the wall
clock, it produces a **300× false spike at every bar boundary**.

`LiveFiveMinuteSession.advance()` is the function that closes a bar:

```python
# engine.py, current
def advance(self, timestamp):
    with self._lock:
        if self.current and self.current.start < five_minute_start(timestamp):
            self._finish_current()
            self.current = None          # <-- no forming bar
        self.last_advance_at = timestamp
        self._record_ema_sample(timestamp)
        return self.snapshot()           # <-- snapshot taken with current = None
```

And `snapshot()` falls back to the last completed bar:

```python
current_bar = self.current if self.current else (self.completed[-1] if self.completed else None)
```

So the snapshot returned by *the very call that closes the bar* carries the
**completed** bar as `current_candle`. A clock-derived elapsed reads ~1 second,
because `five_minute_start(now)` has just rolled over:

```
projected = 100_000 × (300 / 1) = 30_000_000   ->  relative volume 300.00  ->  HIGH
```

This is **deterministic, not a race**. In `app.py`:

```python
line 362:   state.update(state["live_session"].advance(now))   # 1s timer
line ~404:  evaluate_signals()                                 # same tick, reads that state
```

`state["current_candle"]` is written **only** by `advance()` — the ticker's
`on_tick=session.apply_tick` return value is discarded — so a tick arriving
0.001s after rollover cannot prevent it. Expect ~75 occurrences per session.

It also lands at the worst moment: a just-closed bar has final OHLC, so Trend /
Pullback / Rejection pass on their genuine merits, leaving the volume gate as
the only thing that could withhold the alert — and that is exactly the reading
that is 300× too high.

The `0.25 × sma20` floor does **not** catch this: the floor rejects bars with
*too little* volume, and this bar carries a full bar's worth.

The same defect covers the **feed-stall** case, where `advance()` keeps running
on the 1s timer with no ticks arriving, and `current_candle` remains a finished
bar for the whole stall.

**Therefore: never ship projection without bar-derived elapsed.**

## A.3 Changes

### A.3.1 `config.py` — add three constants

Place with the other Signal Engine tuning constants:

```python
# Nominal bar length in seconds — the projection target for the Volume signal.
BAR_SECONDS = 300.0

# Absolute traded-volume floor for projection, as a fraction of sma20_volume.
# Below this, too little has actually traded to extrapolate from and the Volume
# signal reports Unknown rather than a noisy multiple of a tiny number.
VOLUME_PROJECTION_FLOOR_PCT = 0.25

# Minimum elapsed seconds before projecting at all. Guards the opening moments
# of a bar, where the multiplier is large enough that a single tick dominates.
# Set to 0.0 to disable this guard and rely on the volume floor alone.
VOLUME_PROJECTION_MIN_ELAPSED_SECONDS = 30.0
```

`VOLUME_HIGH_THRESHOLD` (1.30) and `VOLUME_LOW_THRESHOLD` (0.80) are unchanged —
they now apply to the *projected* ratio.

### A.3.2 `engine.py` — emit `elapsed_seconds` from `snapshot()`

`snapshot()` is the only code that knows whether `self.current` exists, so it is
the only correct place to derive elapsed. Inside `snapshot()`, within the
existing `with self._lock:` block, alongside the existing `current_bar` /
`current_candle` derivation:

```python
# Elapsed is derived from the bar being handed out, NOT from the wall clock.
# When advance() has closed a bar and nulled self.current, current_candle is a
# COMPLETED bar — it is a full BAR_SECONDS old by definition, so the Volume
# signal's projection multiplier must collapse to 1.0. A clock-derived elapsed
# would read ~1s here and inflate a finished bar by up to 300x.
if self.current is not None and self.last_advance_at is not None:
    elapsed_seconds = (self.last_advance_at - self.current.start).total_seconds()
    # Clamp: negative guards a clock step-back; the upper bound guards a bar
    # that outlives its own window during a feed stall.
    elapsed_seconds = min(max(elapsed_seconds, 0.0), config.BAR_SECONDS)
else:
    elapsed_seconds = config.BAR_SECONDS
```

Add to the returned dict:

```python
"elapsed_seconds": elapsed_seconds,
```

**Note on `last_advance_at`:** `apply_tick()` also returns `snapshot()`, and on
that path `last_advance_at` may lag by up to the advance cadence. This is
acceptable because the returned value is discarded in `app.py`, and `main.py`
reads via `advance()`. Do **not** substitute `datetime.now()` here — that
reintroduces the clock dependency this change exists to remove. If a fresher
value is ever needed, use the current bar's own last tick timestamp, not the
wall clock.

### A.3.3 `signal_engine.py` — add the field and rewrite the volume rule

Add to `IndicatorSnapshot` (keep the default so existing tests still construct
it without the new field):

```python
    # Age of the candle being evaluated, in seconds. Supplied by
    # engine.snapshot(); BAR_SECONDS for a completed bar. Falls back to
    # BAR_SECONDS (no projection) when absent.
    elapsed_seconds: Optional[float] = None
```

Replace `_evaluate_volume()`:

```python
    def _evaluate_volume(self, candle: Optional[Candle], ind: IndicatorSnapshot):
        """Relative volume on a PROJECTED full-bar basis, so a bar is judged on
        its participation rate rather than on how far into it we are.

        Two guards stop a small numerator being multiplied into a fake spike:
        an absolute floor on volume actually traded, and a minimum elapsed time.
        Both report Unknown, which fails the Decision Engine's mandatory Volume
        gate — early-bar WAIT is therefore explicit rather than a misleading Low.
        """
        if candle is None or not ind.sma20_volume:
            return VolumeState.UNKNOWN, "Insufficient data for Volume"

        floor = config.VOLUME_PROJECTION_FLOOR_PCT * ind.sma20_volume
        if candle.volume < floor:
            return VolumeState.UNKNOWN, (
                f"Traded {candle.volume:.0f} < floor {floor:.0f} "
                f"({config.VOLUME_PROJECTION_FLOOR_PCT:.0%} of average) — too early to project"
            )

        elapsed = ind.elapsed_seconds if ind.elapsed_seconds is not None else config.BAR_SECONDS
        elapsed = min(max(elapsed, 0.0), config.BAR_SECONDS)

        if elapsed < config.VOLUME_PROJECTION_MIN_ELAPSED_SECONDS:
            return VolumeState.UNKNOWN, (
                f"Only {elapsed:.0f}s into bar (min "
                f"{config.VOLUME_PROJECTION_MIN_ELAPSED_SECONDS:.0f}s) — too early to project"
            )

        # max(elapsed, 1.0) guards division when MIN_ELAPSED is configured to 0.0.
        projected = candle.volume * (config.BAR_SECONDS / max(elapsed, 1.0))
        relative_volume = projected / ind.sma20_volume
        pace = f"{candle.volume:.0f} in {elapsed:.0f}s -> projected {projected:.0f}"

        if relative_volume >= config.VOLUME_HIGH_THRESHOLD:
            return VolumeState.HIGH, f"Projected relative volume {relative_volume:.2f} >= {config.VOLUME_HIGH_THRESHOLD} ({pace})"
        if relative_volume >= config.VOLUME_LOW_THRESHOLD:
            return VolumeState.NORMAL, f"Projected relative volume {relative_volume:.2f} in Normal band ({pace})"
        return VolumeState.LOW, f"Projected relative volume {relative_volume:.2f} < {config.VOLUME_LOW_THRESHOLD} ({pace})"
```

### A.3.4 `app.py` — pass the field through

In `evaluate_signals()`, add to the `IndicatorSnapshot(...)` construction
(~line 598):

```python
        elapsed_seconds=state.get("elapsed_seconds"),
```

### A.3.5 `main.py` — pass the field through

In the CLI loop's `IndicatorSnapshot(...)` construction (~line 118):

```python
                    elapsed_seconds=snap.get("elapsed_seconds"),
```

## A.4 Tests

### `test_signal_engine.py` — Volume rule

The existing helper is `ind(**kwargs) -> IndicatorSnapshot(**kwargs)`, so tests
pass `elapsed_seconds=` directly.

Existing Volume tests assume raw ratios and **will need updating** — pass
`elapsed_seconds=300` to preserve their intent (a completed-bar reading), or
re-express them in projected terms.

Add:

1. **Projection makes an early bar tradable.** `sma20_volume=100_000`,
   `volume=40_000`, `elapsed_seconds=60` → projected 200,000 → ratio 2.00 →
   `HIGH`. (Raw ratio 0.40 would have been `LOW`.)
2. **Completed bar is unprojected.** `volume=100_000`, `elapsed_seconds=300` →
   ratio 1.00 → `NORMAL`.
3. **Volume floor.** `volume=20_000` (< 0.25 × 100,000), `elapsed_seconds=60` →
   `UNKNOWN`, detail mentions the floor.
4. **Min-elapsed guard.** `volume=50_000`, `elapsed_seconds=5` → `UNKNOWN`,
   detail mentions elapsed. (Passes the floor, blocked by time.)
5. **Missing elapsed defaults to no projection.** `elapsed_seconds=None`,
   `volume=100_000` → ratio 1.00 → `NORMAL`.
6. **Elapsed clamps above BAR_SECONDS.** `elapsed_seconds=600`,
   `volume=100_000` → clamped to 300 → ratio 1.00 → `NORMAL` (not 0.50 / `LOW`).
7. **Band boundaries.** Projected ratios of exactly 1.30 → `HIGH` and exactly
   0.80 → `NORMAL`.

### `test_live_session.py` — elapsed derivation

8. **Forming bar reports true partial elapsed.** Tick at `09:20:00`,
   `advance()` at `09:21:00` → `snapshot()["elapsed_seconds"] == 60`.
9. **Bar-boundary regression (the 300× case).** Ticks inside the `09:20–09:25`
   bar, then `advance(09:25:01)`. Assert
   `snapshot()["elapsed_seconds"] == config.BAR_SECONDS` — **not** ~1 — while
   `current_candle` is the completed bar. This is the core regression test; name
   it explicitly, e.g. `test_completed_bar_reports_full_elapsed_not_clock_age`.
10. **Feed-stall clamp.** After a bar closes with no further ticks, repeated
    `advance()` calls over several simulated minutes keep returning
    `elapsed_seconds == config.BAR_SECONDS`.
11. **No bars at all.** Fresh session, no ticks → `elapsed_seconds ==
    config.BAR_SECONDS` and `current_candle is None` (Volume returns `UNKNOWN`
    via the existing `candle is None` branch).

### End-to-end

12. **No false HIGH at a bar boundary.** Drive a `LiveFiveMinuteSession` through
    a bar close where the completed bar's volume equals `sma20_volume`, feed the
    snapshot through `SignalEngine` → `DecisionEngine`, assert Volume is
    `NORMAL` (not `HIGH`) on the boundary tick.

## A.5 Definition of done — Part A

- [ ] Full suite green; no test weakened to pass.
- [ ] `grep -rn "elapsed" engine.py signal_engine.py` shows elapsed derived from
      `self.current.start` / `last_advance_at` only — no `datetime.now()` in the
      volume path.
- [ ] Volume detail strings show the pace (`X in Ys -> projected Z`).
- [ ] `README.md` Volume section documents projection and both guards.
- [ ] One live session confirms Volume no longer sits at `Low` for the first
      ~3 minutes of every bar, and no `HIGH` appears at a bar boundary on
      ordinary volume.

## A.6 Known interaction, not addressed here

`Unknown` fails the mandatory Volume gate, so the two guards convert the first
seconds of each bar into an explicit `WAIT`. This is intended and improves on
today's misleading `Low`. The practical entry window per bar becomes
`[MIN_ELAPSED, 300]` seconds once the volume floor is met — tune
`VOLUME_PROJECTION_MIN_ELAPSED_SECONDS` down if that proves too restrictive,
since the volume floor alone already blocks projection from a tiny numerator.

---
---

# PART B — Prior-Day Seeding for EMA10 and Volume Baseline

**Scope:** Warm-up state only. No signal rule, threshold, or decision logic changes.

## B.1 Background — what exists today

`fetch_session_five_minute_candles()` fetches **today only**:

```python
# engine.py, current
from_dt = datetime.combine(
    now.date(), dtime(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE), tzinfo=IST
)
raw = kite.historical_data(
    instrument_token=instrument_token, from_date=from_dt, to_date=now,
    interval="5minute", oi=True,
)
```

Everything derives from `self.completed`, which therefore starts empty at 09:15.
Two consequences, every trading day:

**EMA10 starts arbitrary.** `_rebuild_completed_state()` seeds it with the first
bar's close:

```python
self.completed_ema = candle.close if self.completed_ema is None else (...)
```

So at 09:20, EMA10 *equals* the first bar's close. With multiplier 2/11 the seed
decays to ~16% weight after 9 bars (10:00) and ~1.8% after 20 bars (10:55).

**SMA20-volume averages whatever exists.** `_average_recent_volume()` divides by
`len(recent)`, not by the requested lookback:

```python
recent = self.completed[-lookback:]
return sum(bar.volume for bar in recent) / len(recent)
```

At 09:20 that is a one-bar "average", so relative volume ≈ 1.00 by construction.
The 20th bar does not complete until **10:55**.

This contradicts the project's own documented rule: *"pull several prior trading
days of 5-min candles to properly seed EMA — never approximate from the current
session alone."*

**Why waiting is not a workaround.** Starting at 10:55 costs 100 minutes of every
session including the opening range, and still leaves the baseline skewed: all 20
bars then span 09:15–10:55, the highest-volume period of the day. Trading into
the midday trough against an inflated baseline reads `Low` systematically.

## B.2 What must and must not be seeded

Seed bars supply **two fields only**:

| Consumer | Field used | Reason |
|---|---|---|
| EMA10 | `close` | So the first bar's close isn't the entire EMA |
| Volume SMA20 | `volume` | So the window holds 20 real bars at 09:15 |

**Must NOT see seed bars:**

| Consumer | Why |
|---|---|
| VWAP (`_add_volume`) | Session-anchored — prior bars give a multi-day VWAP |
| Volume Profile (`compute_profile`) | Session-anchored — prior bars give a multi-day POC/VAH/VAL |
| OI (`_oi_pattern`) | Compares adjacent completed bars. Yesterday's 15:25 bar next to today's 09:15 bar would measure the **overnight** OI change and misclassify it as fresh conviction on bar one. |

All three already read `self.completed`. Keeping seed bars in a **separate
collection** (`self.seed`) protects them with no changes to those functions —
this is why the fix is a second list, not a longer `from_date`.

## B.3 Changes

### B.3.1 `config.py` — add two constants

```python
# Prior-session 5-minute bars used to warm EMA10 and the volume baseline before
# today's first bar. 36 bars = 3 hours: clears SMA20 (needs 20) and leaves the
# EMA seed at roughly 1% weight, with slack for half-days and thin sessions.
SEED_BARS = 36
# Calendar days to search back for those bars. Weekends and NSE holidays make
# "the previous trading day" a calendar problem — fetch a window and take the
# last SEED_BARS before today's open, so holidays need no special handling.
SEED_LOOKBACK_DAYS = 5
```

### B.3.2 `engine.py` — widen the fetch, split the result

Change `fetch_session_five_minute_candles()` to return **two** lists:

```python
def fetch_session_five_minute_candles(kite, instrument_token: int, now: datetime):
    """Returns (seed_bars, today_bars).

    seed_bars are prior-session bars used ONLY to warm EMA10 and the volume
    baseline. They must never reach VWAP, the Volume Profile, or the OI
    pattern — all of which are session-anchored. See LiveFiveMinuteSession.
    """
    session_open = datetime.combine(
        now.date(), dtime(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE), tzinfo=IST
    )
    from_dt = session_open - timedelta(days=config.SEED_LOOKBACK_DAYS)
    raw = kite.historical_data(
        instrument_token=instrument_token, from_date=from_dt, to_date=now,
        interval="5minute", oi=True,
    )
    bars = []
    for r in raw:
        start = r["date"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
        oi = r.get("oi")
        bars.append(FiveMinuteCandle(start, r["open"], r["high"], r["low"], r["close"],
                                     r["volume"], oi_open=oi, oi_close=oi))
    seed = [b for b in bars if b.start < session_open][-config.SEED_BARS:]
    today = [b for b in bars if b.start >= session_open]
    return seed, today
```

Widening `from_date` returns more rows; the split is what keeps them harmless.

### B.3.3 `LiveFiveMinuteSession.__init__` — accept and store seed bars

```python
def __init__(self, historical_bars, now, bin_size, value_area_pct,
             ema_period: int = 10, seed_bars=None):
    ...
    # Prior-session bars: EMA warm-up and volume baseline ONLY. Deliberately
    # NOT in self.completed — see _rebuild_completed_state and B.2.
    self.seed = list(seed_bars or [])
```

Keeping `seed_bars` a keyword argument with a default means existing callers and
tests continue to construct the session unchanged.

### B.3.4 `_rebuild_completed_state()` — split the loops

The EMA walks seed then today; VWAP and the profile stay on today only:

```python
def _rebuild_completed_state(self):
    today_candles = [bar.as_candle() for bar in self.completed]
    # Session-anchored: today's completed bars only.
    self.profile = compute_profile(today_candles, bin_size=self.bin_size,
                                   value_area_pct=self.value_area_pct)
    self.session_volume = self.session_price_volume = 0.0
    for candle in today_candles:
        self._add_volume(candle.volume, (candle.high + candle.low + candle.close) / 3)

    # Continuous: seed bars warm the EMA so bar one isn't the whole average.
    self.completed_ema = None
    multiplier = 2 / (self.ema_period + 1)
    for bar in list(self.seed) + list(self.completed):
        self.completed_ema = bar.close if self.completed_ema is None else (
            (bar.close - self.completed_ema) * multiplier + self.completed_ema
        )
```

`_finish_current()` needs **no change** — it advances `completed_ema`
incrementally from whatever value the seeded rebuild left.

### B.3.5 `_average_recent_volume()` — draw from seed + completed

```python
def _average_recent_volume(self, lookback: int) -> Optional[float]:
    """Average volume over the last `lookback` completed bars, drawing on
    prior-session seed bars when today hasn't produced enough yet — so the
    baseline is a true 20-bar average from the session's first bar."""
    recent = (list(self.seed) + list(self.completed))[-lookback:]
    if not recent:
        return None
    return sum(bar.volume for bar in recent) / len(recent)
```

### B.3.6 `app.py` and `main.py` — pass seed bars through

Both call sites unpack the new tuple. In `app.py`'s `_bootstrap_live_session()`:

```python
def _bootstrap_live_session(kite, instrument_token, now):
    seed_bars, bars = fetch_session_five_minute_candles(kite, instrument_token, now)
    return LiveFiveMinuteSession(bars, now, config.BIN_SIZE, config.VALUE_AREA_PCT,
                                 ema_period=config.EMA_PERIOD, seed_bars=seed_bars)
```

Apply the equivalent change wherever `main.py` builds its session.

## B.4 Tests

### `test_live_session.py`

1. **EMA is not the first bar's close.** Session seeded with prior bars at a
   clearly different price level; assert `snapshot()["ema_10"]` after the first
   completed bar differs materially from that bar's close.
2. **Volume baseline is full from bar one.** 36 seed bars + 1 completed bar →
   `average_bar_volume` reflects 20 bars, not 1. Assert it equals the mean of the
   last 20 of `seed + completed`.
3. **VWAP excludes seed bars.** Build two sessions with identical today-bars, one
   with seed bars at a far-away price, one without. Assert `vwap` is **identical**.
4. **Volume Profile excludes seed bars.** Same construction; assert `poc`, `vah`,
   `val` are identical. This is the regression test for the multi-day-profile trap.
5. **OI pattern does not span the overnight gap.** Seed bar with a very different
   `oi_close` immediately before today's first bar; assert the first OI pattern of
   the day is `"OI N/A"` (only one completed bar today) rather than a reading
   derived from the seed bar.
6. **Empty seed is harmless.** `seed_bars=None` reproduces today's behaviour
   exactly — this keeps the change safe when history is unavailable.
7. **Fewer seed bars than requested.** 5 seed bars + 3 completed → averages 8, no
   crash, no division by zero.

### `test_engine.py` (or wherever fetch is covered)

8. **Split boundary.** Given raw bars spanning two days, assert every bar in
   `seed` starts before today's session open and every bar in `today` starts at
   or after it.
9. **Seed is capped and takes the most recent.** 200 prior bars supplied → `seed`
   has exactly `SEED_BARS` entries, and they are the last 36 chronologically.

## B.5 Definition of done — Part B

- [ ] Full suite green; no test weakened to pass.
- [ ] Tests 3, 4 and 5 pass — seed bars provably cannot reach VWAP, the Volume
      Profile, or the OI pattern.
- [ ] At 09:20 on a live session, `average_bar_volume` reflects 20 bars and
      EMA10 is not equal to the first bar's close.
- [ ] `README.md` states which indicators are continuous (EMA, volume SMA) and
      which are session-anchored (VWAP, Volume Profile, OI pattern).

## B.6 Known limitation, not addressed here

A rolling 20-bar volume average is still a **flat** baseline, while intraday
volume is U-shaped — Bank Nifty runs heavy at the open and close, light at
midday, easily 3–5× between them. After this fix the baseline is a real 20-bar
average, but at 09:20 it is dominated by yesterday's closing bars (heavy) and at
12:30 by midday bars (light), so `VOLUME_HIGH_THRESHOLD = 1.30` does not mean the
same thing at both times.

The standard remedy is **RVOL** — compare each bar against the average of the
same time-of-day slot over the last N sessions, rather than against the last N
bars. That needs ~20 sessions of history per slot and is a larger data change;
it is deliberately out of scope here and should be decided after the replay
engine can measure the actual volume curve.

---
---

# Out of scope for both parts

- Trend, Pullback, Rejection, OI, or Volume Profile **rules**.
- `DecisionEngine` grading or mandatory-condition logic.
- `AlertEngine` dedupe, cooldown, or delivery.
- Bar-close-only alert gating (separate, deferred decision).
- The `LEVEL_PROXIMITY_POINTS` review.
- RVOL / time-of-day volume normalisation (see B.6).
- The replay engine.
