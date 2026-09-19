"""
Tests for telegram_format.py.

Pure function, so none of this needs an engine, a token or a network. The tests
that matter are the ones pinning what the formatter must NOT do: it must not
recompute the volume ratio, and it must not derive VP position from a price
comparison. Both would put a second copy of a live rule in the view layer.
"""

from datetime import datetime

import pytest

from alert_engine import AlertRecord
from signal_engine import (LevelState, OpenInterestState, PullbackState,
                           RejectionState, SignalSnapshot, TrendState,
                           VolumeState)
from telegram_format import (MISSING, SETUP_LABELS, format_error_alert,
                             format_startup_alert,
                             format_trading_alert, volume_profile_status)


def snapshot(volume=VolumeState.HIGH, oi=OpenInterestState.RISING,
             vah=LevelState.ABOVE, val=LevelState.ABOVE):
    return SignalSnapshot(
        trend=TrendState.BULLISH, pullback=PullbackState.BULLISH,
        rejection=RejectionState.BULLISH, volume=volume, open_interest=oi,
        poc=LevelState.ABOVE, vah=vah, val=val,
        details={"trend": "Close 57799.00 above VWAP 57734.39, slope +0.73"},
    )


def record(**kw):
    base = dict(id="x", timestamp=datetime(2026, 8, 14, 9, 48, 3), type="Trading",
                direction="Long", grade="A", confidence=80, current_price=57799.0,
                setup="ema", vwap=57734.39, ema10=57778.66, ema_slope=0.73,
                signals=snapshot())
    base.update(kw)
    return AlertRecord(**base)


# ------------------------------------------------------------ the whole message

def test_the_message_matches_the_agreed_format():
    """Byte-for-byte against the sample in Telegram_Alert_Format_V2_Detailed-1.md
    §20, using that document's own example values. Arrows point the way PRICE sits
    relative to the level: 57799 is above both VWAP 57734 and EMA10 57779."""
    assert format_trading_alert(record()) == (
        "🟢 A LONG\n"
        "\n"
        "Price: 57799\n"
        "Setup: EMA Pullback + Rejection\n"
        "\n"
        "VWAP: 57734 ↑\n"
        "EMA10: 57779 ↑\n"
        "EMA10 slope: +0.73 /min\n"
        "Volume: High\n"
        "OI: Rising\n"
        "VP: Above VAH"
    )


def test_the_message_is_materially_shorter_than_the_old_one():
    """The old renderer joined all ten reason strings; V2's whole objective."""
    old = (f"A Long\nPrice: 57799.0\n" + "\n".join([
        "Trend: Close 57799.00 above VWAP 57734.39, slope +0.73 (holding, exits below -4.30)",
        "Pullback: Low 57744.20 <= EMA10 57778.66 < Close 57799.00",
        "Rejection: Lower wick + bullish candle + Close above EMA10",
        "Volume: Projected relative volume 1.31 >= 1.30 (3870 in 183s -> projected 6336)",
        "Open Interest: OI 2115510 -> 2115150 (-0.02%)",
        "Poc: Close 57799.00 above POC 57737.50",
        "Vah: Close 57799.00 above VAH 57780.00",
        "Val: Close 57799.00 above VAL 57690.00",
        "Vwap Pullback: Low 57744.20 within 10 of VWAP 57734.39 < Close 57799.00",
        "Vwap Rejection: Close 57799.00 held 98% of range above VWAP 57734.39",
    ]))
    assert len(format_trading_alert(record())) < len(old) / 3


def test_short_gets_the_red_icon():
    assert format_trading_alert(record(direction="Short")).startswith("🔴 A SHORT")


# ------------------------------------------------------------------- setup

@pytest.mark.parametrize("setup,expected", [
    ("ema", "EMA Pullback + Rejection"),
    ("vwap", "VWAP Pullback + Rejection"),
    ("ema+vwap", "EMA + VWAP (both)"),
])
def test_every_setup_value_has_a_label(setup, expected):
    """All three occur — in the 11-14 Aug live log, vwap 25 / ema+vwap 17 / ema 14.
    Leaving the combined case unmapped would have hit 30% of alerts."""
    assert f"Setup: {expected}" in format_trading_alert(record(setup=setup))


def test_setup_labels_cover_exactly_the_decision_values():
    from decision_engine import SETUP_EMA, SETUP_VWAP
    assert set(SETUP_LABELS) == {SETUP_EMA, SETUP_VWAP, f"{SETUP_EMA}+{SETUP_VWAP}"}


def test_an_unknown_setup_is_shown_verbatim_not_dropped():
    assert "Setup: something-new" in format_trading_alert(record(setup="something-new"))


