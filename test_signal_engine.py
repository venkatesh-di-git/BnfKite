"""
Sanity tests for signal_engine.py, one group per
Signal_Decision_Engine_Specification_V1.md's Unit Tests list: Trend,
Pullback, Rejection, Volume, OI, Volume Profile.
"""

import config
from volume_profile import Candle
from signal_engine import (
    IndicatorSnapshot, LevelState, OpenInterestState, PullbackState,
    RejectionState, SignalEngine, TrendState, VolumeState,
)

engine = SignalEngine()


def candle(open_=100, high=105, low=95, close=100, volume=1000):
    return Candle(open=open_, high=high, low=low, close=close, volume=volume)


def ind(**kwargs):
    return IndicatorSnapshot(**kwargs)


# --- Trend ---

def test_trend_bullish():
    c = candle(close=110)
    i = ind(vwap=100, ema10=105, ema10_prev=100)  # slope +5
    state, _ = engine._evaluate_trend(c, i)
    assert state == TrendState.BULLISH
    print("PASS: trend bullish\n")


def test_trend_bearish():
    c = candle(close=90)
    i = ind(vwap=100, ema10=95, ema10_prev=100)  # slope -5
    state, _ = engine._evaluate_trend(c, i)
    assert state == TrendState.BEARISH
    print("PASS: trend bearish\n")


def test_trend_bullish_on_vwap_reclaim_before_ema_catches_up():
    """The reclaim window, and the reason the Trend gate was one-sided.

    EMA10 is a smoothed, lagging value: on a genuine reclaim price crosses VWAP
    first and EMA10 follows minutes later. Requiring BOTH above VWAP meant that
    window never counted. On the 05 Aug tape close>VWAP held on 41 rows but
    close AND ema10 on just 1 — the EMA term discarded 40 of 41 — while the
    mirror case (close<VWAP, ema10>VWAP) occurred exactly 0 times, so the lag
    is entirely one-directional.

    Price must still be on the right side of VWAP; EMA10 no longer has to have
    caught up. Fails before the change with NEUTRAL."""
    c = candle(close=110)
    i = ind(vwap=100, ema10=95, ema10_prev=90)  # close above VWAP, EMA10 still below, slope +5
    state, _ = engine._evaluate_trend(c, i)
    assert state == TrendState.BULLISH, f"expected Bullish on a reclaim, got {state}"
    print("PASS: a VWAP reclaim reads Bullish before EMA10 catches up\n")


def test_trend_bearish_on_vwap_loss_before_ema_catches_up():
    """Mirror of the reclaim case. Never observed on 05 Aug (0 rows), but the
    rule must be symmetric — an asymmetric gate is how the one-sidedness arose."""
    c = candle(close=90)
    i = ind(vwap=100, ema10=105, ema10_prev=110)  # close below VWAP, EMA10 still above, slope -5
    state, _ = engine._evaluate_trend(c, i)
    assert state == TrendState.BEARISH, f"expected Bearish on a loss, got {state}"
    print("PASS: a VWAP loss reads Bearish before EMA10 catches up\n")


def test_trend_still_requires_price_on_the_right_side_of_vwap():
    """Proves only the EMA10 conjunct was dropped, not the whole VWAP gate.
    A strong positive slope while price is BELOW VWAP must stay Neutral —
    if this ever returns Bullish, both terms were removed by mistake."""
    c = candle(close=90)                                  # below VWAP
    i = ind(vwap=100, ema10=95, ema10_prev=90)            # slope +5, strongly up
    state, _ = engine._evaluate_trend(c, i)
    assert state == TrendState.NEUTRAL, f"price below VWAP must not read Bullish, got {state}"

    c = candle(close=110)                                 # above VWAP
    i = ind(vwap=100, ema10=105, ema10_prev=110)          # slope -5, strongly down
    state, _ = engine._evaluate_trend(c, i)
    assert state == TrendState.NEUTRAL, f"price above VWAP must not read Bearish, got {state}"
    print("PASS: price must still be on the right side of VWAP\n")


def test_trend_unknown_on_missing_data():
    state, _ = engine._evaluate_trend(None, ind())
    assert state == TrendState.UNKNOWN
    print("PASS: trend unknown when candle missing\n")


