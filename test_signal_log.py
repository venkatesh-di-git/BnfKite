"""
Tests for engine.SignalStateLog — the measurement instrument behind the
indicator work.

alert_log.csv only records transitions that cleared the Decision Engine's
mandatory gate, so it cannot show which signal flipped, or how often one
flipped without moving the grade. This log fills that gap, which is what
lets several indicator changes ship together and still be told apart.

Writes to a scratch path so runs never touch the real signal_log.csv.
"""

import csv
import inspect
import os
import tempfile
from datetime import datetime, timedelta

import config
import engine
from decision_engine import Decision, DecisionEngine, Direction, Grade, Status
from engine import SignalStateLog
from signal_engine import (
    IndicatorSnapshot, LevelState, OpenInterestState, PullbackState,
    RejectionState, SignalSnapshot, TrendState, VolumeState,
)
from volume_profile import Candle

LOG_PATH = os.path.join(tempfile.gettempdir(), "bnf_kite_test_signal_log.csv")
T0 = datetime(2026, 8, 3, 9, 20)


def fresh_log():
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    return SignalStateLog(path=LOG_PATH)


def make_snapshot(trend=TrendState.BULLISH, volume=VolumeState.HIGH,
                  open_interest=OpenInterestState.RISING):
    return SignalSnapshot(
        trend=trend, pullback=PullbackState.BULLISH, rejection=RejectionState.BULLISH,
        volume=volume, open_interest=open_interest,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE,
    )


def make_decision(snapshot, status=Status.ENTRY, direction=Direction.LONG, grade=Grade.A_PLUS):
    return Decision(direction=direction, status=status, grade=grade, signals=snapshot)


def read_rows():
    with open(LOG_PATH, newline="") as f:
        return list(csv.DictReader(f))


def test_first_evaluation_writes_a_row():
    log = fresh_log()
    snapshot = make_snapshot()

    assert log.record(snapshot, make_decision(snapshot), T0) is True

    rows = read_rows()
    assert len(rows) == 1
    assert rows[0]["trend"] == "Bullish" and rows[0]["grade"] == "A+"
    print("PASS: the first evaluation writes a row\n")


def test_unchanged_state_writes_nothing():
    """The whole point of change-triggering — at 1 evaluation/second an
    unchanged state would otherwise produce ~23,000 rows a session."""
    log = fresh_log()
    snapshot = make_snapshot()
    decision = make_decision(snapshot)

    log.record(snapshot, decision, T0)
    for i in range(1, 20):
        assert log.record(snapshot, decision, T0 + timedelta(seconds=i)) is False

    assert len(read_rows()) == 1
    print("PASS: repeated identical states write nothing\n")


def test_each_signal_change_writes_a_row():
    log = fresh_log()
    first = make_snapshot()
    log.record(first, make_decision(first), T0)

    changed_volume = make_snapshot(volume=VolumeState.NORMAL)
    assert log.record(changed_volume, make_decision(changed_volume), T0 + timedelta(seconds=1)) is True

    changed_trend = make_snapshot(volume=VolumeState.NORMAL, trend=TrendState.NEUTRAL)
    assert log.record(changed_trend, make_decision(changed_trend), T0 + timedelta(seconds=2)) is True

    rows = read_rows()
    assert [r["volume"] for r in rows] == ["High", "Normal", "Normal"]
    assert [r["trend"] for r in rows] == ["Bullish", "Bullish", "Neutral"]
    print("PASS: a change in any signal writes a row\n")


def test_decision_change_alone_writes_a_row():
    """Grade can move while every signal state holds — that transition is
    exactly what produces a duplicate alert, so it must be visible here."""
    log = fresh_log()
    snapshot = make_snapshot()
    log.record(snapshot, make_decision(snapshot, grade=Grade.A_PLUS), T0)

    assert log.record(snapshot, make_decision(snapshot, grade=Grade.A),
                      T0 + timedelta(seconds=1)) is True

    assert [r["grade"] for r in read_rows()] == ["A+", "A"]
    print("PASS: a decision-only change is recorded\n")


