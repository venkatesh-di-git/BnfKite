# BN Smart Assistant — Implementation Spec v3, rev 2

**Supersedes** `bn_implementation_spec_v3.md` for Commits 2 and 3.
Commit 1 (decision engine gate exposure) is **unchanged** — see v3.

Revision log — five corrections from code review, all verified against source:

| # | Correction | Source |
|---|---|---|
| 1 | `_last_decision_key` short-circuits before the latch; test #6 was unpassable | `alert_engine.py:64-67` |
| 2 | No alert-log `FIELDS` constant and no reusable rotation to "reuse" | `alert_engine.py:110`, `engine.py:495` |
| 3 | `ALERT_GRADE_RANK` named `B+` and `C`, neither of which exist | `Grade` enum, `alert_engine.py:26` |
| 4 | One tick per minute makes Pullback and Rejection structurally unfirable | `engine.py:231-232` |
| 5 | Evaluation cadence unspecified; `elapsed_seconds` moves independently of ticks | `main.py:78` |

---

# Commit 2 — Alert latch (`once_per_bar` + grade-ceiling upgrade)

## Why

`AlertEngine` dedupes on `(status, direction, grade)`. A `WAIT` between two
`ENTRY` evaluations is a different key, so it clears the guard and the next
`ENTRY` is treated as new. Pullback and Rejection are bar-shape rules evaluated
on a *forming* bar, so near EMA10 they flip on every tick and each flip re-arms.

Measured on `alert_log.csv`: **32 trading alerts across 9 unique
`(bar, direction)` pairs.** One bar produced 7.

Bar-close evaluation would fix it, but the alert then arrives after the move.
Alerts are reviewed manually with no auto-execution, so a late alert is
worthless. Industry answer is TradingView's `alert.freq_once_per_bar`: fire on
first occurrence inside the live bar, then latch.

### Why not a strict latch

| Bar | Grade sequence |
|---|---|
| 08-03 15:10 | **B** → A → A → A → A |
| 08-03 15:15 | **B** → A → A |
| 08-03 15:20 | **B** → A+ ×5 |
| 08-05 13:10 | **A** → A → A+ ×4 → A |
| 08-05 14:50 | **B** → B → A |
| 08-05 15:00 | **B** → A |
| 08-05 15:05 | B → B |
| 08-05 15:35 | **B** → B → A |

In 7 of 8 bars the first alert was the **worst** grade. The ceiling climbed
every time, decayed zero times.

```
current (freq_all)        32
strict latch               8    loses 7 of 8 upgrades
latch + upgrade re-fire   15    zero upgrades lost
```

**Chosen: latch + upgrade re-fire.** Bounded at 3 alerts per bar per direction
by construction (B → A → A+); the ceiling only ratchets upward.

**Caveat:** 8 multi-alert bars across 2 late-session windows. The B→A jumps are
driven by the volume gate maturing (relative volume 0.93 → 2.01), which is
partly the projection filling out rather than conviction arriving. Revisit once
replay covers more sessions.

---

## 2.1 — Expose `bar_start` (`engine.py`)

In `snapshot()`, inside the existing lock, beside `current_candle`:

```python
"bar_start": current_bar.start if current_bar else None,
```

Must derive from `current_bar` — the same object that produced
`current_candle`. **Not** `five_minute_start(now)`: when `advance()` closes a bar
it sets `self.current = None`, and `snapshot()` then falls back to the
*completed* bar while the wall clock has already rolled. A clock-derived key
would stamp the old bar's alert with the new bar's window, latch it, and
silently swallow the first genuine alert of that new bar — a missed setup with
no error. Identical trap to the one already documented for `elapsed_seconds`.

---

## 2.2 — Grade rank (`alert_engine.py`) — **REVISED (correction 3)**

The `Grade` enum is `A+, A, B, Ignore`. There is no `B+` and no `C`; the
previous spec invented both.

`TRADING_GRADES` at `alert_engine.py:26` already encodes "what alerts", so a
second independent literal is a drift hazard — the same anti-drift reasoning
behind Commit 1. Derive one from the other:

```python
ALERT_GRADE_RANK = {"B": 1, "A": 2, "A+": 3}     # Ignore never alerts
TRADING_GRADES = tuple(ALERT_GRADE_RANK)          # single source of truth
```

Any grade absent from `ALERT_GRADE_RANK` is not alertable, by construction.