# --- Trend hysteresis (Schmitt trigger) ---
#
# Like a PLC analog alarm that trips at 80 and clears at 78: entering a trend
# demands slope beyond +/-h, but holding one only demands slope hasn't reversed
# past the opposite edge. `previous` carries the state the trigger is in.

def bullish_snapshot(trend=TrendState.BULLISH):
    """A previous snapshot whose only field the Trend rule reads is `trend`."""
    from signal_engine import SignalSnapshot, PullbackState, RejectionState
    return SignalSnapshot(
        trend=trend, pullback=PullbackState.NONE, rejection=RejectionState.NONE,
        volume=VolumeState.NORMAL, open_interest=OpenInterestState.FLAT,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE)


def with_hysteresis(points):
    original = config.EMA_SLOPE_HYSTERESIS_POINTS
    config.EMA_SLOPE_HYSTERESIS_POINTS = points
    return original


def test_trend_requires_slope_beyond_the_enter_threshold():
    original = with_hysteresis(2.0)
    try:
        c = candle(close=110)
        weak, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=104))  # +1
        assert weak == TrendState.NEUTRAL, "slope inside the band must not trip"

        strong, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=100))  # +5
        assert strong == TrendState.BULLISH, "slope beyond +h must trip"
    finally:
        config.EMA_SLOPE_HYSTERESIS_POINTS = original
    print("PASS: entering a trend needs slope beyond +h\n")


def test_trend_holds_once_entered_until_slope_reverses_past_the_exit():
    """The core of a Schmitt trigger: the exit threshold is not the entry one."""
    original = with_hysteresis(2.0)
    try:
        c, was_bull = candle(close=110), bullish_snapshot()

        held, detail = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=104), was_bull)
        assert held == TrendState.BULLISH, "slope +1 should HOLD Bullish, not reset to Neutral"
        assert "holding" in detail

        still, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=106), was_bull)
        assert still == TrendState.BULLISH, "slope -1 is inside the band — still Bullish"

        cleared, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=108), was_bull)
        assert cleared == TrendState.NEUTRAL, "slope -3 is past -h — must clear"
    finally:
        config.EMA_SLOPE_HYSTERESIS_POINTS = original
    print("PASS: a trend holds until slope reverses past the exit threshold\n")


def test_slope_chattering_at_zero_yields_one_stable_state():
    """THE regression. A slope oscillating either side of zero used to flip the
    state on every evaluation; with hysteresis it must settle on exactly one."""
    original = with_hysteresis(2.0)
    try:
        c = candle(close=110)
        # Trip Bullish once, then chatter around zero.
        state, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=100))
        assert state == TrendState.BULLISH
        previous, seen = bullish_snapshot(state), set()
        for prev_ema in (104.5, 105.5, 104.8, 105.2, 105.9, 104.2, 105.1):
            state, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=prev_ema),
                                              previous)
            seen.add(state)
            previous = bullish_snapshot(state)
        assert seen == {TrendState.BULLISH}, f"state chattered across {seen}"
    finally:
        config.EMA_SLOPE_HYSTERESIS_POINTS = original
    print("PASS: a slope chattering at zero produces one stable state\n")


def test_trend_without_previous_uses_plain_thresholds():
    """previous=None must reproduce the un-hysteresised behaviour, which is what
    keeps every other test in this file valid."""
    original = with_hysteresis(2.0)
    try:
        c = candle(close=110)
        state, _ = engine._evaluate_trend(c, ind(vwap=100, ema10=105, ema10_prev=104), None)
        assert state == TrendState.NEUTRAL, "no previous state means no holding"
    finally:
        config.EMA_SLOPE_HYSTERESIS_POINTS = original
    print("PASS: previous=None falls back to plain thresholds\n")