def test_volume_columns_do_not_trigger_rows():
    """bar_volume and sma20_volume are recorded for analysis but excluded
    from the change key — volume accumulates every tick, so including it
    would make every evaluation look like a change."""
    log = fresh_log()
    snapshot = make_snapshot()
    decision = make_decision(snapshot)

    log.record(snapshot, decision, T0, bar_volume=100, sma20_volume=2000)
    for i in range(1, 10):
        assert log.record(snapshot, decision, T0 + timedelta(seconds=i),
                          bar_volume=100 + i * 50, sma20_volume=2000) is False

    rows = read_rows()
    assert len(rows) == 1 and rows[0]["bar_volume"] == "100"
    print("PASS: changing volume alone does not trigger a row\n")


def test_recorded_context_columns_round_trip():
    log = fresh_log()
    snapshot = make_snapshot()

    log.record(snapshot, make_decision(snapshot), T0, current_price=57850.5,
               bar_volume=1234, sma20_volume=2000, elapsed_seconds=120.0)

    row = read_rows()[0]
    assert row["current_price"] == "57850.5"
    assert row["sma20_volume"] == "2000"
    assert row["elapsed_seconds"] == "120.0"
    assert row["timestamp"].startswith("2026-08-03T09:20")
    print("PASS: price/volume/elapsed context round-trips through the CSV\n")


def test_measured_columns_do_not_trigger_rows():
    """The core regression for the new columns. open/high/low/ema_10/vwap/
    ema_slope all move on every tick; if any leaked into STATE_FIELDS the log
    would write a row per evaluation and stop being a transition log at all.
    Fails if the new columns are added to the change key."""
    log = fresh_log()
    snapshot = make_snapshot()
    decision = make_decision(snapshot)

    log.record(snapshot, decision, T0, **engine.measured_fields(
        Candle(open=100.0, high=101.0, low=99.0, close=100.5, volume=10),
        IndicatorSnapshot(vwap=100.2, ema10=100.1, ema10_prev=100.0,
                          sma20_volume=2000, elapsed_seconds=60.0)))

    for i in range(1, 10):
        # Every measured value moves; not one discrete state does.
        wrote = log.record(snapshot, decision, T0 + timedelta(seconds=i),
                           **engine.measured_fields(
                               Candle(open=100.0, high=101.0 + i, low=99.0 - i,
                                      close=100.5 + i, volume=10 + i * 7),
                               IndicatorSnapshot(vwap=100.2 + i * 0.3, ema10=100.1 + i * 0.4,
                                                 ema10_prev=100.0, sma20_volume=2000,
                                                 elapsed_seconds=60.0 + i)))
        assert wrote is False, f"tick {i} wrote a row — a measured column is in the change key"

    assert len(read_rows()) == 1
    print("PASS: moving every measured column alone does not trigger a row\n")


def test_measured_columns_round_trip_from_candle_and_indicators():
    log = fresh_log()
    snapshot = make_snapshot()

    log.record(snapshot, make_decision(snapshot), T0, current_price=57850.5,
               **engine.measured_fields(
                   Candle(open=57800.0, high=57880.0, low=57790.0, close=57860.0, volume=1234),
                   IndicatorSnapshot(vwap=57905.25, ema10=57840.5, ema10_prev=57836.2,
                                     sma20_volume=2000, elapsed_seconds=120.0)))

    row = read_rows()[0]
    assert row["open"] == "57800.0" and row["high"] == "57880.0" and row["low"] == "57790.0"
    assert row["ema_10"] == "57840.5" and row["vwap"] == "57905.25"
    assert row["bar_volume"] == "1234" and row["elapsed_seconds"] == "120.0"
    # current_price is the live tick, deliberately NOT candle.close (57860.0).
    assert row["current_price"] == "57850.5"
    assert abs(float(row["ema_slope"]) - (57840.5 - 57836.2)) < 1e-9
    print("PASS: measured columns round-trip from candle + indicators\n")


def test_ema_slope_is_blank_before_the_lookback_fills():
    """Cold start: ema10_prev is None until EMA_SLOPE_LOOKBACK_SECONDS of
    samples exist, and the log must not invent a slope of 0.0 there."""
    log = fresh_log()
    snapshot = make_snapshot()

    log.record(snapshot, make_decision(snapshot), T0, **engine.measured_fields(
        Candle(open=1.0, high=2.0, low=0.5, close=1.5, volume=10),
        IndicatorSnapshot(ema10=57840.5, ema10_prev=None)))

    assert read_rows()[0]["ema_slope"] == ""
    print("PASS: ema_slope is blank, not zero, before the lookback fills\n")