# ---------------------------------------------------------- volume profile

@pytest.mark.parametrize("vah,val,expected", [
    (LevelState.ABOVE, LevelState.ABOVE, "Above VAH"),
    (LevelState.BELOW, LevelState.BELOW, "Below VAL"),
    (LevelState.BELOW, LevelState.ABOVE, "Inside Value"),
])
def test_vp_status_reads_the_level_state(vah, val, expected):
    assert volume_profile_status(vah, val) == expected


def test_rejected_is_not_treated_as_a_breakout():
    """LevelState includes REJECTED — price reached the level and was turned away.
    A `price > vah` comparison can never produce it, which is exactly why the
    formatter must not re-derive VP position from prices."""
    assert volume_profile_status(LevelState.REJECTED, LevelState.ABOVE) == "Inside Value"
    assert volume_profile_status(LevelState.BELOW, LevelState.REJECTED) == "Inside Value"


def test_unknown_levels_give_no_vp_claim():
    assert volume_profile_status(LevelState.UNKNOWN, LevelState.UNKNOWN) == MISSING


# ------------------------------------------------- volume and OI are STATES

def test_volume_is_the_engines_state_not_a_recomputed_ratio():
    """The projected ratio lives only inside _evaluate_volume, behind a min-elapsed
    guard, a clamp, an absolute floor and Schmitt banding. Recomputing it here
    would print a number on bars the engine itself called Unknown."""
    text = format_trading_alert(record(signals=snapshot(volume=VolumeState.NORMAL)))
    assert "Volume: Normal" in text
    assert "x" not in text.split("Volume:")[1].split("\n")[0]


def test_an_undetermined_volume_never_reads_as_a_real_state():
    text = format_trading_alert(record(signals=snapshot(volume=VolumeState.UNKNOWN)))
    assert f"Volume: {MISSING}" in text


@pytest.mark.parametrize("state,expected", [
    (OpenInterestState.RISING, "Rising"),
    (OpenInterestState.FALLING, "Falling"),
    (OpenInterestState.FLAT, "Flat"),
])
def test_oi_shows_direction_without_a_percentage(state, expected):
    """The percentage is not carried by measured_fields(); the direction is a
    direct read of what the engine concluded."""
    line = [l for l in format_trading_alert(
        record(signals=snapshot(oi=state))).split("\n") if l.startswith("OI:")][0]
    assert line == f"OI: {expected}"


# ----------------------------------------------------------- missing values

def test_the_slope_line_names_ema10():
    """It is EMA10's change over 60s (signal_engine._evaluate_trend), and VWAP has
    no slope anywhere in the engine. Printed under a VWAP line, a bare "Slope:"
    invites the misreading the format spec explicitly warned about."""
    line = [l for l in format_trading_alert(record()).splitlines()
            if "slope" in l.lower()][0]
    assert line.startswith("EMA10 slope:")
    assert "/min" in line, "a points figure is meaningless without its lookback"


def test_the_combined_setup_says_both():
    """A bare '+' reads as a list of alternatives; this value means both six-gate
    setups passed at once."""
    assert "Setup: EMA + VWAP (both)" in format_trading_alert(record(setup="ema+vwap"))


def test_missing_indicators_render_as_dashes_never_invented():
    text = format_trading_alert(record(vwap=None, ema10=None, ema_slope=None))
    assert f"VWAP: {MISSING}" in text
    assert f"EMA10: {MISSING}" in text
    assert f"EMA10 slope: {MISSING}" in text


def test_ema10_is_shown_even_on_a_vwap_setup():
    """V2 §19's acceptance criterion. The old message only ever exposed EMA10
    inside the verbose Pullback sentence, so a VWAP-only setup hid it."""
    text = format_trading_alert(
        record(setup="vwap", signals=snapshot(vah=LevelState.BELOW)))
    assert "EMA10: 57779" in text


def test_a_record_without_a_snapshot_still_renders():
    """AlertRecords built by hand in older tests carry no signals object."""
    text = format_trading_alert(record(signals=None))
    assert text.startswith("🟢 A LONG")
    assert "Volume:" not in text


# ------------------------------------------------------------------- errors

def test_error_alerts_stay_one_line():
    rec = AlertRecord(id="x", timestamp=datetime(2026, 8, 14, 9, 15), type="Error",
                      direction=None, grade=None, confidence=None,
                      current_price=None, reason_list=["Feed disconnected"])
    assert format_error_alert(rec) == "⚠️ Feed disconnected"