def test_volume_holds_high_until_the_ratio_falls_past_the_band():
    original = config.VOLUME_HYSTERESIS
    config.VOLUME_HYSTERESIS = 0.05
    try:
        was_high = bullish_snapshot()
        was_high.volume = VolumeState.HIGH
        # 1.28 is below the 1.30 entry but above the 1.25 exit.
        held, _ = engine._evaluate_volume(candle(volume=128_000),
                                          ind(sma20_volume=100_000, elapsed_seconds=300), was_high)
        assert held == VolumeState.HIGH, "1.28 should hold High"

        dropped, _ = engine._evaluate_volume(candle(volume=124_000),
                                             ind(sma20_volume=100_000, elapsed_seconds=300), was_high)
        assert dropped == VolumeState.NORMAL, "1.24 is past the exit — must drop"

        fresh, _ = engine._evaluate_volume(candle(volume=128_000),
                                           ind(sma20_volume=100_000, elapsed_seconds=300), None)
        assert fresh == VolumeState.NORMAL, "without a previous High, 1.28 never reaches High"
    finally:
        config.VOLUME_HYSTERESIS = original
    print("PASS: Volume holds High until the ratio falls past the band\n")


def test_volume_holds_low_until_the_ratio_rises_past_the_band():
    """The Low edge must move the opposite way — harder to escape Low, not easier."""
    original = config.VOLUME_HYSTERESIS
    config.VOLUME_HYSTERESIS = 0.05
    try:
        was_low = bullish_snapshot()
        was_low.volume = VolumeState.LOW
        held, _ = engine._evaluate_volume(candle(volume=82_000),
                                          ind(sma20_volume=100_000, elapsed_seconds=300), was_low)
        assert held == VolumeState.LOW, "0.82 is below the 0.85 exit — should hold Low"

        escaped, _ = engine._evaluate_volume(candle(volume=86_000),
                                             ind(sma20_volume=100_000, elapsed_seconds=300), was_low)
        assert escaped == VolumeState.NORMAL, "0.86 is past the exit — must rise to Normal"
    finally:
        config.VOLUME_HYSTERESIS = original
    print("PASS: Volume holds Low until the ratio rises past the band\n")


# --- Pullback ---

def test_pullback_bullish():
    c = candle(low=95, close=101)
    state, _ = engine._evaluate_pullback(c, ind(ema10=100))
    assert state == PullbackState.BULLISH
    print("PASS: pullback bullish\n")


def test_pullback_bearish():
    c = candle(high=105, close=99)
    state, _ = engine._evaluate_pullback(c, ind(ema10=100))
    assert state == PullbackState.BEARISH
    print("PASS: pullback bearish\n")


def test_pullback_none():
    c = candle(low=101, high=104, close=102)  # never touches EMA10=100
    state, _ = engine._evaluate_pullback(c, ind(ema10=100))
    assert state == PullbackState.NONE
    print("PASS: pullback none\n")


def test_pullback_unknown_on_missing_ema():
    state, _ = engine._evaluate_pullback(candle(), ind(ema10=None))
    assert state == PullbackState.UNKNOWN
    print("PASS: pullback unknown when EMA10 missing\n")


# --- Rejection ---

def test_rejection_bullish():
    c = candle(open_=99, low=95, close=102)  # lower wick, bullish candle
    state, _ = engine._evaluate_rejection(c, ind(ema10=100))
    assert state == RejectionState.BULLISH
    print("PASS: rejection bullish\n")


def test_rejection_bearish():
    c = candle(open_=101, high=105, close=98)  # upper wick, bearish candle
    state, _ = engine._evaluate_rejection(c, ind(ema10=100))
    assert state == RejectionState.BEARISH
    print("PASS: rejection bearish\n")


def test_rejection_none_without_wick():
    c = candle(open_=99, low=99, high=102, close=102)  # no lower wick
    state, _ = engine._evaluate_rejection(c, ind(ema10=100))
    assert state == RejectionState.NONE
    print("PASS: rejection none without a wick\n")


# --- VWAP Pullback / Rejection (the second setup) ---
#
# Geometry throughout is the real 06 Aug 11:50 bar, where price pulled back to
# VWAP and reversed 100 points with nothing alerting. Using the actual numbers
# means these tests fail if the rule stops covering the case it was written for.

VWAP_BAR = dict(open_=58014.0, high=58045.8, low=58014.0, close=58045.8)
VWAP_AT_LOW = 58012.49   # price turned 1.51 points ABOVE it, never crossing


def test_vwap_pullback_bullish_on_a_near_touch():
    """The case the EMA10-shaped rule would miss: low 58014.00 never reaches
    VWAP 58012.49, so a strict `low <= vwap` straddle records nothing."""
    state, detail = engine._evaluate_vwap_pullback(candle(**VWAP_BAR), ind(vwap=VWAP_AT_LOW))
    assert state == PullbackState.BULLISH, detail
    assert candle(**VWAP_BAR).low > VWAP_AT_LOW, "precondition: price never crossed VWAP"
    print("PASS: VWAP pullback fires on a near touch that never crosses\n")


