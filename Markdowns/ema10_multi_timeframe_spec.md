# EMA10 @ 10min / 15min — order-watch message addition

## STATUS — spec only, not implemented.

## Objective

Add EMA10 at the 10-minute and 15-minute timeframes to the order-watch
Telegram message (`order_premium.py`'s `format_order_premium()`), alongside
the existing futures-VWAP-equivalent reprice. Context only — not a Black-76
pricing input. Black-76 prices an option from IV, forward, strike, time and
rate; EMA10 has no place in that formula. This sits next to the Black-76
output in the same message, not inside it.

Scope, as specified: **closed bars only, no warm-up.** No live/forming-bar
EMA, no seeding from a prior session. See Consequences below for what that
means at the start of a session.

---

## Current state

The scanner has exactly one EMA today: **EMA10 on 5-minute bars**
(`config.EMA_PERIOD = 10`, `config.BAR_SECONDS = 300`). It lives inside
`engine.py`'s `LiveFiveMinuteSession` — a tick-driven, continuously-running
class whose bucketing is hardcoded to 5 minutes (`FiveMinuteCandle`,
`five_minute_start()`, a literal `timedelta(minutes=5)` in `apply_tick()`).
`signal_engine.py` (hashed into `engine_version()`) only ever reads
`indicators.ema10` — it has no concept of another timeframe.

There is no 10-minute or 15-minute EMA anywhere in this codebase, for any
symbol, today.

---

## Design

**Standalone, same precedent as `order_watch.py` itself.** Built in
`order_premium.py`, not `engine.py`/`signal_engine.py` — the live alert
pipeline and `engine_version()` (`fd8e6f05`) must stay untouched. This is a
message addition for order-watch, not a scanner feature.

**Data source: `kite.historical_data()` with native intervals.** Kite
supports `"10minute"` and `"15minute"` directly — no resampling of 5-minute
bars needed:

```python
raw = kite.historical_data(
    instrument_token=contract.instrument_token,
    from_date=session_open, to_date=now,
    interval="10minute",   # or "15minute"
)
```

**Closed bars only — the guard that matters.** `historical_data()` can
return a partial candle for the bucket still in progress when `to_date`
falls mid-bucket (`engine.py`'s own seed-fetch guards against exactly this
— see `fetch_session_five_minute_candles` / `LiveFiveMinuteSession.__init__`,
which drops any bar whose bucket start is not strictly before the current
one). Same guard here: drop the last returned candle if its bucket hasn't
fully closed as of `now`.

```python
def _closed_bars(raw, interval_minutes: int, now: datetime):
    cutoff = _bucket_start(now, interval_minutes)
    return [b for b in raw if _bucket_start(b["date"], interval_minutes) < cutoff]
```

**EMA10, cold-started, no warm-up — exactly as specified.** Same formula
`engine.py` already uses (`multiplier = 2/(period+1)`), seeded from the
first closed bar's close, folding forward:

```python
def _ema10_closed(bars) -> Optional[float]:
    ema = None
    multiplier = 2 / (config.EMA_PERIOD + 1)
    for b in bars:
        ema = b["close"] if ema is None else (b["close"] - ema) * multiplier + ema
    return ema
```

No prior-session seed bars, unlike the main engine's `LiveFiveMinuteSession`
(which warms EMA10 from `config.SEED_BARS` prior bars on purpose). That
asymmetry is deliberate per this spec, not an oversight — see Consequences.

**Caching.** A 10-min/15-min bar only changes once every 10/15 minutes, so
re-fetching on every order group (as `resolve_vwap`/`price_order` already do
for VWAP and LTP) is unnecessary REST load. Cache each timeframe's EMA10,
refresh only once the wall clock crosses into a new bucket since the last
fetch — same shape as `OrderWatch._maybe_refresh_instruments()`, but keyed
per timeframe instead of a fixed 24h.

**Message addition.** Two more lines in `format_order_premium()`:

```
EMA10 (10m, closed): 56512.30 · LTP 27 pts above
EMA10 (15m, closed): 56498.10 · LTP 41 pts above
```

---

## Consequences of "no warm-up"

- **Undefined until the first closed bar of the session exists** — no
  10-minute EMA10 until 09:25 IST, no 15-minute EMA10 until 09:30 IST. Any
  order priced before that shows those lines as unavailable, not a stale or
  wrong number.
- **Noisy early in the session.** A cold EMA10 seeded from one close needs
  several closed bars to converge — the main engine's 5-minute EMA10 avoids
  this by warming from `config.SEED_BARS` prior-session bars; this feature
  explicitly does not. Accepted as specified, not a defect to fix later
  without being asked.
- Gets more meaningful as the session goes on — by late morning both
  timeframes have enough closed bars that cold-start vs warm-start barely
  differs.

---

## Testing

Pure math, no network — same shape as `test_black76.py`: `_closed_bars()`
correctly drops a partial trailing candle, `_ema10_closed()` matches the
same incremental formula `engine.py` uses on identical synthetic input, and
the "undefined before the first closed bar" case returns `None` rather than
raising.

---

## Out of scope

- Live/forming-bar EMA (i.e. `engine.py`'s `_live_ema()` equivalent) — closed
  bars only, as specified.
- Warm-up / prior-session seeding — explicitly excluded, as specified.
- Any other timeframe beyond 10min/15min.
- Using EMA10 as a Black-76 pricing input — it isn't one; this stays context
  alongside the pricing output, not inside it.