# Measured columns the callers pass separately, because neither is derivable
# from (candle, indicators): current_price is the live tick rather than
# candle.close, and bar_start comes off snapshot() — deriving it from the row
# timestamp would misfile every row written just after a bar closed.
CALLER_SUPPLIED_MEASURED = {"current_price", "bar_start"}


def test_measured_fields_tolerates_no_candle():
    """evaluate_signals() runs before the first bar forms."""
    fields = engine.measured_fields(None, IndicatorSnapshot())
    assert set(fields) == set(f + "_" if f == "open" else f
                              for f in engine.SignalStateLog.MEASURED_FIELDS
                              if f not in CALLER_SUPPLIED_MEASURED)
    assert all(v is None for v in fields.values())
    print("PASS: measured_fields handles a missing candle\n")


def test_record_accepts_every_measured_column():
    """Drift guard for the exception list above: any MEASURED_FIELD that
    measured_fields() does not supply must be an accepted record() keyword,
    or it silently logs blank forever."""
    accepted = inspect.signature(SignalStateLog.record).parameters
    supplied = set(engine.measured_fields(None, IndicatorSnapshot()))
    for field in engine.SignalStateLog.MEASURED_FIELDS:
        name = field + "_" if field == "open" else field
        assert name in supplied or name in accepted, f"{field} can never be populated"
    print("PASS: every measured column is either derived or an accepted keyword\n")


# --- near-miss columns -------------------------------------------------------
#
# Only fired alerts have ever been visible. These columns record the setups that
# nearly fired and which single gate refused, so a threshold can be retuned on
# evidence instead of intuition.

_decision_engine = DecisionEngine()


def near_miss_snapshot(**overrides):
    """A snapshot that passes every Long gate, then breaks exactly one."""
    fields = dict(trend=TrendState.BULLISH, pullback=PullbackState.BULLISH,
                  rejection=RejectionState.BULLISH, volume=VolumeState.HIGH,
                  open_interest=OpenInterestState.RISING,
                  poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE)
    fields.update(overrides)
    return SignalSnapshot(**fields)


def test_single_blocking_gate_is_named():
    """Volume LOW is the only Long gate failing — the case worth measuring."""
    log = fresh_log()
    snapshot = near_miss_snapshot(volume=VolumeState.LOW)
    log.record(snapshot, _decision_engine.evaluate(snapshot), T0)
    assert read_rows()[0]["long_blocked_ema"] == "volume"
    print("PASS: a single blocking gate is named in the log\n")


def test_no_blocking_gate_when_the_direction_passes():
    log = fresh_log()
    snapshot = near_miss_snapshot()
    decision = _decision_engine.evaluate(snapshot)
    assert decision.status is Status.ENTRY, "precondition: this is a clean Long"
    log.record(snapshot, decision, T0)
    assert read_rows()[0]["long_blocked_ema"] == ""
    print("PASS: a passing direction records no blocking gate\n")


def test_two_blocking_gates_record_nothing():
    """Two blockers means nothing aligned — that is not a near miss, and
    recording it would drown the signal in noise."""
    log = fresh_log()
    snapshot = near_miss_snapshot(volume=VolumeState.LOW,
                                  open_interest=OpenInterestState.FALLING)
    log.record(snapshot, _decision_engine.evaluate(snapshot), T0)
    assert read_rows()[0]["long_blocked_ema"] == ""
    print("PASS: two or more blocking gates record nothing\n")


def test_both_directions_can_be_near_misses_at_once():
    """Why these are two columns rather than one combined field."""
    log = fresh_log()
    # Trend Bearish: Long is blocked on trend alone. Flipping pullback and
    # rejection bearish too leaves Short blocked on nothing... so break Short's
    # volume instead, keeping each direction at exactly one blocker.
    snapshot = SignalSnapshot(
        trend=TrendState.BULLISH, pullback=PullbackState.BULLISH,
        rejection=RejectionState.BULLISH, volume=VolumeState.LOW,
        open_interest=OpenInterestState.RISING,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE)
    decision = _decision_engine.evaluate(snapshot)
    assert len(decision.long_gates.failed) == 1
    assert len(decision.short_gates.failed) > 1  # trend/pullback/rejection all wrong
    log.record(snapshot, decision, T0)
    row = read_rows()[0]
    assert row["long_blocked_ema"] == "volume" and row["short_blocked_ema"] == ""
    print("PASS: the two directions are recorded independently\n")