def test_vwap_pullback_none_when_price_is_far_above():
    """The 11:55 bar, 54 points above VWAP — not a pullback by any reading."""
    c = candle(open_=58066.8, high=58100.0, low=58066.8, close=58099.8)
    state, _ = engine._evaluate_vwap_pullback(c, ind(vwap=58012.7))
    assert state == PullbackState.NONE
    print("PASS: VWAP pullback stays NONE when price is far above VWAP\n")


def test_vwap_pullback_proximity_boundary():
    """Exactly at the band fires; one point beyond does not."""
    band = config.LEVEL_PROXIMITY_POINTS
    at_edge = candle(open_=100, high=120, low=100 + band, close=115)
    beyond = candle(open_=100, high=120, low=100 + band + 1, close=115)
    assert engine._evaluate_vwap_pullback(at_edge, ind(vwap=100))[0] == PullbackState.BULLISH
    assert engine._evaluate_vwap_pullback(beyond, ind(vwap=100))[0] == PullbackState.NONE
    print("PASS: VWAP pullback respects the proximity boundary\n")


def test_vwap_pullback_bearish_mirrors_bullish():
    c = candle(open_=100, high=99, low=80, close=85)  # high near VWAP, close below
    state, _ = engine._evaluate_vwap_pullback(c, ind(vwap=100))
    assert state == PullbackState.BEARISH
    print("PASS: VWAP pullback bearish mirrors the bullish case\n")


def test_vwap_pullback_unknown_without_vwap():
    state, _ = engine._evaluate_vwap_pullback(candle(), ind())
    assert state == PullbackState.UNKNOWN
    print("PASS: VWAP pullback is UNKNOWN without VWAP\n")


def test_vwap_rejection_fires_on_a_bar_that_opened_at_its_low():
    """The whole reason this rule is close-position-in-range rather than a wick
    test. The 11:50 bar opened AT its low, so the EMA10 rule's
    `low < min(open, close)` is false for the entire life of the bar — and a
    clean V-reversal is exactly the shape that produces it."""
    c = candle(**VWAP_BAR)
    assert not (c.low < min(c.open, c.close)), "precondition: no wick by the strict test"
    assert engine._evaluate_rejection(c, ind(ema10=58000))[0] == RejectionState.NONE
    state, detail = engine._evaluate_vwap_rejection(c, ind(vwap=VWAP_AT_LOW))
    assert state == RejectionState.BULLISH, detail
    print("PASS: VWAP rejection fires where the wick test structurally cannot\n")


def test_vwap_rejection_none_when_close_gives_back_the_range():
    """The 06 Aug 11:40 bar: probed up, then closed on its low. 0% held."""
    c = candle(open_=58064.8, high=58071.8, low=58034.2, close=58034.2)
    state, _ = engine._evaluate_vwap_rejection(c, ind(vwap=58012.2))
    assert state == RejectionState.NONE
    print("PASS: VWAP rejection stays NONE when the close gives back the range\n")


def test_vwap_rejection_boundary_at_the_configured_fraction():
    held = config.VWAP_REJECTION_CLOSE_PCT
    span = 100.0
    exactly = candle(open_=1, high=100 + span, low=100, close=100 + span * held)
    just_under = candle(open_=1, high=100 + span, low=100, close=100 + span * held - 1)
    assert engine._evaluate_vwap_rejection(exactly, ind(vwap=50))[0] == RejectionState.BULLISH
    assert engine._evaluate_vwap_rejection(just_under, ind(vwap=50))[0] == RejectionState.NONE
    print("PASS: VWAP rejection respects VWAP_REJECTION_CLOSE_PCT exactly\n")


def test_vwap_rejection_requires_the_right_side_of_vwap():
    """Held its range, but below VWAP — that is not a bullish rejection."""
    c = candle(**VWAP_BAR)
    state, _ = engine._evaluate_vwap_rejection(c, ind(vwap=58999.0))
    assert state == RejectionState.NONE
    print("PASS: VWAP rejection requires the close on the right side of VWAP\n")


