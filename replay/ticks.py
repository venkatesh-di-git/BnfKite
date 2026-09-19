"""
replay/ticks.py — minute candles -> a tick stream, in one of two modes.

TWO MODES, AND THE LOSING ONE IS KEPT ON PURPOSE.

  ohlc4-distance-from-open-v1   four ticks per minute      DEFAULT
  close-only-v1                 one tick per minute        the control

Close-only was the original decision, on the reasoning that expanding a candle
into four ticks "fabricates path". Measured against the live alert log over
11-14 Aug it reproduces NOTHING:

    match = same direction, same setup, <=2pt, <=60s, one-to-one
        close-only        0/56          ceiling (direction + <=90s)  11/56
        ohlc expansion    5/56          ceiling                      30/56

The mechanism is understood and was predicted in the spec, just not its size: a
five-minute bar assembled from minute CLOSES has no wicks by construction, so the
two range-derived gates cannot fire. At each of 14 Aug's 13 live alerts,
rejection blocked 11 and pullback blocked 10.

So close-only loses, decisively — and it stays in this file anyway, because 5/56
only means something next to a 0/56 measured the same way. Deleting the losing
arm would destroy the evidence for the switch.

NEITHER MODE REPRODUCES THE LIVE ALERT STREAM. 26 of 56 live alerts have no
replay counterpart within 90 seconds in either mode, and the ones that do line up
in time still disagree on price by 5-35 points, because a replay alert's price can
only ever be one of the four O/H/L/C vertices while the live price was whatever
tick happened to be trading. See Markdowns/replay_engine_spec_v3.md §0b.

See §1, §2 and §6 of that spec.
"""

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Dict, List, Optional

import config

CLOSE_ONLY_MODE = "close-only-v1"
OHLC_MODE = "ohlc4-distance-from-open-v1"

# The default, and the value pinned into the golden file. The name encodes the
# ORDERING RULE, not just "ohlc4" — the ordering is the part that would silently
# change every result if it were revised, so it has to be visible in the pin.
TICK_MODE = OHLC_MODE

# A minute candle stamped 09:19 spans 09:19:00-09:19:59, and its ticks must land in
# the 09:15 five-minute bucket. Stamping the last one at +60s would push it into the
# 09:20 bucket, silently moving the last minute of every five-minute bar into the
# next one — the bar boundaries would still look right and the volume would be in
# the wrong bar.
_CLOSE_OFFSET = timedelta(seconds=59)
_OHLC_OFFSETS = (timedelta(seconds=14), timedelta(seconds=29),
                 timedelta(seconds=44), _CLOSE_OFFSET)


@dataclass(frozen=True)
class ReplayTick:
    timestamp: datetime          # inside the minute the candle covers
    price: float
    cumulative_volume: float     # RUNNING SESSION TOTAL, not per-minute
    oi: Optional[float]


def _session_end() -> dtime:
    return dtime(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)


def _opened(candle: dict) -> datetime:
    d = candle["date"]
    return d if isinstance(d, datetime) else datetime.fromisoformat(d)


def _in_session(candles: List[dict]):
    """Candles at or after MARKET_CLOSE are dropped.

    Kite returns 385 minute candles for recent sessions, running to 15:39, which
    aggregate to 77 five-minute bars ending at 15:35. The engine's last bar starts
    at 15:25, and every hand analysis this month trimmed post-close bars. Keeping
    them would silently mismatch every cross-check, on the two bars of the day least
    likely to be eyeballed.
    """
    end = _session_end()
    for c in candles:
        t = _opened(c)
        if t.time() < end:
            yield c, t


def build_close_only_ticks(candles: List[dict]) -> List[ReplayTick]:
    """One tick per minute candle, carrying that minute's close.

    The control arm. Produces a bar whose high and low come only from minute
    closes — i.e. a bar with no wicks, which is a falsehood about the range rather
    than a conservative approximation of it.
    """
    ticks: List[ReplayTick] = []
    running = 0.0
    for c, opened in _in_session(candles):
        running += float(c["volume"] or 0.0)
        oi = c.get("oi")
        ticks.append(ReplayTick(
            timestamp=opened + _CLOSE_OFFSET,
            price=float(c["close"]),
            cumulative_volume=running,
            oi=None if oi is None else float(oi),
        ))
    return ticks


def _ohlc_sequence(o: float, h: float, l: float, c: float) -> List[float]:
    """Order the four values by DISTANCE FROM OPEN.

    High nearer the open than the low is -> O,H,L,C. Otherwise O,L,H,C.

    This is TradingView's broker-emulator heuristic. It is NOT the "up candle ->
    O,H,L,C" rule an earlier draft of the spec carried: that one inverts on exactly
    the trending bars that matter, because a strong up candle opens near its low, so
    its true path is O,L,H,C.
    """
    near, far = (h, l) if abs(h - o) <= abs(l - o) else (l, h)
    return [o, near, far, c]


def build_ohlc_ticks(candles: List[dict]) -> List[ReplayTick]:
    """Four ticks per minute candle, at +14s, +29s, +44s and +59s.

    All four prices REALLY TRADED inside that minute; only their order within it is
    inferred, and only for the minute currently forming — every earlier minute's
    contribution to the running bar high/low is locked in regardless of assumed
    order. Fabrication is confined to a narrow, decaying window. That is a weaker
    claim than "path doesn't matter": the gates read the FORMING bar, so the
    timestamp and price stamped on a replay alert are themselves artifacts of this
    ordering.

    VOLUME IS SPLIT IN EQUAL QUARTERS, not loaded onto the last tick. Live volume
    accrues continuously; loading it at +59s leaves three ticks with a zero delta and
    makes projected relative volume sawtooth — and the volume gate is one of the six
    things replay exists to measure. The final quarter absorbs the rounding so the
    running total is exact at every minute boundary.
    """
    ticks: List[ReplayTick] = []
    running = 0.0
    for c, opened in _in_session(candles):
        vol = float(c["volume"] or 0.0)
        oi = c.get("oi")
        oi = None if oi is None else float(oi)
        prices = _ohlc_sequence(float(c["open"]), float(c["high"]),
                                float(c["low"]), float(c["close"]))
        for k, (offset, price) in enumerate(zip(_OHLC_OFFSETS, prices)):
            running += (vol - 3.0 * (vol / 4.0)) if k == 3 else vol / 4.0
            # OI is a minute-resolution figure; all four ticks of a minute carry it.
            ticks.append(ReplayTick(timestamp=opened + offset, price=price,
                                    cumulative_volume=running, oi=oi))
    return ticks


BUILDERS: Dict[str, Callable[[List[dict]], List[ReplayTick]]] = {
    CLOSE_ONLY_MODE: build_close_only_ticks,
    OHLC_MODE: build_ohlc_ticks,
}


def build_ticks(candles: List[dict], mode: str = None) -> List[ReplayTick]:
    """Dispatch on tick mode. Defaults to TICK_MODE.

    cumulative_volume is monotonically non-decreasing in both modes, matching
    KiteTicker's day-cumulative volume_traded. This is the one thing that must not
    be got wrong: apply_tick DIFFS this value (engine.py:270-274), so handing it
    per-minute volume yields near-zero deltas, a volume gate that never fires, and
    no error of any kind.
    """
    mode = mode or TICK_MODE
    try:
        return BUILDERS[mode](candles)
    except KeyError:
        raise ValueError(
            f"unknown tick mode {mode!r}; known: {sorted(BUILDERS)}") from None