def test_decision_without_gates_records_blank_and_does_not_crash():
    """Decisions built by hand (tests, older callers) carry no GateResults."""
    log = fresh_log()
    snapshot = make_snapshot()
    log.record(snapshot, make_decision(snapshot), T0)
    row = read_rows()[0]
    assert row["long_blocked_ema"] == "" and row["short_blocked_ema"] == ""
    print("PASS: a Decision without gates logs blank rather than crashing\n")


def test_near_miss_columns_add_no_rows():
    """They are in the change key, but each is a pure function of fields
    already there — so they cannot change unless the key would have changed
    anyway. This is what keeps the log's compression intact."""
    log = fresh_log()
    snapshot = near_miss_snapshot(volume=VolumeState.LOW)
    decision = _decision_engine.evaluate(snapshot)

    written = [log.record(snapshot, decision, T0 + timedelta(seconds=i)) for i in range(5)]
    assert written == [True, False, False, False, False]
    assert len(read_rows()) == 1
    print("PASS: the near-miss columns add no extra rows\n")


def test_bar_start_is_recorded_and_not_derived_from_the_timestamp():
    """A row written just after a bar closes carries the COMPLETED bar, while
    the wall clock has already rolled — so five_minute_start(timestamp) would
    file it under the wrong bar and corrupt the per-bar counts."""
    log = fresh_log()
    snapshot = near_miss_snapshot(volume=VolumeState.LOW)
    written_at = datetime(2026, 8, 3, 15, 10, 1)   # clock is in the 15:10 window
    bar = datetime(2026, 8, 3, 15, 5)              # ...but the bar handed out is 15:05

    log.record(snapshot, _decision_engine.evaluate(snapshot), written_at, bar_start=bar)
    row = read_rows()[0]
    assert row["bar_start"] == bar.isoformat()
    assert row["bar_start"] != engine.five_minute_start(written_at).isoformat()
    print("PASS: bar_start is the recorded bar, not derived from the timestamp\n")


def test_near_miss_spanning_a_bar_boundary_writes_one_row_per_bar():
    """The proof behind counting distinct (bar_start, gate) pairs: a near-miss
    cannot cross a boundary unnoticed, because it requires the volume gate to
    PASS, and every boundary resets elapsed and forces Volume to UNKNOWN — a
    state change, hence a row carrying the new bar_start."""
    log = fresh_log()
    blocked = near_miss_snapshot(open_interest=OpenInterestState.FALLING)
    boundary = near_miss_snapshot(open_interest=OpenInterestState.FALLING,
                                  volume=VolumeState.UNKNOWN)
    bar_a = datetime(2026, 8, 3, 15, 5)
    bar_b = datetime(2026, 8, 3, 15, 10)

    log.record(blocked, _decision_engine.evaluate(blocked), T0, bar_start=bar_a)
    log.record(boundary, _decision_engine.evaluate(boundary), T0, bar_start=bar_b)
    log.record(blocked, _decision_engine.evaluate(blocked), T0, bar_start=bar_b)

    pairs = {(r["bar_start"], r["long_blocked_ema"]) for r in read_rows() if r["long_blocked_ema"]}
    assert (bar_a.isoformat(), "open_interest") in pairs
    assert (bar_b.isoformat(), "open_interest") in pairs
    print("PASS: a near-miss across a boundary is counted in both bars\n")


def test_the_two_setups_are_blocked_independently():
    """The four near-miss columns exist so the setups can be compared. A
    snapshot where EMA10 is blocked on volume alone and VWAP on pullback alone
    must record both, not one merged verdict."""
    log = fresh_log()
    snapshot = SignalSnapshot(
        trend=TrendState.BULLISH, pullback=PullbackState.BULLISH,
        rejection=RejectionState.BULLISH, volume=VolumeState.LOW,
        open_interest=OpenInterestState.RISING,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE,
        vwap_pullback=PullbackState.NONE, vwap_rejection=RejectionState.BULLISH)
    log.record(snapshot, _decision_engine.evaluate(snapshot), T0)

    row = read_rows()[0]
    assert row["long_blocked_ema"] == "volume"
    assert row["long_blocked_vwap"] == ""   # two blockers: pullback AND volume
    assert row["vwap_pullback"] == "None" and row["vwap_rejection"] == "Bullish"
    print("PASS: the two setups are blocked and recorded independently\n")