def test_vwap_rejection_zero_range_bar_does_not_divide_by_zero():
    """snapshot() hands out a bar with high == low == close at every bar
    boundary — once per 5 minutes, every session."""
    c = candle(open_=100, high=100, low=100, close=100)
    state, detail = engine._evaluate_vwap_rejection(c, ind(vwap=90))
    assert state == RejectionState.NONE, detail
    print("PASS: a zero-range bar returns NONE rather than dividing by zero\n")


def test_vwap_rules_do_not_disturb_the_ema_rules():
    """Same candle through both pairs: they answer about different levels and
    must be free to disagree."""
    c = candle(**VWAP_BAR)
    i = ind(ema10=58050.0, vwap=VWAP_AT_LOW)
    assert engine._evaluate_pullback(c, i)[0] == PullbackState.NONE      # close below EMA10
    assert engine._evaluate_vwap_pullback(c, i)[0] == PullbackState.BULLISH
    print("PASS: the EMA10 and VWAP rules answer independently\n")


def test_evaluate_all_populates_both_setups():
    snapshot = engine.evaluate_all(candle(**VWAP_BAR),
                                   ind(vwap=VWAP_AT_LOW, ema10=58050.0, ema10_prev=58040.0,
                                       sma20_volume=1000))
    assert snapshot.vwap_pullback == PullbackState.BULLISH
    assert snapshot.vwap_rejection == RejectionState.BULLISH
    assert "vwap_pullback" in snapshot.details and "vwap_rejection" in snapshot.details
    print("PASS: evaluate_all populates the VWAP setup and its details\n")


# --- Volume ---

def test_volume_high():
    state, _ = engine._evaluate_volume(candle(volume=1500), ind(sma20_volume=1000))
    assert state == VolumeState.HIGH
    print("PASS: volume high\n")


def test_volume_normal():
    state, _ = engine._evaluate_volume(candle(volume=1000), ind(sma20_volume=1000))
    assert state == VolumeState.NORMAL
    print("PASS: volume normal\n")


def test_volume_low():
    state, _ = engine._evaluate_volume(candle(volume=500), ind(sma20_volume=1000))
    assert state == VolumeState.LOW
    print("PASS: volume low\n")


def test_volume_unknown_without_sma():
    state, _ = engine._evaluate_volume(candle(), ind(sma20_volume=None))
    assert state == VolumeState.UNKNOWN
    print("PASS: volume unknown without SMA20\n")


# The four tests above pass no elapsed_seconds, so they exercise the
# "treat as complete, no projection" default — kept unchanged deliberately,
# as evidence that projection is backwards-compatible for a full bar.


def test_projection_makes_an_early_bar_tradable():
    """The false-negative this fixes: 40k traded in the first 60s of a bar
    whose average is 100k reads Low un-projected (0.40) and never clears the
    mandatory gate, even though it is running at twice normal pace."""
    raw, _ = engine._evaluate_volume(candle(volume=40_000), ind(sma20_volume=100_000,
                                                                elapsed_seconds=300))
    assert raw == VolumeState.LOW, "precondition: un-projected, this is Low"

    state, detail = engine._evaluate_volume(candle(volume=40_000),
                                            ind(sma20_volume=100_000, elapsed_seconds=60))
    assert state == VolumeState.HIGH, f"expected High, got {state} ({detail})"
    print("PASS: projection surfaces an early bar running at pace\n")


def test_completed_bar_is_not_projected():
    state, _ = engine._evaluate_volume(candle(volume=100_000),
                                       ind(sma20_volume=100_000, elapsed_seconds=300))
    assert state == VolumeState.NORMAL
    print("PASS: a completed bar is compared unprojected\n")


def test_volume_floor_blocks_projection_from_a_tiny_numerator():
    state, detail = engine._evaluate_volume(candle(volume=20_000),
                                            ind(sma20_volume=100_000, elapsed_seconds=60))
    assert state == VolumeState.UNKNOWN
    assert "floor" in detail
    print("PASS: the volume floor reports Unknown, not a projected spike\n")