def test_error_alert_without_a_reason_does_not_crash():
    rec = AlertRecord(id="x", timestamp=datetime(2026, 8, 14, 9, 15), type="Error",
                      direction=None, grade=None, confidence=None, current_price=None)
    assert "Unknown error" in format_error_alert(rec)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


# ------------------------------------------------------------------ startup

def test_startup_sentence_is_one_line_with_the_seed_count():
    from telegram_format import startup_message
    assert startup_message("BANKNIFTY26AUGFUT", 36, 36) == (
        "🟢 BANKNIFTY26AUGFUT session started. Seed: 36 bars.")


def test_any_seed_shortfall_warns():
    """Not a separate threshold. config.SEED_BARS is what replay asserts equality
    against, so live and replay agree on 'properly seeded' with no second number
    to keep in sync. A '<15' rule would have blessed every value from 15 to 35,
    all of which are already degraded."""
    from telegram_format import startup_message
    for seed in (35, 20, 1, 0):
        assert "that's low" in startup_message("X", seed, 36)
    assert "that's low" not in startup_message("X", 36, 36)


def test_startup_singularises_one_bar():
    """The 11 Aug failure shape produced exactly this message."""
    from telegram_format import startup_message
    assert "Seed: 1 bar —" in startup_message("X", 1, 36)
    assert "Seed: 2 bars" in startup_message("X", 2, 36)


def test_startup_survives_a_missing_symbol():
    from telegram_format import startup_message
    assert startup_message(None, 36, 36).startswith("🟢 Session session started")


def test_startup_record_is_its_own_type_not_an_error():
    """A 'started fine' message tagged Error would pollute every later error
    query, and process_error's de-dupe keys on the message string."""
    from alert_engine import AlertEngine
    engine = AlertEngine(enable_telegram=False)
    rec = engine.process_startup("X", 36, 36, now=datetime(2026, 8, 14, 9, 15))
    assert rec.type == "Startup"
    assert rec.direction is None and rec.grade is None
    assert format_startup_alert(rec) == "🟢 X session started. Seed: 36 bars."


def test_startup_never_opens_a_flip_cooldown_window():
    """_last_alert must stay None, or the session's first real alert could be
    withheld for arriving too soon after a status message."""
    from alert_engine import AlertEngine
    engine = AlertEngine(enable_telegram=False)
    engine.process_startup("X", 36, 36, now=datetime(2026, 8, 14, 9, 15))
    assert engine._last_alert is None


def test_startup_rows_are_skipped_by_the_analysis_consumers():
    """alert_log.csv gains a Startup row per restart; every study filters on
    type == 'Trading', so counts stay comparable across the change."""
    import inspect
    from replay import compare, cooldown_study
    assert 'type != "Trading"' in inspect.getsource(compare.replay_alerts_as_rows) \
        or 'type"] != "Trading"' in inspect.getsource(compare.load_live_alerts)
    assert 'type"] != "Trading"' in inspect.getsource(cooldown_study.load)


def test_the_csv_stores_a_number_not_an_emoji_sentence():
    """The whole point of the structured design: `grep seed_bars= alert_log.csv`
    answers "was every session properly seeded" without parsing prose, and no
    emoji ever reaches a CSV."""
    from alert_engine import AlertEngine
    engine = AlertEngine(enable_telegram=False)
    rec = engine.process_startup("BANKNIFTY26AUGFUT", 36, 36,
                                 now=datetime(2026, 8, 14, 9, 15))
    stored = rec.reason_list[0]
    assert stored.isascii(), f"non-ASCII reached the CSV: {stored!r}"
    assert "seed_bars=36" in stored and "expected_bars=36" in stored
    assert rec.seed_count == 36 and rec.expected_seed == 36
    # ...while Telegram still gets the human version.
    assert format_startup_alert(rec).startswith("🟢")


def test_the_alert_log_schema_is_unchanged():
    """Adding a seed_count COLUMN would trip rotate_if_header_stale and rename the
    VM's 57-row history out from under every study that reads alert_log.csv."""
    from alert_engine import ALERT_LOG_FIELDS
    assert ALERT_LOG_FIELDS == ["id", "timestamp", "type", "direction", "grade",
                                "confidence", "setup", "current_price",
                                "reason_list", "engine_version"]


def test_a_low_seed_is_visible_in_both_the_csv_and_the_message():
    from alert_engine import AlertEngine
    engine = AlertEngine(enable_telegram=False)
    rec = engine.process_startup("X", 1, 36, now=datetime(2026, 8, 14, 9, 15))
    assert "seed_bars=1 expected_bars=36" in rec.reason_list[0]
    assert "that's low" in format_startup_alert(rec)