def test_setup_column_records_which_setup_fired():
    log = fresh_log()
    snapshot = SignalSnapshot(
        trend=TrendState.BULLISH, pullback=PullbackState.NONE,
        rejection=RejectionState.NONE, volume=VolumeState.HIGH,
        open_interest=OpenInterestState.RISING,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE,
        vwap_pullback=PullbackState.BULLISH, vwap_rejection=RejectionState.BULLISH)
    log.record(snapshot, _decision_engine.evaluate(snapshot), T0)
    assert read_rows()[0]["setup"] == "vwap"
    print("PASS: the setup column records which setup fired\n")


def test_close_is_recorded_and_distinct_from_current_price():
    """current_price is the live tick; close belongs to the candle. They differ
    whenever no bar is forming, and the VWAP Rejection rule is measured off the
    candle — so auditing it from current_price would be wrong on those rows."""
    log = fresh_log()
    snapshot = make_snapshot()
    log.record(snapshot, make_decision(snapshot), T0, current_price=58050.5,
               **engine.measured_fields(
                   Candle(open=58000.0, high=58060.0, low=57990.0, close=58020.0, volume=10),
                   IndicatorSnapshot()))
    row = read_rows()[0]
    assert row["close"] == "58020.0"
    assert row["current_price"] == "58050.5"
    print("PASS: close is recorded and distinct from current_price\n")


def _manifest_path():
    return os.path.join(os.path.dirname(LOG_PATH), "run_manifest.csv")


def _clear_manifest():
    directory = os.path.dirname(LOG_PATH)
    for name in os.listdir(directory):
        if name.startswith("run_manifest"):
            os.remove(os.path.join(directory, name))


