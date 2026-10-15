"""
Tests for the logged-only columns added 15 Oct 2026: ema20_15m, slope_15m,
slope_15m_dir, vol_ratio_projected, vol_ratio_last_closed.

They exist for the backtest comparison in
Markdowns/backtest_vwap_pullback_oct2026.md. The property that matters most is
that no rule reads them — grades and gates must be unaffected.
"""

import csv
import inspect
import os
import re
import tempfile
from datetime import datetime, timedelta

import pytest

import config
import engine
from alert_engine import AlertEngine
from decision_engine import DecisionEngine, Decision, Direction, Grade, Status
from engine import (FiveMinuteCandle, LiveFiveMinuteSession, ema20_15m_from_bars,
                    fifteen_minute_start, projected_volume_ratio)
from signal_engine import IndicatorSnapshot, SignalEngine
from volume_profile import Candle

LOGGED = ("ema20_15m", "slope_15m", "vol_ratio_projected", "vol_ratio_last_closed")
DAY = datetime(2026, 10, 15)


def bars(closes, start=DAY.replace(hour=9, minute=15), volumes=None):
    out = []
    for i, close in enumerate(closes):
        vol = volumes[i] if volumes else 1000.0
        out.append(FiveMinuteCandle(start + timedelta(minutes=5 * i),
                                    close, close, close, close, vol))
    return out


# ------------------------------------------------------------- 15m buckets

@pytest.mark.parametrize("hhmm, expected", [
    ((9, 15), (9, 15)), ((9, 25), (9, 15)), ((9, 30), (9, 30)),
    ((12, 44), (12, 30)), ((15, 25), (15, 15)), ((15, 35), (15, 30)),
])
def test_buckets_align_to_the_0915_open(hhmm, expected):
    t = DAY.replace(hour=hhmm[0], minute=hhmm[1])
    assert fifteen_minute_start(t) == DAY.replace(hour=expected[0], minute=expected[1])


def test_a_forming_15m_bucket_is_excluded():
    # 09:15, 09:20, 09:25 complete the first bucket; 09:30, 09:35 do not.
    complete = ema20_15m_from_bars(bars([100, 101, 102]))
    partial = ema20_15m_from_bars(bars([100, 101, 102, 150, 160]))
    assert complete[0] == partial[0] == 102  # bucket close = last 5m close
    assert partial[1] is None  # one completed bucket -> no slope yet


def test_ema20_and_slope_match_a_hand_fold():
    closes_15m = [100, 110, 105, 120]
    five_min = [c for c in closes_15m for _ in range(3)]
    ema, slope = ema20_15m_from_bars(bars(five_min))
    k = 2 / 21
    e = 100.0
    history = [e]
    for c in closes_15m[1:]:
        e = (c - e) * k + e
        history.append(e)
    assert ema == pytest.approx(history[-1])
    assert slope == pytest.approx(history[-1] - history[-2])


# ---------------------------------------------------------- volume ratio

@pytest.mark.parametrize("volume, sma20, elapsed", [
    (1500, 1000, 300), (900, 1000, 300), (500, 1000, 300),
    (400, 1000, 100), (700, 1000, 150),
])
def test_projected_ratio_is_the_number_the_rule_compared(volume, sma20, elapsed):
    """Pins engine.projected_volume_ratio to signal_engine's own arithmetic, so the
    logged number can't drift from the ratio the Volume rule actually used."""
    _, detail = SignalEngine()._evaluate_volume(
        Candle(100, 100, 100, 100, volume),
        IndicatorSnapshot(sma20_volume=sma20, elapsed_seconds=elapsed))
    shown = float(re.search(r"(?:relative volume|at) (\d+\.\d+)", detail).group(1))
    assert round(projected_volume_ratio(volume, sma20, elapsed), 2) == shown


def test_no_projected_ratio_before_the_rule_would_project():
    early = config.VOLUME_PROJECTION_MIN_ELAPSED_SECONDS - 1
    assert projected_volume_ratio(1000, 1000, early) is None
    assert projected_volume_ratio(1000, None, 300) is None


def test_last_closed_ratio_uses_the_20_bars_before_it():
    seed = bars([100] * 30, start=(DAY - timedelta(days=1)).replace(hour=9, minute=15),
                volumes=[1000.0] * 30)
    today = bars([100, 100], volumes=[1000.0, 3000.0])
    session = LiveFiveMinuteSession(today, DAY.replace(hour=9, minute=22),
                                    config.BIN_SIZE, config.VALUE_AREA_PCT,
                                    seed_bars=seed)
    # 09:20 is still forming at 09:22, so the last closed bar is 09:15 (1000).
    assert session.vol_ratio_last_closed == pytest.approx(1.0)
    session.advance(DAY.replace(hour=9, minute=25, second=1))  # closes 09:20 (3000)
    assert session.vol_ratio_last_closed == pytest.approx(3.0)


def test_no_last_closed_ratio_before_today_closes_a_bar():
    seed = bars([100] * 30, start=(DAY - timedelta(days=1)).replace(hour=9, minute=15))
    session = LiveFiveMinuteSession([], DAY.replace(hour=9, minute=15),
                                    config.BIN_SIZE, config.VALUE_AREA_PCT,
                                    seed_bars=seed)
    assert session.vol_ratio_last_closed is None
    assert session.snapshot()["ema20_15m"] is not None  # seeded from the prior session


# ------------------------------------------------------ never read by a rule

def test_no_rule_reads_the_logged_columns():
    for cls in (SignalEngine, DecisionEngine):
        source = inspect.getsource(cls)
        for name in LOGGED:
            assert name not in source, f"{cls.__name__} reads {name}"


# --------------------------------------------------------- written to logs

def test_alert_row_carries_the_logged_values(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ALERT_LOG_FILE", str(tmp_path / "alert_log.csv"))
    alerts = AlertEngine(enable_telegram=False)
    decision = Decision(direction=Direction.LONG, status=Status.ENTRY, grade=Grade.A,
                        signals=SignalEngine().evaluate_all(None, IndicatorSnapshot()))
    alerts.process_decision(decision, 100, bar_start=DAY, now=DAY,
                            indicators=IndicatorSnapshot(ema20_15m=55500.0, slope_15m=-3.5,
                                                         vol_ratio_projected=1.42,
                                                         vol_ratio_last_closed=0.97))
    with open(config.ALERT_LOG_FILE, newline="") as f:
        row = next(csv.DictReader(f))
    assert row["ema20_15m"] == "55500.0"
    assert row["slope_15m"] == "-3.5"
    assert row["slope_15m_dir"] == "-1"
    assert row["vol_ratio_projected"] == "1.42"
    assert row["vol_ratio_last_closed"] == "0.97"


def test_slope_direction_is_zero_for_a_flat_slope():
    assert engine.logged_htf_fields(IndicatorSnapshot(slope_15m=0.0))["slope_15m_dir"] == 0
    assert engine.logged_htf_fields(IndicatorSnapshot())["slope_15m_dir"] is None