---

## 2.3 — Latch supersedes `_last_decision_key` — **REVISED (correction 1)**

`process_decision` currently short-circuits at `alert_engine.py:64-67`:

```python
key = (decision.status.value, decision.direction.value, decision.grade.value)
if key == self._last_decision_key:
    return None
```

`(ENTRY, Short, A)` on bar N and bar N+1 is the *same key*, so the old guard
returns `None` before the latch is ever consulted. A latch bolted on top would
be unreachable for the most common case, and the "new bar fires again" test
could not pass.

**Resolution: the latch is the authority.** The old key survives only as the
fallback when `bar_start is None` — without it that path would alert every 2
seconds.

### State

```python
self._latch: dict[tuple[datetime, str], int] = {}   # (bar_start, direction) -> best rank
```

### Order of evaluation, per trading decision

1. `decision.status != ENTRY` or grade not in `ALERT_GRADE_RANK` → update
   `_last_decision_key`, return `None`
2. `bar_start is None` → **fall back** to `_last_decision_key` dedupe
   (insufficient data to latch)
3. Otherwise, key = `(bar_start, direction)`:
   - key absent → **fire**, store rank
   - rank > stored → **fire**, update stored
   - else → suppress

### Rules

- `WAIT` / `NEUTRAL` evaluations do **not** clear the latch
- Ceiling is **per direction** — a Long A+ does not suppress a later Short A in
  the same bar
- **Error alerts are exempt** — routed before the latch, never stored in it
- Evict keys older than the current `bar_start` to bound memory

`process_decision` gains `bar_start`; `app.py` and `main.py` pass it from the
snapshot, alongside the `elapsed_seconds` they already thread.

---

## 2.4 — Version hash must cover alerting (`engine.py`)

`engine_version()` hashes `signal_engine.py` and `decision_engine.py`.
`alert_engine.py` is absent — so this commit, which changes what is actually
delivered, would leave the fingerprint unchanged. Two runs, radically different
alert behaviour, identical version string.

Add `"alert_engine.py"` to the module tuple. **Required, not optional.**

---

## 2.5 — Alert log provenance and rotation — **REVISED (correction 2)**

`alert_log.csv` is the file policy is scored against — the 32→15 analysis and
the grade-trajectory finding both came from it. It carries no version marker,
and this commit changes alert counts by design. Unmarked rows cannot be labelled
retroactively.

### The rotation does not exist yet

The previous spec said to "reuse the rotation already in `SignalStateLog`". That
was wrong on two counts:

- `_migrate_if_stale` (`engine.py:495`) is an **instance method** reading
  `self.path` and `self.FIELDS`
- the alert log has **no FIELDS constant** — `fields` is a local variable inside
  `_store` (`alert_engine.py:110`)

### Extract to module level (`engine.py`)

```python
def rotate_if_header_stale(path: str, fields: list) -> None:
    """Rotate an existing log whose header predates `fields`.

    Writers emit a header only when the file is absent, so appending wider rows
    to an older file would silently produce a ragged CSV. Old data is preserved
    under a timestamped name rather than mixed.
    """
    try:
        with open(path, "r", newline="") as f:
            header = next(csv.reader(f), None)
    except FileNotFoundError:
        return
    if header == fields:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(path)
    os.rename(path, f"{base}.{stamp}{ext}")
```

`SignalStateLog._migrate_if_stale` becomes a one-line delegation:
`rotate_if_header_stale(self.path, self.FIELDS)`.

**Import direction is acyclic** — `engine.py` imports only `config` and
`volume_profile`, so `alert_engine → engine` introduces no cycle. Verified.

### In `alert_engine.py`

- Promote the local `fields` to a module- or class-level `ALERT_LOG_FIELDS`
  constant, with `engine_version` appended
- Call `rotate_if_header_stale(config.ALERT_LOG_FILE, ALERT_LOG_FIELDS)` once
  per process, on first write — not per row

`session_date` was considered and dropped — timestamps are full ISO, the date is
derivable.

---

## Tests

1. Same bar, same direction, same grade repeatedly → 1 alert
2. Same bar, B → A → A+ → 3 alerts
3. Same bar, A+ → A → 1 alert (no downgrade re-fire)
4. `WAIT` between two identical `ENTRY`s → 1 alert *(the current failure)*
5. Opposite direction, same bar → separate alert, independent ceiling
6. **New bar, same direction, same grade → fires again** *(guards correction 1:
   fails if `_last_decision_key` still short-circuits)*
