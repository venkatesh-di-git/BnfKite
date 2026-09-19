"""
engine.py

Shared logic between the CLI (main.py) and the NiceGUI dashboard
(app.py): the tick-driven 5-minute session, market-hours check, and
output persistence. Neither entry point duplicates this.

Live pricing/volume/OI all come from KiteTicker (see kite_ticker.py);
REST historical_data is used only ONCE at startup, to seed the bars
already completed earlier in the session before the websocket connects.
"""

import csv
import hashlib
import json
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from threading import RLock
from typing import Optional
from zoneinfo import ZoneInfo

import config
from volume_profile import Candle, compute_profile, VolumeProfileResult

IST = ZoneInfo("Asia/Kolkata")

# Constants that change what the rules DO. Config values must be in the hash,
# not just module source: retuning a threshold alters behaviour without editing
# either rule module.
_VERSIONED_CONSTANTS = (
    "EMA_SLOPE_LOOKBACK_SECONDS", "EMA_SLOPE_HYSTERESIS_POINTS",
    "VOLUME_HIGH_THRESHOLD", "VOLUME_LOW_THRESHOLD", "VOLUME_HYSTERESIS",
    "VOLUME_PROJECTION_FLOOR_PCT", "VOLUME_PROJECTION_MIN_ELAPSED_SECONDS",
    "VOLUME_LOOKBACK_BARS", "OI_FLAT_THRESHOLD_PCT", "LEVEL_PROXIMITY_POINTS",
    "BAR_SECONDS", "VWAP_REJECTION_CLOSE_PCT",
)


def rotate_if_header_stale(path: str, fields) -> None:
    """Rotate an existing log whose header predates `fields`.

    Writers here emit a header only when the file is absent, so appending wider
    rows to an older file would silently produce a ragged CSV. The old data is
    kept intact under a timestamped name rather than mixed into the new schema.

    Shared by SignalStateLog and AlertEngine — the alert log had no rotation at
    all, and duplicating this one is how the two drift apart.

    list() on both sides is load-bearing: csv.reader always hands back a list,
    so comparing it against a tuple of identical strings is False and the log
    would be rotated on EVERY process start, quietly starting a fresh empty
    file each launch. Field lists in this codebase are written both ways.
    """
    try:
        with open(path, "r", newline="") as f:
            header = next(csv.reader(f), None)
    except FileNotFoundError:
        return
    if header is not None and list(header) == list(fields):
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(path)
    os.rename(path, f"{base}.{stamp}{ext}")