def test_thin_late_bar_reports_low_not_unknown():
    """Regression, from the live 05 Aug 10:45 bar. It traded 4,110 against an
    SMA20 of 17,562 with 285s of 300 elapsed — a 0.24 ratio, unmistakably Low.
    The floor applied to every state, so it read Unknown ("too early to
    project") when the bar was 95% complete, hiding the state right up to the
    close. Fails before the fix with Unknown."""
    state, detail = engine._evaluate_volume(candle(volume=4_110),
                                            ind(sma20_volume=17_562, elapsed_seconds=285))
    assert state == VolumeState.LOW, f"expected Low, got {state} ({detail})"
    assert 4_110 < config.VOLUME_PROJECTION_FLOOR_PCT * 17_562, "precondition: below the floor"
    print("PASS: a thin bar late in its window reports Low, not Unknown\n")


def test_floor_still_blocks_a_thin_bar_reading_high():
    """The other half of the asymmetry. High and Normal are the states that
    open the Decision Engine's volume gate, so an untrusted projection must
    not reach them. Guard, not a regression — this passed before the fix too."""
    for volume, elapsed, would_be in [(4_300, 45, "High"), (4_000, 60, "Normal")]:
        state, detail = engine._evaluate_volume(candle(volume=volume),
                                                ind(sma20_volume=17_562, elapsed_seconds=elapsed))
        assert state == VolumeState.UNKNOWN, f"{would_be} case: got {state} ({detail})"
        assert "floor" in detail
    print("PASS: the floor still blocks a thin bar projecting to High or Normal\n")


def test_low_from_a_thin_bar_still_blocks_entry():
    """Reporting Low instead of Unknown must not open the gate: the Decision
    Engine accepts only High and Normal, so both readings block equally."""
    state, _ = engine._evaluate_volume(candle(volume=4_110),
                                       ind(sma20_volume=17_562, elapsed_seconds=285))
    assert state not in (VolumeState.HIGH, VolumeState.NORMAL)
    print("PASS: a thin-bar Low blocks entry exactly as Unknown did\n")


def test_min_elapsed_guard_blocks_a_large_multiplier():
    """Passes the volume floor but is 5s into the bar — the floor bounds the
    numerator, not the multiplier, so this guard is what stops a 60x spike."""
    state, detail = engine._evaluate_volume(candle(volume=50_000),
                                            ind(sma20_volume=100_000, elapsed_seconds=5))
    assert state == VolumeState.UNKNOWN
    assert "into bar" in detail
    print("PASS: the min-elapsed guard blocks an early large multiplier\n")


def test_missing_elapsed_defaults_to_no_projection():
    state, _ = engine._evaluate_volume(candle(volume=100_000), ind(sma20_volume=100_000))
    assert state == VolumeState.NORMAL
    print("PASS: absent elapsed means no projection\n")


def test_elapsed_clamps_above_bar_seconds():
    """A stalled feed can leave a bar older than its own window; without the
    clamp the projection would shrink it to Low."""
    state, _ = engine._evaluate_volume(candle(volume=100_000),
                                       ind(sma20_volume=100_000, elapsed_seconds=600))
    assert state == VolumeState.NORMAL
    print("PASS: elapsed clamps to BAR_SECONDS\n")


def test_projected_band_boundaries():
    high, _ = engine._evaluate_volume(candle(volume=65_000),
                                      ind(sma20_volume=100_000, elapsed_seconds=150))
    assert high == VolumeState.HIGH, "projected exactly 1.30 should be High"

    normal, _ = engine._evaluate_volume(candle(volume=40_000),
                                        ind(sma20_volume=100_000, elapsed_seconds=150))
    assert normal == VolumeState.NORMAL, "projected exactly 0.80 should be Normal"
    print("PASS: projected ratios land correctly on the band boundaries\n")


# --- Open Interest ---

def test_oi_rising():
    state, _ = engine._evaluate_open_interest(ind(current_oi=105000, previous_oi=100000))
    assert state == OpenInterestState.RISING
    print("PASS: oi rising\n")


def test_oi_falling():
    state, _ = engine._evaluate_open_interest(ind(current_oi=95000, previous_oi=100000))
    assert state == OpenInterestState.FALLING
    print("PASS: oi falling\n")


def test_oi_flat():
    state, _ = engine._evaluate_open_interest(ind(current_oi=100010, previous_oi=100000))
    assert state == OpenInterestState.FLAT
    print("PASS: oi flat\n")