7. `bar_start is None` → falls back to `_last_decision_key`, does not alert every
   evaluation
8. Error alert fires regardless of latch state and does not populate it
9. `bar_start` across an `advance()` boundary equals the bar handed out, not the
   clock *(guards the 2.1 trap)*
10. `TRADING_GRADES` is derived from `ALERT_GRADE_RANK` — no grade appears in one
    and not the other
11. Replaying the archived `alert_log.csv` through the latch yields 15
12. Existing `alert_log.csv` with the old header is rotated, not appended to
13. `rotate_if_header_stale` is a no-op when the header already matches
14. `engine_version()` changes when `alert_engine.py` changes

## Acceptance

- All existing tests pass (106 baseline + Commit 1 additions)
- EMA10-chop scenario produces ≤3 alerts per `(bar, direction)`, not 7
- Every new alert row carries a non-empty `engine_version`
- `run_manifest.csv` shows a new fingerprint

---

# Commit 3 — Setup replay engine

## Purpose

Feed a past day's data through the engine and produce the alerts it *would have*
fired, in the same format as the live alert log.

That is the whole requirement. It exists so a rule change — a threshold, the
latch, the VWAP gate — can be evaluated in seconds instead of waiting for a live
session.

**The user is the scoring function.** No MFE/MAE, no win rate, no R multiples,
no target or stop simulation. The alert list is the output; judgement stays
human.

## Explicitly not included

- Entry / SL / T1 / T2 (not carried by `Decision`; would require a port)
- Outcome measurement of any kind
- Parameter sweeping — overfits to a handful of sessions
- P&L simulation, order placement
- Latch-policy comparison from one run (post-hoc filtering) — considered, not
  taken; latch is applied inline as in production

## Data

- **1-minute Kite historical candles.** Required, not a preference: alerts fire
  *inside* the bar, so replay must sample inside the bar. 5-minute data would
  only reproduce bar-close behaviour, which is not what the engine does.
- Current-month futures only. A rollover mid-history breaks VWAP and volume
  profile continuity — treat each contract as a separate corpus.
- Local cache of fetched candles, so repeat runs cost no API calls.

---

## 3.1 — Tick expansion — **REVISED (correction 4)**

### Why one tick per minute fails

`engine.py:231-232` builds the forming bar's high and low from tick prices:

```python
self.current.high = max(self.current.high, price)
self.current.low  = min(self.current.low,  price)
```

With a single close-priced tick per minute, `high == low == close` on the
forming bar. Pullback requires `low <= ema10 < close`; Rejection requires a
wick. **Both gates become structurally unfirable** — replay would report near
zero alerts and the result would look like a conservative estimate rather than a
broken harness.

### Expansion

Four ticks per minute at **+14s, +29s, +44s, +59s**, in candle order:

- up candle (`close >= open`): O → H → L → C
- down candle: O → L → H → C

**Volume is distributed across the four ticks, not loaded onto the last.**
Two reasons:

1. Live volume accrues continuously. Loading it at +59s leaves the first three
   ticks with zero delta, so projected relative volume sawtooths — and the
   volume gate is precisely what is being measured.
2. `_add_volume(delta, price)` feeds the **volume profile** at the tick price.
   All volume at the close price biases POC toward closing prices, which then
   distorts the VAH/VAL gate.

Expansion mode is stamped into every output row.

### Fidelity — supersedes the "lower bound" claim in v3

v3 stated replay alert count is a **lower bound**. That held for close-only
ticks. **It does not hold once path is synthesized**, and the earlier statement
must not be carried forward.

O→H→L→C is a convention, not a fact. Whether the high preceded the low is
unrecoverable from a 1-minute OHLC bar. A fabricated ordering can manufacture a
pullback or a rejection wick that never occurred as easily as it can miss one.

**Replay alert count is therefore an estimate with fabricated path dependence —
it can err in either direction.**

The two use cases diverge here, and both remain valid:

- **Golden file / regression:** unaffected. The fabrication is deterministic and
  constant across runs, so it still catches every rule change exactly.
- **"What would have fired":** directional evidence, not a count to trust
  absolutely. This matters most at the first intended use — comparing replay
  against a live session — where "lower bound" would invite explaining away a
  real discrepancy.

---