def engine_version() -> str:
    """8-char fingerprint of the rule logic: the source of both rule modules
    plus the tuning constants.

    Exists because signal_log.csv silently mixed two engine versions on 05 Aug
    — 44 rows written before a volume-rule fix and the rest after, with nothing
    to tell them apart. A replay across that file draws wrong conclusions.

    Hashing source means a comment-only edit registers as a new version. That
    is over-sensitive on purpose: the failure being prevented is a behaviour
    change that leaves no trace.
    """
    h = hashlib.sha256()
    here = os.path.dirname(os.path.abspath(__file__))
    # alert_engine.py is in here because it decides what is actually DELIVERED.
    # Without it, the latch could change alert counts radically while two runs
    # reported an identical version string.
    for module in ("signal_engine.py", "decision_engine.py", "alert_engine.py"):
        try:
            with open(os.path.join(here, module), "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            h.update(b"<missing>")
    for name in _VERSIONED_CONSTANTS:
        h.update(f"{name}={getattr(config, name, None)!r};".encode())
    return h.hexdigest()[:8]


@dataclass
class FiveMinuteCandle:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi_open: Optional[float] = None
    oi_close: Optional[float] = None

    def as_candle(self) -> Candle:
        return Candle(self.open, self.high, self.low, self.close, self.volume)


def five_minute_start(value: datetime) -> datetime:
    return value.replace(minute=value.minute - value.minute % 5, second=0, microsecond=0)


def classify_oi_pattern(price_change: Optional[float], oi_change: Optional[float]) -> str:
    """Same four-state classification used in the Pine script's OI gate,
    so Python and Pine agree on what 'fresh conviction' looks like."""
    if price_change is None or oi_change is None:
        return "OI N/A"
    if price_change > 0 and oi_change > 0:
        return "Fresh Longs"
    if price_change > 0 and oi_change < 0:
        return "Short Covering"
    if price_change < 0 and oi_change > 0:
        return "Fresh Shorts"
    if price_change < 0 and oi_change < 0:
        return "Long Liquidation"
    return "Flat"


def fetch_session_five_minute_candles(kite, instrument_token: int, now: datetime):
    """One-shot REST call at startup: returns (seed_bars, today_bars).

    seed_bars are prior-session bars used ONLY to warm EMA10 and the volume
    baseline. They must never reach VWAP, the Volume Profile, or the OI
    pattern — all three are session-anchored, and feeding them prior sessions
    would give a multi-day VWAP/profile and measure the overnight OI gap as
    fresh conviction on today's first bar. See LiveFiveMinuteSession.

    Widening from_date is what makes the seed available; splitting the result
    is what keeps it harmless.
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
        bars.append(FiveMinuteCandle(start, r["open"], r["high"], r["low"], r["close"], r["volume"],
                                      oi_open=oi, oi_close=oi))
    seed = [b for b in bars if b.start < session_open][-config.SEED_BARS:]
    today = [b for b in bars if b.start >= session_open]
    return seed, today


class LiveFiveMinuteSession:
    """Thread-safe state: ticks update the current bar; only closed bars
    form the profile. Price, volume, and OI all come from live ticks
    once started — see apply_tick()."""

    def __init__(self, historical_bars, now: datetime, bin_size: float, value_area_pct: float,
                 ema_period: int = 10, seed_bars=None):
        self._lock = RLock()
        # Prior-session bars: EMA warm-up and volume baseline ONLY. Kept out
        # of self.completed on purpose — VWAP, the Volume Profile and the OI
        # pattern all read self.completed, so this separation is what stops
        # them ever seeing another session. A keyword default keeps existing
        # callers and tests constructing the session unchanged.
        self.seed = list(seed_bars or [])
        # The seed/completed split above keeps prior-session bars out of VWAP and
        # the profile — but only for the bars handed in at construction. Nothing
        # stops a session that OUTLIVES its day from accumulating tomorrow's bars
        # into self.completed, which is a multi-day POC/VWAP that raises no error.
        # Callers check is_from_a_previous_day() and rebuild.
        self.session_date = now.date()
        self.bin_size, self.value_area_pct, self.ema_period = bin_size, value_area_pct, ema_period
        current_start = five_minute_start(now)
        self.completed = [bar for bar in historical_bars if five_minute_start(bar.start) < current_start]
        partial = [bar for bar in historical_bars if five_minute_start(bar.start) == current_start]
        self.current = partial[-1] if partial else None
        self.last_cumulative_volume = None
        self.last_price = self.current.close if self.current else None
        # Bucket closure is monotonic: once a bar is closed, that bucket can
        # never be reopened. advance() closes bars on the local wall clock
        # while apply_tick() sees exchange_timestamps that lag it during a
        # lull, so without this a late tick would re-create an already-closed
        # bar and the next advance() would close it again — once per second
        # until the timestamps caught up, appending a duplicate fragment bar
        # to `completed` every time.
        self.last_closed_start = five_minute_start(self.completed[-1].start) if self.completed else None
        # (timestamp, live_ema) samples backing the Trend rule's slope. Filled
        # by advance(), which is the wall-clock entry point in both runners —
        # sampling from apply_tick's exchange_timestamp instead would mix the
        # two clocks, the mistake behind closed buckets being reopened.
        self._ema_history: deque = deque()
        self.last_advance_at: Optional[datetime] = None
        self._rebuild_completed_state()
        if self.current:
            self._add_volume(self.current.volume, (self.current.high + self.current.low + self.current.close) / 3)

    def is_from_a_previous_day(self, now: datetime) -> bool:
        """True once this session has outlived the day it was built for.

        Lives here rather than as an inline date comparison in the caller so the
        rule is testable — app.py has no test coverage.
        """
        return now.date() != self.session_date

    def _rebuild_completed_state(self):
        # Session-anchored: today's completed bars only. Seed bars must not
        # reach either of these — they would produce a multi-day VWAP and a
        # multi-day POC/VAH/VAL.
        today_candles = [bar.as_candle() for bar in self.completed]
        self.profile = compute_profile(today_candles, bin_size=self.bin_size,
                                       value_area_pct=self.value_area_pct)
        self.session_volume = self.session_price_volume = 0.0
        for candle in today_candles:
            self._add_volume(candle.volume, (candle.high + candle.low + candle.close) / 3)

        # Continuous: prior-session bars warm the EMA so today's first bar
        # isn't the entire average. _finish_current() then advances it
        # incrementally from whatever this leaves.
        self.completed_ema = None
        multiplier = 2 / (self.ema_period + 1)
        for bar in list(self.seed) + list(self.completed):
            self.completed_ema = bar.close if self.completed_ema is None else (
                (bar.close - self.completed_ema) * multiplier + self.completed_ema
            )

    def _add_volume(self, volume: float, price: float):
        if volume > 0:
            self.session_volume += volume
            self.session_price_volume += price * volume

    def _finish_current(self):
        if not self.current:
            return
        self.completed.append(self.current)
        self.last_closed_start = self.current.start
        multiplier = 2 / (self.ema_period + 1)
        self.completed_ema = self.current.close if self.completed_ema is None else (
            (self.current.close - self.completed_ema) * multiplier + self.completed_ema
        )
        self.profile = compute_profile(
            [bar.as_candle() for bar in self.completed],
            bin_size=self.bin_size, value_area_pct=self.value_area_pct,
        )

    def apply_tick(self, price: float, cumulative_volume: float, oi: Optional[float], timestamp: datetime):
        """Every live tick updates price/volume/OI for the current forming bar."""
        with self._lock:
            bucket = five_minute_start(timestamp)
            if self.last_closed_start is not None:
                # Tick stamped inside a bucket we've already closed — fold it
                # into the next live bar instead of resurrecting that bucket.
                # Volume accounting is unaffected: last_cumulative_volume still
                # advances, so the delta is attributed to the new bar.
                bucket = max(bucket, self.last_closed_start + timedelta(minutes=5))
            if self.current is None or self.current.start != bucket:
                self._finish_current()
                self.current = FiveMinuteCandle(bucket, price, price, price, price, 0.0)
            if self.last_cumulative_volume is not None and cumulative_volume >= self.last_cumulative_volume:
                delta = cumulative_volume - self.last_cumulative_volume
                self.current.volume += delta
                self._add_volume(delta, price)
            self.last_cumulative_volume = cumulative_volume
            self.current.high = max(self.current.high, price)
            self.current.low = min(self.current.low, price)
            self.current.close = price
            self.last_price = price
            if oi is not None:
                if self.current.oi_open is None:
                    self.current.oi_open = oi
                self.current.oi_close = oi
            return self.snapshot()

    def set_profile_parameters(self, bin_size: float, value_area_pct: float):
        with self._lock:
            self.bin_size, self.value_area_pct = bin_size, value_area_pct
            self.profile = compute_profile(
                [bar.as_candle() for bar in self.completed], bin_size=bin_size, value_area_pct=value_area_pct
            )

    def advance(self, timestamp: datetime):
        """Close an elapsed bar even when the next trade arrives late, and
        sample the live EMA for the Trend rule's rolling slope."""
        with self._lock:
            if self.current and self.current.start < five_minute_start(timestamp):
                self._finish_current()
                self.current = None
            self.last_advance_at = timestamp
            self._record_ema_sample(timestamp)
            return self.snapshot()

    def _oi_pattern(self):
        """Compare the reference bar (current if forming, else last
        completed) against the previous completed bar. Returns
        (current_oi, previous_oi, oi_pattern) — previous_oi is exposed
        separately so the Signal Engine can classify Rising/Falling/Flat
        purely off OI, without the price coupling oi_pattern has."""
        ref_bar = self.current if self.current else (self.completed[-1] if self.completed else None)
        prev_bar = self.completed[-1] if (self.current and self.completed) else (
            self.completed[-2] if len(self.completed) >= 2 else None
        )
        if ref_bar is None or prev_bar is None:
            return None, None, "OI N/A"
        if ref_bar.oi_close is None or prev_bar.oi_close is None:
            return None, None, "OI N/A"
        price_change = ref_bar.close - prev_bar.close
        oi_change = ref_bar.oi_close - prev_bar.oi_close
        return ref_bar.oi_close, prev_bar.oi_close, classify_oi_pattern(price_change, oi_change)

    def _live_ema(self) -> Optional[float]:
        """EMA10 including the still-forming bar's close."""
        if not self.current:
            return self.completed_ema
        if self.completed_ema is None:
            return self.current.close
        multiplier = 2 / (self.ema_period + 1)
        return (self.current.close - self.completed_ema) * multiplier + self.completed_ema

    def _record_ema_sample(self, timestamp: datetime):
        """Append one live-EMA sample and evict anything well past the window.
        Ignores non-advancing timestamps so a clock that stalls or steps back
        can't corrupt the ordering the lookup below relies on."""
        ema = self._live_ema()
        if ema is None:
            return
        if self._ema_history and timestamp <= self._ema_history[-1][0]:
            return
        self._ema_history.append((timestamp, ema))
        cutoff = timestamp - timedelta(seconds=config.EMA_SLOPE_LOOKBACK_SECONDS * 2)
        while self._ema_history and self._ema_history[0][0] < cutoff:
            self._ema_history.popleft()

    def _ema_at_lookback(self) -> Optional[float]:
        """Newest sample that is at least EMA_SLOPE_LOOKBACK_SECONDS old, or
        None until the window has filled.

        Selected by elapsed time, not by position: app.py advances once a
        second but main.py every SNAPSHOT_INTERVAL_SECONDS, so a fixed-length
        window would span different durations in the two runners.
        """
        if self.last_advance_at is None:
            return None
        target = self.last_advance_at - timedelta(seconds=config.EMA_SLOPE_LOOKBACK_SECONDS)
        candidate = None
        for sampled_at, value in self._ema_history:
            if sampled_at > target:
                break
            candidate = value
        return candidate

    def _average_recent_volume(self, lookback: int) -> Optional[float]:
        """Average volume over the last `lookback` completed bars, drawing on
        prior-session seed bars when today hasn't produced enough yet — so the
        baseline is a true 20-bar average from the session's first bar rather
        than a one-bar 'average' that makes relative volume ~1.00 by
        construction until 10:55."""
        recent = (list(self.seed) + list(self.completed))[-lookback:]
        if not recent:
            return None
        return sum(bar.volume for bar in recent) / len(recent)

    def snapshot(self):
        with self._lock:
            vwap = self.session_price_volume / self.session_volume if self.session_volume else None
            ema = self._live_ema()
            current_oi, previous_oi, oi_pattern = self._oi_pattern()
            current_bar = self.current if self.current else (self.completed[-1] if self.completed else None)
            # Copy the bar's OHLCV here, inside the lock. Handing out
            # self.current itself would let a consumer read it on another
            # thread while apply_tick mutates it — yielding a candle whose
            # close came from one tick and whose volume came from the next,
            # a state the market never actually had. Deriving
            # current_bar_volume from the same copy keeps the two fields
            # consistent with each other by construction.
            current_candle = current_bar.as_candle() if current_bar else None
            # Elapsed is derived from the bar being handed out, NOT the wall
            # clock. advance() nulls self.current when it closes a bar, so the
            # snapshot returned by that very call carries a COMPLETED bar via
            # the fallback above — a full BAR_SECONDS old by definition. A
            # clock-derived elapsed would read ~1s there, because
            # five_minute_start(now) has just rolled over, and inflate a
            # finished bar's volume by up to 300x. Deterministic, ~75 times a
            # session. Same reasoning covers a feed stall, where advance()
            # keeps running with current_candle stuck on a finished bar.
            if self.current is not None and self.last_advance_at is not None:
                elapsed_seconds = (self.last_advance_at - self.current.start).total_seconds()
                # Lower clamp guards a clock step-back; upper guards a bar that
                # outlives its own window.
                elapsed_seconds = min(max(elapsed_seconds, 0.0), config.BAR_SECONDS)
            else:
                elapsed_seconds = config.BAR_SECONDS
            return {
                "result": self.profile, "vwap": vwap, "ema_10": ema,
                # EMA10 as it stood EMA_SLOPE_LOOKBACK_SECONDS ago; the Trend
                # rule takes ema - ema_10_prev as the slope. Deliberately NOT
                # the last closed bar's EMA: within a bar that is constant, so
                # the difference collapsed to a distance from it and flipped
                # sign on every tick across it. None until the window fills.
                "ema_10_prev": self._ema_at_lookback(),
                "current_price": self.last_price, "candle_count": len(self.completed),
                "oi": current_oi, "previous_oi": previous_oi, "oi_pattern": oi_pattern,
                "current_bar_volume": current_candle.volume if current_candle else None,
                "average_bar_volume": self._average_recent_volume(config.VOLUME_LOOKBACK_BARS),
                # Still-forming candle as an immutable point-in-time copy —
                # Pullback/Rejection need its OHLC, not just the latest price.
                "current_candle": current_candle,
                "elapsed_seconds": elapsed_seconds,
                # The bar current_candle came from, for the alert latch's
                # per-bar key. Derived from current_bar, NOT
                # five_minute_start(now), for the same reason elapsed_seconds
                # is: advance() nulls self.current when it closes a bar, so
                # this snapshot carries a COMPLETED bar while the wall clock
                # has already rolled. A clock-derived key would stamp that
                # bar's alert with the next bar's window, latch it, and
                # silently swallow the first genuine alert of the new bar.
                "bar_start": current_bar.start if current_bar else None,
            }


def is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = dtime(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)
    close_t = dtime(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)
    return open_t <= now.time() <= close_t


def write_output(result: VolumeProfileResult, tradingsymbol: str, now: datetime,
                  current_price: float = None, vwap: float = None, ema_10: float = None,
                  oi: float = None, oi_pattern: str = None):
    payload = {
        "timestamp": now.isoformat(),
        "instrument": tradingsymbol,
        "poc": result.poc,
        "vah": result.vah,
        "val": result.val,
        "total_volume": result.total_volume,
        "current_price": current_price,
        "vwap": vwap,
        "ema_10": ema_10,
        "oi": oi,
        "oi_pattern": oi_pattern,
        "bin_size": config.BIN_SIZE,
        "value_area_pct": config.VALUE_AREA_PCT,
    }
    with open(config.OUTPUT_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    log_exists = False
    try:
        with open(config.LOG_FILE, "r"):
            log_exists = True
    except FileNotFoundError:
        pass

    fields = ["timestamp", "instrument", "poc", "vah", "val", "total_volume",
              "current_price", "vwap", "ema_10", "oi", "oi_pattern"]

    with open(config.LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not log_exists:
            writer.writerow(fields)
        writer.writerow([payload[field] for field in fields])

    return payload


def measured_fields(candle, indicators) -> dict:
    """The measured columns for SignalStateLog.record(), pulled off the very
    objects the rules were evaluated against.

    Exists so app.py and main.py cannot drift: both already duplicate the
    evaluate/decide/alert sequence, and the whole point of these columns is
    that they record exactly what the rules compared.
    """
    ema10 = indicators.ema10 if indicators else None
    ema10_prev = indicators.ema10_prev if indicators else None
    return {
        "open_": candle.open if candle else None,
        "high": candle.high if candle else None,
        "low": candle.low if candle else None,
        "close": candle.close if candle else None,
        "bar_volume": candle.volume if candle else None,
        "sma20_volume": indicators.sma20_volume if indicators else None,
        "elapsed_seconds": indicators.elapsed_seconds if indicators else None,
        "ema_10": ema10,
        "vwap": indicators.vwap if indicators else None,
        # Same subtraction _evaluate_trend does; None until the lookback fills.
        "ema_slope": (ema10 - ema10_prev)
                     if (ema10 is not None and ema10_prev is not None) else None,
    }


def _blocking_gate(gates) -> str:
    """The single gate refusing this direction, or "" when zero or several do.

    EXACTLY one blocker is the signal worth recording: direction is right and
    one threshold is holding the setup back, which is evidence that threshold
    may be mistuned. Two or more blockers means nothing aligned, which says
    nothing about any single threshold.

    Only fired alerts have ever been visible. This is the other half — the
    setups that nearly fired and why they didn't.
    """
    if gates is None:
        return ""
    failed = gates.failed
    return failed[0] if len(failed) == 1 else ""


class SignalStateLog:
    """Appends a row whenever any signal or decision state changes.

    Change-triggered rather than per-tick, so a quiet session stays small
    while every transition is captured. Lives beside write_output() because
    it is output persistence, not trading logic: it reads `.value` off
    whatever the engines produce and never interprets it.

    Both runners share this one implementation — app.py's evaluate_signals()
    and main.py's loop already duplicate the evaluate/decide/alert sequence,
    and this must not become a third copy.
    """

    # Discrete states only — these form the change key. Everything in
    # MEASURED_FIELDS is continuous and deliberately excluded: those values move
    # on every tick, so including any of them would make every evaluation look
    # like a change and destroy the log's compression.
    # The *_blocked columns cost ZERO extra rows despite being in the change key:
    # each is a pure function of the state fields already here, so none can change
    # unless the key would have changed anyway. Both directions can be near-misses
    # at once, and the two setups block for different reasons, so all four are
    # recorded separately — collapsing them would lose exactly the comparison the
    # VWAP setup was added to make.
    #
    # vwap_pullback/vwap_rejection DO add rows: unlike the blocked columns they are
    # independent state dimensions, not derived. That is intended.
    STATE_FIELDS = ["status", "direction", "grade", "trend", "pullback", "rejection",
                    "volume", "open_interest", "poc", "vah", "val",
                    "vwap_pullback", "vwap_rejection", "setup",
                    "long_blocked_ema", "short_blocked_ema",
                    "long_blocked_vwap", "short_blocked_vwap"]
    # The inputs the rules actually compared, recorded so a threshold can be
    # retuned from the log instead of reconstructed. high/low in particular are
    # unrecoverable afterwards: rows are written only on change, so ticks
    # between them are invisible and a bar's running extremes cannot be rebuilt.
    # relative_volume is NOT here — it is derivable from bar_volume,
    # sma20_volume and elapsed_seconds, which is verified by test.
    # bar_start is here, NOT in STATE_FIELDS, and it is what makes the near-miss
    # columns countable. Rows are written only on change, so a near-miss that
    # holds steady for four minutes writes one row while one oscillating near a
    # threshold writes twenty — counting rows therefore ranks the NOISIEST gate
    # first, not the costliest. The metric is distinct (bar_start,
    # blocking_gate) pairs: "in how many bars did this gate block a near-miss".
    #
    # Excluding it from the change key keeps the zero-extra-rows property, and
    # is exact rather than merely lucky: a near-miss requires the volume gate to
    # PASS, which requires elapsed >= VOLUME_PROJECTION_MIN_ELAPSED_SECONDS.
    # Every bar boundary resets elapsed, forcing VolumeState to UNKNOWN — a
    # state change, so a row is written carrying the new bar_start. No
    # (bar, gate) pair can go unrecorded.
    # close is recorded even though current_price usually equals it: the two
    # diverge exactly when no bar is forming, and the VWAP Rejection rule is
    # close-position-in-range, so auditing it offline off the wrong one would be
    # silently wrong on precisely those rows.
    MEASURED_FIELDS = ["bar_start", "current_price", "open", "high", "low", "close",
                       "bar_volume", "sma20_volume", "elapsed_seconds",
                       "ema_10", "vwap", "ema_slope"]
    FIELDS = ["timestamp"] + STATE_FIELDS + MEASURED_FIELDS + ["engine_version"]

    def __init__(self, path: Optional[str] = None):
        self.path = path or config.SIGNAL_LOG_FILE
        self._last_state = None
        self._version = engine_version()
        self._prepared = False

    def _prepare(self, now: datetime):
        """Rotation and manifest are once-per-process, not per row."""
        if self._prepared:
            return
        self._prepared = True
        self._migrate_if_stale()
        self._write_manifest(now)

    def _migrate_if_stale(self):
        """Rotate an existing log whose header predates the current FIELDS.
        Shared with AlertEngine — see rotate_if_header_stale."""
        rotate_if_header_stale(self.path, self.FIELDS)

    def _write_manifest(self, now: datetime):
        """One line per process start, so a version change can be explained
        rather than merely detected."""
        path = os.path.join(os.path.dirname(self.path) or ".", "run_manifest.csv")
        fields = ["timestamp", "engine_version"] + list(_VERSIONED_CONSTANTS)
        # Same rotation the signal and alert logs use. Without it this writer —
        # the only one that lacked it — appends a wider row under the old header
        # the moment _VERSIONED_CONSTANTS grows, producing a ragged CSV with no
        # error. Adding VWAP_REJECTION_CLOSE_PCT did exactly that.
        rotate_if_header_stale(path, fields)
        exists = os.path.exists(path)
        try:
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if not exists:
                    writer.writerow(fields)
                writer.writerow([now.isoformat(), self._version]
                                + [getattr(config, n, None) for n in _VERSIONED_CONSTANTS])
        except OSError:
            pass  # provenance is a nice-to-have; never break logging over it

    def record(self, snapshot, decision, now: datetime, current_price=None,
               bar_volume=None, sma20_volume=None, elapsed_seconds=None,
               open_=None, high=None, low=None, ema_10=None, vwap=None,
               ema_slope=None, bar_start=None, close=None) -> bool:
        """Writes a row only if a state changed. Returns whether it wrote."""
        if snapshot is None or decision is None:
            return False

        state = {
            "status": decision.status.value,
            "direction": decision.direction.value,
            "grade": decision.grade.value,
            "trend": snapshot.trend.value,
            "pullback": snapshot.pullback.value,
            "rejection": snapshot.rejection.value,
            "volume": snapshot.volume.value,
            "open_interest": snapshot.open_interest.value,
            "poc": snapshot.poc.value,
            "vah": snapshot.vah.value,
            "val": snapshot.val.value,
            "vwap_pullback": snapshot.vwap_pullback.value,
            "vwap_rejection": snapshot.vwap_rejection.value,
            "setup": getattr(decision, "setup", None) or "",
            # Read straight off the Decision — recomputing the gate rules here
            # is how this copy would drift from decision_engine's.
            "long_blocked_ema": _blocking_gate(getattr(decision, "long_gates", None)),
            "short_blocked_ema": _blocking_gate(getattr(decision, "short_gates", None)),
            "long_blocked_vwap": _blocking_gate(getattr(decision, "long_vwap_gates", None)),
            "short_blocked_vwap": _blocking_gate(getattr(decision, "short_vwap_gates", None)),
        }
        key = tuple(state[field] for field in self.STATE_FIELDS)
        if key == self._last_state:
            return False
        self._last_state = key

        row = dict(state)
        row.update({
            "timestamp": now.isoformat(),
            # From snapshot(), never five_minute_start(timestamp) — a row
            # stamped 15:10:01 can legitimately describe the 15:05 bar.
            "bar_start": bar_start.isoformat() if bar_start else None,
            "current_price": current_price,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "bar_volume": bar_volume,
            "sma20_volume": sma20_volume,
            "elapsed_seconds": elapsed_seconds,
            "ema_10": ema_10,
            "vwap": vwap,
            "ema_slope": ema_slope,
            "engine_version": self._version,
        })

        self._prepare(now)

        log_exists = False
        try:
            with open(self.path, "r"):
                log_exists = True
        except FileNotFoundError:
            pass

        with open(self.path, "a", newline="") as f:
            writer = csv.writer(f)
            if not log_exists:
                writer.writerow(self.FIELDS)
            writer.writerow([row[field] for field in self.FIELDS])
        return True


def read_recent_log(n: int = 20):
    """Returns the last n rows from the CSV log, newest first. Empty
    list if the log doesn't exist yet."""
    try:
        with open(config.LOG_FILE, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return list(reversed(rows[-n:]))