def test_oi_unknown_without_previous():
    state, _ = engine._evaluate_open_interest(ind(current_oi=100000, previous_oi=None))
    assert state == OpenInterestState.UNKNOWN
    print("PASS: oi unknown without previous OI\n")


# --- Volume Profile (POC / VAH / VAL) ---

def test_volume_profile_states():
    i = ind(poc=57450, vah=57500, val=57400)

    poc, vah, val, _ = engine._evaluate_volume_profile(candle(close=57450), i)
    assert poc == LevelState.AT and vah == LevelState.BELOW and val == LevelState.ABOVE

    poc, vah, val, _ = engine._evaluate_volume_profile(candle(close=57495), i)
    assert vah == LevelState.REJECTED

    poc, vah, val, _ = engine._evaluate_volume_profile(candle(close=57405), i)
    assert val == LevelState.REJECTED

    poc, vah, val, _ = engine._evaluate_volume_profile(candle(close=57600), i)
    assert poc == LevelState.ABOVE and vah == LevelState.ABOVE

    poc, vah, val, _ = engine._evaluate_volume_profile(candle(close=57300), i)
    assert poc == LevelState.BELOW and val == LevelState.BELOW
    print("PASS: volume profile above/below/at/rejected states\n")


def test_volume_profile_unknown_without_levels():
    poc, vah, val, _ = engine._evaluate_volume_profile(candle(), ind())
    assert poc == vah == val == LevelState.UNKNOWN
    print("PASS: volume profile unknown without POC/VAH/VAL\n")


if __name__ == "__main__":
    test_trend_bullish()
    test_trend_bearish()
    test_trend_bullish_on_vwap_reclaim_before_ema_catches_up()
    test_trend_bearish_on_vwap_loss_before_ema_catches_up()
    test_trend_still_requires_price_on_the_right_side_of_vwap()
    test_trend_unknown_on_missing_data()
    test_trend_requires_slope_beyond_the_enter_threshold()
    test_trend_holds_once_entered_until_slope_reverses_past_the_exit()
    test_slope_chattering_at_zero_yields_one_stable_state()
    test_trend_without_previous_uses_plain_thresholds()
    test_volume_holds_high_until_the_ratio_falls_past_the_band()
    test_volume_holds_low_until_the_ratio_rises_past_the_band()
    test_pullback_bullish()
    test_pullback_bearish()
    test_pullback_none()
    test_pullback_unknown_on_missing_ema()
    test_rejection_bullish()
    test_rejection_bearish()
    test_rejection_none_without_wick()
    test_vwap_pullback_bullish_on_a_near_touch()
    test_vwap_pullback_none_when_price_is_far_above()
    test_vwap_pullback_proximity_boundary()
    test_vwap_pullback_bearish_mirrors_bullish()
    test_vwap_pullback_unknown_without_vwap()
    test_vwap_rejection_fires_on_a_bar_that_opened_at_its_low()
    test_vwap_rejection_none_when_close_gives_back_the_range()
    test_vwap_rejection_boundary_at_the_configured_fraction()
    test_vwap_rejection_requires_the_right_side_of_vwap()
    test_vwap_rejection_zero_range_bar_does_not_divide_by_zero()
    test_vwap_rules_do_not_disturb_the_ema_rules()
    test_evaluate_all_populates_both_setups()
    test_volume_high()
    test_volume_normal()
    test_volume_low()
    test_volume_unknown_without_sma()
    test_projection_makes_an_early_bar_tradable()
    test_completed_bar_is_not_projected()
    test_volume_floor_blocks_projection_from_a_tiny_numerator()
    test_thin_late_bar_reports_low_not_unknown()
    test_floor_still_blocks_a_thin_bar_reading_high()
    test_low_from_a_thin_bar_still_blocks_entry()
    test_min_elapsed_guard_blocks_a_large_multiplier()
    test_missing_elapsed_defaults_to_no_projection()
    test_elapsed_clamps_above_bar_seconds()
    test_projected_band_boundaries()
    test_oi_rising()
    test_oi_falling()
    test_oi_flat()
    test_oi_unknown_without_previous()
    test_volume_profile_states()
    test_volume_profile_unknown_without_levels()
    print("All tests passed.")