def test_manifest_rows_all_match_the_header_width():
    """The assertion that would have caught the real bug.

    _write_manifest emits a header only when the file is absent, and every row
    is len(_VERSIONED_CONSTANTS) + 2 wide. Add a constant and, without rotation,
    the next process appends a wider row under the old header — a ragged CSV,
    no error, and the provenance the file exists to provide is corrupted.

    Rotation alone is not enough to test: it can pass while ragged rows still
    accumulate. Column width is the property that actually matters.
    """
    _clear_manifest()
    stale = ["timestamp", "engine_version"] + list(engine._VERSIONED_CONSTANTS)[:-1]
    with open(_manifest_path(), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(stale)
        w.writerow(["2026-08-03T09:00:00", "deadbeef"] + [0] * (len(stale) - 2))

    log = SignalStateLog(path=LOG_PATH)
    snapshot = make_snapshot()
    log.record(snapshot, make_decision(snapshot), T0)

    with open(_manifest_path(), newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["timestamp", "engine_version"] + list(engine._VERSIONED_CONSTANTS)
    for i, row in enumerate(rows):
        assert len(row) == len(rows[0]), f"row {i} has {len(row)} cols, header has {len(rows[0])}"
    print("PASS: every manifest row matches the header width\n")


def test_stale_manifest_header_is_rotated_not_appended_to():
    """The old manifest is provenance for past runs — moved aside, never lost."""
    _clear_manifest()
    stale = ["timestamp", "engine_version"]
    with open(_manifest_path(), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(stale)
        w.writerow(["2026-08-03T09:00:00", "deadbeef"])

    log = SignalStateLog(path=LOG_PATH)
    snapshot = make_snapshot()
    log.record(snapshot, make_decision(snapshot), T0)

    rotated = [p for p in os.listdir(os.path.dirname(LOG_PATH))
               if p.startswith("run_manifest.") and p != "run_manifest.csv"]
    assert rotated, "the old manifest was not preserved"
    with open(os.path.join(os.path.dirname(LOG_PATH), rotated[0]), newline="") as f:
        assert list(csv.reader(f))[1][1] == "deadbeef", "old provenance was lost"
    print("PASS: a stale manifest header is rotated, not appended to\n")


def test_stale_header_is_rotated_not_appended_to():
    """record() only writes a header when the file is absent, so appending
    wider rows to a pre-existing narrower file would silently produce a ragged
    CSV. The old file must be moved aside intact."""
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    old_header = ["timestamp", "status", "direction"]
    with open(LOG_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(old_header)
        w.writerow(["2026-08-03T09:00:00", "WAIT", "Neutral"])

    log = SignalStateLog(path=LOG_PATH)
    snapshot = make_snapshot()
    assert log.record(snapshot, make_decision(snapshot), T0) is True

    rows = read_rows()
    assert len(rows) == 1, "new file must contain only the new row"
    with open(LOG_PATH, newline="") as f:
        assert next(csv.reader(f)) == SignalStateLog.FIELDS

    rotated = [p for p in os.listdir(os.path.dirname(LOG_PATH))
               if p.startswith("bnf_kite_test_signal_log.") and p != os.path.basename(LOG_PATH)]
    assert rotated, "old log was not preserved"
    for p in rotated:
        os.remove(os.path.join(os.path.dirname(LOG_PATH), p))
    print("PASS: a stale header is rotated aside, not appended to\n")


def test_engine_version_tracks_tuning_constants():
    """The 05 Aug log mixed pre- and post-fix rows with nothing to tell them
    apart. Changing a threshold must change the recorded version."""
    before = engine.engine_version()
    original = config.EMA_SLOPE_HYSTERESIS_POINTS
    config.EMA_SLOPE_HYSTERESIS_POINTS = original + 1.0
    try:
        assert engine.engine_version() != before, "version ignored a tuning change"
    finally:
        config.EMA_SLOPE_HYSTERESIS_POINTS = original
    assert engine.engine_version() == before, "version not stable for equal config"
    print("PASS: engine_version tracks tuning constants\n")


def test_engine_version_is_recorded_on_every_row():
    log = fresh_log()
    snapshot = make_snapshot()
    log.record(snapshot, make_decision(snapshot), T0)
    assert read_rows()[0]["engine_version"] == engine.engine_version()
    print("PASS: engine_version is stamped on the row\n")


def test_missing_snapshot_or_decision_is_a_noop():
    """evaluate_signals() returns early before a session exists; the log must
    tolerate being called with nothing to record rather than creating a file."""
    log = fresh_log()

    assert log.record(None, None, T0) is False
    assert log.record(make_snapshot(), None, T0) is False

    assert not os.path.exists(LOG_PATH)
    print("PASS: nothing to record creates no file\n")


if __name__ == "__main__":
    test_first_evaluation_writes_a_row()
    test_unchanged_state_writes_nothing()
    test_each_signal_change_writes_a_row()
    test_decision_change_alone_writes_a_row()
    test_volume_columns_do_not_trigger_rows()
    test_recorded_context_columns_round_trip()
    test_measured_columns_do_not_trigger_rows()
    test_measured_columns_round_trip_from_candle_and_indicators()
    test_ema_slope_is_blank_before_the_lookback_fills()
    test_measured_fields_tolerates_no_candle()
    test_record_accepts_every_measured_column()
    test_single_blocking_gate_is_named()
    test_no_blocking_gate_when_the_direction_passes()
    test_two_blocking_gates_record_nothing()
    test_both_directions_can_be_near_misses_at_once()
    test_decision_without_gates_records_blank_and_does_not_crash()
    test_near_miss_columns_add_no_rows()
    test_bar_start_is_recorded_and_not_derived_from_the_timestamp()
    test_near_miss_spanning_a_bar_boundary_writes_one_row_per_bar()
    test_the_two_setups_are_blocked_independently()
    test_setup_column_records_which_setup_fired()
    test_close_is_recorded_and_distinct_from_current_price()
    test_manifest_rows_all_match_the_header_width()
    test_stale_manifest_header_is_rotated_not_appended_to()
    test_stale_header_is_rotated_not_appended_to()
    test_engine_version_tracks_tuning_constants()
    test_engine_version_is_recorded_on_every_row()
    test_missing_snapshot_or_decision_is_a_noop()
    print("All tests passed.")
