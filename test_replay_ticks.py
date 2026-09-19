"""
Tests for replay/ticks.py — both tick modes.

Three of these guard failures that produce no error at all, which is why they are
tests rather than review notes: per-minute volume instead of cumulative silently
kills the volume gate; a +60s stamp silently moves the last minute of every
five-minute bar into the next one; and an inverted ordering rule silently changes
every result while still looking like a plausible path.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
from engine import five_minute_start
from replay.data import fetch_minute_candles
from replay.ticks import (BUILDERS, CLOSE_ONLY_MODE, OHLC_MODE, TICK_MODE,
                          ReplayTick, build_close_only_ticks, build_ohlc_ticks,
                          build_ticks)

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "BANKNIFTY26AUGFUT"
DAY = date(2026, 8, 14)


def _candles(n=10, start_hour=9, start_minute=15, volume=100.0):
    t = datetime(2026, 8, 14, start_hour, start_minute, tzinfo=IST)
    out = []
    for i in range(n):
        out.append({"date": (t + timedelta(minutes=i)).isoformat(),
                    "open": 57800.0 + i, "high": 57805.0 + i, "low": 57795.0 + i,
                    "close": 57800.0 + i, "volume": volume, "oi": 2_000_000.0 + i})
    return out


def _archived():
    return fetch_minute_candles(None, SYMBOL, 0, DAY, allow_api=False)


# --------------------------------------------------------------- tick counts

def test_close_only_is_one_tick_per_candle():
    assert len(build_close_only_ticks(_candles(10))) == 10


def test_ohlc_is_four_ticks_per_candle():
    assert len(build_ohlc_ticks(_candles(10))) == 40


def test_empty_input():
    assert build_close_only_ticks([]) == []
    assert build_ohlc_ticks([]) == []


# ------------------------------------------------------------------- volume

@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_volume_is_a_running_session_total(builder):
    """The trap. apply_tick diffs cumulative volume (engine.py:270), so per-minute
    figures produce near-zero deltas and a volume gate that never fires — with no
    exception raised anywhere."""
    ticks = builder(_candles(5, volume=100.0))
    assert ticks[-1].cumulative_volume == 500.0
    vols = [t.cumulative_volume for t in ticks]
    assert all(b >= a for a, b in zip(vols, vols[1:]))


@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_cumulative_volume_never_decreases_on_real_data(builder):
    vols = [t.cumulative_volume for t in builder(_archived())]
    assert all(b >= a for a, b in zip(vols, vols[1:])), "cumulative volume went backwards"


def test_quarter_split_sums_to_the_candle_volume_exactly():
    """Each minute's four quarters must total that minute's volume EXACTLY, or the
    session total drifts and the volume baseline drifts with it. The fourth quarter
    absorbs the rounding for this reason."""
    raw = _archived()
    ticks = build_ohlc_ticks(raw)
    in_session = [c for c in raw
                  if datetime.fromisoformat(c["date"]).time()
                  < datetime(2026, 1, 1, config.MARKET_CLOSE_HOUR,
                             config.MARKET_CLOSE_MINUTE).time()]
    running = 0.0
    for i, c in enumerate(in_session):
        running += float(c["volume"] or 0.0)
        assert ticks[i * 4 + 3].cumulative_volume == running, f"drift at minute {i}"


def test_quarter_split_is_not_loaded_onto_the_last_tick():
    """Loading the minute's volume at +59s leaves three ticks with a zero delta and
    makes projected relative volume sawtooth."""
    deltas, prev = [], 0.0
    for t in build_ohlc_ticks(_candles(1, volume=100.0)):
        deltas.append(t.cumulative_volume - prev)
        prev = t.cumulative_volume
    assert deltas == [25.0, 25.0, 25.0, 25.0]


# ----------------------------------------------------------------- ordering

def test_ordering_high_closer_to_open():
    c = [{"date": datetime(2026, 8, 14, 9, 15, tzinfo=IST).isoformat(),
          "open": 100.0, "high": 102.0, "low": 90.0, "close": 95.0,
          "volume": 4.0, "oi": 1.0}]
    assert [t.price for t in build_ohlc_ticks(c)] == [100.0, 102.0, 90.0, 95.0]


def test_ordering_low_closer_to_open():
    """A strong up candle opens near its LOW, so its path is O,L,H,C. The older
    'up candle -> O,H,L,C' rule inverts on exactly these bars."""
    c = [{"date": datetime(2026, 8, 14, 9, 15, tzinfo=IST).isoformat(),
          "open": 100.0, "high": 130.0, "low": 98.0, "close": 128.0,
          "volume": 4.0, "oi": 1.0}]
    assert [t.price for t in build_ohlc_ticks(c)] == [100.0, 98.0, 130.0, 128.0]


def test_ordering_ties_go_to_the_high():
    c = [{"date": datetime(2026, 8, 14, 9, 15, tzinfo=IST).isoformat(),
          "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0,
          "volume": 4.0, "oi": 1.0}]
    assert [t.price for t in build_ohlc_ticks(c)] == [100.0, 110.0, 90.0, 105.0]


@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_every_real_price_is_inside_its_candle_range(builder):
    """Neither mode may invent a price outside what actually traded that minute."""
    raw = _archived()
    by_minute = {datetime.fromisoformat(c["date"]).replace(tzinfo=None): c for c in raw}
    for t in builder(raw):
        c = by_minute[t.timestamp.replace(tzinfo=None, second=0, microsecond=0)]
        assert c["low"] <= t.price <= c["high"]


# -------------------------------------------------------------------- time

@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_ticks_land_in_their_own_five_minute_bucket(builder):
    """A 09:19 candle must produce ticks in the 09:15 bucket, not 09:20. Stamping
    the last at +60s instead of +59s moves the last minute of every bar into the
    next one: bar boundaries still look correct and the volume is in the wrong bar."""
    for tick in builder(_candles(10)):
        candle_open = tick.timestamp.replace(second=0, microsecond=0)
        assert five_minute_start(tick.timestamp) == five_minute_start(candle_open)


@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_last_minute_of_a_bar_is_not_promoted(builder):
    ticks = builder(_candles(5))                          # 09:15..09:19
    assert five_minute_start(ticks[-1].timestamp) == datetime(2026, 8, 14, 9, 15, tzinfo=IST)


@pytest.mark.parametrize("builder,expected", [(build_close_only_ticks, 375),
                                              (build_ohlc_ticks, 1500)])
def test_post_close_candles_are_dropped(builder, expected):
    """Recent sessions carry 385 minute candles running to 15:39, aggregating to 77
    five-minute bars ending 15:35; the engine's last bar starts at 15:25."""
    raw = _archived()
    assert len(raw) == 385, "fixture changed — 14 Aug should have 385 minute candles"
    ticks = builder(raw)
    assert len(ticks) == expected
    last = ticks[-1].timestamp
    assert (last.hour, last.minute) == (15, 29)
    assert five_minute_start(last) == datetime(2026, 8, 14, 15, 25, tzinfo=IST)