## 3.2 — Evaluation cadence — **REVISED (correction 5)**

Replay steps **simulated time every 2 seconds**, mirroring `main.py:78-157` —
not once per candle, and not only on ticks.

`elapsed_seconds` drives the volume projection and advances on `session.advance()`
alone, independently of tick arrival. Evaluating only when a tick lands would
miss state transitions that fire live. Roughly 150 evaluations per 5-minute bar;
trivially fast, and the latch absorbs the extra.

---

## 3.3 — Required mechanics

- **Injected clock.** `alert_engine.py:73` and `:93` call `datetime.now()` for
  the record timestamp; both need a `now` parameter. The rule path is already
  clean — `engine.py`, `signal_engine.py`, `decision_engine.py` have zero clock
  reads.
- **Fresh instances per replayed day.** The latch dict, `SignalLog`'s change
  key, and `_last_decision_key` all carry state. Leaking them across days
  silently swallows day 2's first alerts.
- **Prime `last_cumulative_volume = 0.0`** or the first candle's volume is
  dropped.
- **`engine_version` stamped on every replay run and every output row.** A run
  spanning a version boundary must refuse or split, never average.
- **Telegram delivery off** via an explicit flag, not by relying on a blank
  token.

## Output

`replay_alerts_<date>_<engine_version>.csv`, same columns as `alert_log.csv`,
plus the expansion mode.

---

## 3.4 — Near-miss log

Every evaluation where the direction gates passed but **exactly one** gate
blocked, with the gate name and the value that missed.

Reads directly off `Decision` from Commit 1:

```python
if len(d.short_gates.failed) == 1:
    blocking = d.short_gates.failed[0]
```

This is the missed-A+ measurement, currently invisible — only fired alerts are
ever seen. If most near-misses are Volume blocking at 1.25 against a 1.30
threshold, that is a threshold decision made on evidence. It is also the only
way to tell whether the VWAP fix helped or whether Volume is the real
bottleneck.

Output: `replay_nearmiss_<date>_<engine_version>.csv` — timestamp, bar_start,
direction, blocking gate, measured value for that gate.

---

## 3.5 — Golden-file regression test

Freeze one replayed day's alert output as a checked-in reference CSV. Any commit
that changes it fails the test until the diff is reviewed and approved.

This turns replay into CI: every refactor and every threshold tweak shows
exactly which alerts moved, before shipping. It is the only item here that
protects automatically rather than when someone remembers to look — and the
failure it prevents has already happened once, when 44 rows straddled a
volume-rule change with nothing to mark them.

Depends on replay determinism: same input, same output, byte for byte. That
follows from the injected clock, fixed tick-expansion offsets, fixed 2s cadence,
and fresh-instances rules above.

Pin the reference file's `engine_version` **and expansion mode** in the test, so
a legitimate change produces a clear "golden file is stale for version X" rather
than a mystery diff.

---

# Follow-ups this sequence unblocks

1. **VWAP fix validation.** The EMA-term removal shipped at 15:20 on 05 Aug with
   40 minutes left of a down session — 54 post-fix rows, 0 Bullish. Unvalidated.
   Needs a session containing a genuine VWAP reclaim; replay can supply one.
2. **Grade-trajectory measurement at scale.** Whether the ceiling genuinely
   climbs, or whether it is projected volume maturing, decides both the upgrade
   policy and any elapsed-threshold design.
3. **Elapsed-dependent grade thresholds** (fire A+ only before 60s, A after
   120s) — the natural successor to the latch, but needs replay evidence first.
4. **Near-miss live column** in `signal_log.csv` — Commit 1 makes this available
   live, not just in replay.
5. **RVOL / time-of-day volume normalisation.** The SMA20 baseline is flat
   against a U-shaped intraday volume curve, so 1.30 means different things at
   09:20 and 12:30. Separate investigation; replay supplies the evidence.
6. **Path-order sensitivity.** Running replay under both expansion orders
   (O→H→L→C and O→L→H→C) and comparing alert sets would bound the fabrication
   error introduced in 3.1. Cheap once replay exists; deliberately out of scope
   for the first build.

---

# Security — open, unchanged

- `.kite_session_cache.json` ships in the zip with a live `access_token` and
  `api_key` — sufficient for order placement until expiry. Add to zip exclusions
  alongside `.env`.
- Kite API secret from earlier zips remains unrotated.
- Telegram bot token remains unrevoked.
