# Session Bias Engine (V1) — ON HOLD, evidence against

**Status:** not built. Measured against the 12 Aug session and it did not earn its place.
This doc now records why, and what would change the verdict.

---

## What prompted the review

12 Aug produced 24 alerts, 9 direction flips (6 inside 15 minutes, 3 inside 3 minutes), a 42–46%
hit rate at +30m, and adverse excursion exceeding favourable on most alerts. The engine has no
higher-timeframe anchor — the Trend gate needs close on the correct side of session VWAP *and* a
**60-second** EMA slope. VWAP sits inside the day's range, so in a rotational session price crosses
it repeatedly and each crossing re-permits the opposite direction.

Session bias was the proposed fix. It was tested before building.

---

## Finding 1 — the label does not predict outcome

Splitting the 24 alerts by whether a 15-min EMA10 slope agreed with the alert direction:

| | n | win @30m | avg points |
|---|---|---|---|
| bias **agrees** with alert | 15 | 47% | **−41.5** |
| bias **disagrees** | 6 | 17% | **−44.9** |

Both groups lose about the same per alert. The label separates hit rate somewhat but not outcome.

As an informational line on the alert, that makes it a field you learn to ignore inside a week —
worse than absent, because it occupies attention while implying a reliability it does not have.

## Finding 2 — the central input vetoes the start of moves

Used as a gate, the 15-min slope filter made the day materially worse:

| filter | kept | win @30m | sum points | keeps 09:21 winners |
|---|---|---|---|---|
| baseline | 24 | 46% | −286.8 | 3/3 |
| **15m EMA10 slope agrees** | 15 | 47% | **−622.0** | **0/3** |
| outside opening range | 2 | 0% | −243.0 | 0/3 |

It deleted all three 09:21 alerts — the only genuinely good signals of the day (+212 to +233
points at 30 min) — and doubled the loss.

The reason is structural, not a tuning problem. At 09:21 a 15-minute EMA has one bar of history,
so its slope cannot yet have turned. Any rule requiring higher-timeframe confirmation
systematically vetoes the beginning of a move, which is precisely when the signal is worth most.

**Opening range fails for a different reason:** the first 15 minutes on 12 Aug spanned 316 points
(57550–57866), so almost nothing trades outside it. Two alerts survived, both losers.

## Finding 3 — what did work is not in this spec

| filter | kept | win @30m | flips | sum points |
|---|---|---|---|---|
| baseline | 24 | 46% | 9 (6 fast) | −286.8 |
| 30m flip cooldown, no escape | 16 | 50% | 1 (0 fast) | +4.2 |
| **30m cooldown, A+ breaks it** | **17** | **59%** | **3 (1 fast)** | **−14.2** |

*Suppress a direction change within 30 minutes unless the new alert is A+.*

A plain cooldown hides genuine reversals — it suppressed three A+ Shorts on 12 Aug that were all
correct (+70.6, +30.0, +14.6). Letting A+ break it keeps those while still removing the chop,
which is mostly B and A alerts flipping. Hit rate 46% → 59%, alerts 24 → 17, fast flips 6 → 1, all
three morning winners retained.

Roughly five lines in `alert_engine.py`, beside the existing latch. No new module, no 15-min
aggregation, no opening range.

---

## Honest limits on the above

- **One session.** Ten candidate filters against 24 alerts will produce a winner by chance.
- **n=6** on the bias-disagrees side. Thin.
- 11:00–11:36 on 12 Aug was clearly rotational, which is the regime the cooldown is built for and
  the one most flattering to it.
- Prices came from `signal_log` rather than 1-minute candles (the token had expired), so the
  outcome numbers are slightly coarser than Kite bars would give.

None of this rescues the bias engine — the burden is on it to show it helps, and it did not — but
it does mean the cooldown needs replay validation before shipping.

---

## The question this doc is asking

**What argument justifies building it anyway?** Any one of these would reopen it, and each is
answerable with evidence rather than opinion:

1. **"It should not be judged on 12 Aug alone."** Fair — but then name the sessions to test on.
   The replay engine (`bn_replay_engine_spec.md`) exists for this. If bias separates outcome
   across 20 sessions the way it failed to across 1, that settles it in its favour.

2. **"The measured version is not the intended version."** The test used the 15-min EMA slope
   because that was the spec's deciding input. If the real intent is prior-day levels, or a
   trend-vs-range classification, or the daily open — say which, and it can be tested the same way
   in an afternoon.

3. **"It is for confidence, not filtering."** If seeing 🔴 Bearish next to a Long alert would
   actually change whether you take it, the label has value even with a weak statistical edge.
   That is a real answer — but it needs to be stated deliberately, because Finding 1 says the
   label would be wrong about as often as right.

4. **"The value is the record, not the display."** Logging a `session_bias` column costs one field
   and makes the question answerable later from real sessions. This is the cheapest version and
   the hardest to argue against — but it is a logging change, not an engine.

Until one of these is answered, the recommendation is: **build the cooldown, validate it on
replay, leave session bias unbuilt.**

---

## Original design, kept for reference

Timeframe 15-minute, aggregated from `LiveFiveMinuteSession.completed` in threes.
Updates at 09:32 / 10:45 / 13:35 / 14:45 — fixed, never per tick.

Deciding inputs: price vs VWAP, 15-min EMA10 slope, opening-range position.
Context inputs: OI (`classify_oi_pattern` already returns the four states), volume profile
(POC/VAH/VAL already computed).

Classification: unanimity across the three deciding inputs, else Neutral/Rotational. No weighting.

`EMA10 vs VWAP` was excluded — that exact term was removed from the Trend gate after measurement
([signal_engine.py:168](signal_engine.py#L168)): close held above VWAP on 41 rows, close *and*
EMA10 on 1. The EMA term lags by minutes and the lag runs one way only.