@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_timestamps_are_strictly_increasing(builder):
    ts = [t.timestamp for t in builder(_archived())]
    assert all(b > a for a, b in zip(ts, ts[1:]))


# ---------------------------------------------------------------------- oi

@pytest.mark.parametrize("builder", [build_close_only_ticks, build_ohlc_ticks])
def test_oi_reaches_the_tick(builder):
    assert all(t.oi is not None for t in builder(_archived())), "OI is one of the six gates"


def test_a_minutes_oi_is_carried_by_all_four_of_its_ticks():
    ticks = build_ohlc_ticks(_candles(3))
    for i in range(3):
        quartet = ticks[i * 4:(i + 1) * 4]
        assert len({t.oi for t in quartet}) == 1
        assert quartet[0].oi == 2_000_000.0 + i


# ---------------------------------------------------------------- dispatch

def test_default_mode_is_ohlc():
    """OHLC is the default because close-only reproduced 0 of 56 live alerts."""
    assert TICK_MODE == OHLC_MODE == "ohlc4-distance-from-open-v1"


def test_close_only_mode_is_still_available_as_the_control():
    """Kept deliberately: 5/56 only means something next to a 0/56 measured the
    same way."""
    assert CLOSE_ONLY_MODE in BUILDERS
    assert len(build_ticks(_candles(10), mode=CLOSE_ONLY_MODE)) == 10


def test_build_ticks_dispatches_on_mode():
    assert len(build_ticks(_candles(10))) == 40                       # default OHLC
    assert len(build_ticks(_candles(10), mode=OHLC_MODE)) == 40


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown tick mode"):
        build_ticks(_candles(1), mode="ohlc4")


def test_ticks_are_immutable():
    t = build_ohlc_ticks(_candles(1))[0]
    with pytest.raises(Exception):
        t.price = 1.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
