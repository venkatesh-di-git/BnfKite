"""
Sanity tests for decision_engine.py, per
Signal_Decision_Engine_Specification_V1.md's Unit Tests list: Long
decision, Short decision, WAIT decision (+ grading and UNKNOWN safety,
since those directly gate ENTRY/WAIT).
"""

from decision_engine import DecisionEngine, Direction, Grade, Status
from signal_engine import (
    LevelState, OpenInterestState, PullbackState, RejectionState,
    SignalSnapshot, TrendState, VolumeState,
)

engine = DecisionEngine()


def snapshot(bullish=True, volume=VolumeState.HIGH, oi=OpenInterestState.RISING,
             vah=LevelState.BELOW, val=LevelState.ABOVE, poc=LevelState.AT):
    trend = TrendState.BULLISH if bullish else TrendState.BEARISH
    pullback = PullbackState.BULLISH if bullish else PullbackState.BEARISH
    rejection = RejectionState.BULLISH if bullish else RejectionState.BEARISH
    return SignalSnapshot(trend=trend, pullback=pullback, rejection=rejection,
                           volume=volume, open_interest=oi, poc=poc, vah=vah, val=val)


def vwap_snapshot(bullish=True, ema_setup=False, volume=VolumeState.HIGH,
                  oi=OpenInterestState.RISING):
    """A snapshot where the VWAP setup passes. `ema_setup` controls whether the
    EMA10 setup passes too, so the OR and the setup label can both be pinned."""
    trend = TrendState.BULLISH if bullish else TrendState.BEARISH
    want_pullback = PullbackState.BULLISH if bullish else PullbackState.BEARISH
    want_rejection = RejectionState.BULLISH if bullish else RejectionState.BEARISH
    return SignalSnapshot(
        trend=trend,
        pullback=want_pullback if ema_setup else PullbackState.NONE,
        rejection=want_rejection if ema_setup else RejectionState.NONE,
        volume=volume, open_interest=oi,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE,
        vwap_pullback=want_pullback, vwap_rejection=want_rejection,
    )


def test_vwap_setup_alone_produces_an_entry():
    """The point of the second setup: a retracement to VWAP that is not a
    retracement to EMA10 is now tradeable rather than invisible."""
    decision = engine.evaluate(vwap_snapshot(bullish=True))
    assert decision.direction == Direction.LONG
    assert decision.status == Status.ENTRY
    assert decision.setup == "vwap"
    assert decision.long_vwap_gates.passed and not decision.long_gates.passed
    print("PASS: the VWAP setup alone produces an ENTRY\n")


def test_ema_setup_alone_is_labelled_ema():
    decision = engine.evaluate(snapshot(bullish=True))
    assert decision.status == Status.ENTRY
    assert decision.setup == "ema"
    assert not decision.long_vwap_gates.passed
    print("PASS: the EMA10 setup alone is labelled 'ema'\n")


def test_both_setups_passing_is_labelled_ema_plus_vwap():
    """Recorded rather than collapsed — whether the two agree in practice is
    the question the labelling exists to answer."""
    decision = engine.evaluate(vwap_snapshot(bullish=True, ema_setup=True))
    assert decision.setup == "ema+vwap"
    assert decision.long_gates.passed and decision.long_vwap_gates.passed
    print("PASS: both setups passing is labelled 'ema+vwap'\n")


def test_vwap_setup_short_side():
    decision = engine.evaluate(vwap_snapshot(bullish=False))
    assert decision.direction == Direction.SHORT
    assert decision.status == Status.ENTRY
    assert decision.setup == "vwap"
    print("PASS: the VWAP setup works on the short side\n")


def test_grade_is_identical_whichever_setup_fires():
    """Grading reads volume and OI only, so the two setups must grade the same
    inputs the same way — otherwise the grade would encode the route, not the
    quality."""
    for volume, oi in [(VolumeState.HIGH, OpenInterestState.RISING),
                       (VolumeState.NORMAL, OpenInterestState.RISING),
                       (VolumeState.NORMAL, OpenInterestState.FLAT)]:
        ema = engine.evaluate(snapshot(volume=volume, oi=oi))
        vwap = engine.evaluate(vwap_snapshot(volume=volume, oi=oi))
        assert ema.grade == vwap.grade, f"{volume}/{oi}: {ema.grade} vs {vwap.grade}"
    print("PASS: grade is identical whichever setup fires\n")


def test_setup_is_none_on_wait():
    decision = engine.evaluate(vwap_snapshot(volume=VolumeState.LOW))
    assert decision.status == Status.WAIT
    assert decision.setup is None
    print("PASS: setup is None on WAIT\n")


def test_shared_gates_block_both_setups():
    """Volume, OI, trend and level are shared. A shared gate failing must take
    out both setups — otherwise the VWAP route would be a way around them."""
    decision = engine.evaluate(vwap_snapshot(ema_setup=True, volume=VolumeState.LOW))
    assert decision.status == Status.WAIT
    assert not decision.long_gates.passed and not decision.long_vwap_gates.passed
    assert decision.long_gates.failed == ("volume",)
    assert decision.long_vwap_gates.failed == ("volume",)
    print("PASS: a shared gate failing blocks both setups\n")


def test_long_decision_ideal_is_a_plus():
    decision = engine.evaluate(snapshot(bullish=True))
    assert decision.direction == Direction.LONG
    assert decision.status == Status.ENTRY
    assert decision.grade == Grade.A_PLUS
    print("PASS: long decision, ideal conditions -> A+\n")


def test_long_decision_one_weakness_is_a():
    decision = engine.evaluate(snapshot(bullish=True, volume=VolumeState.NORMAL))
    assert decision.direction == Direction.LONG
    assert decision.status == Status.ENTRY
    assert decision.grade == Grade.A
    print("PASS: long decision, one weak reading -> A\n")


def test_long_decision_two_weaknesses_is_b():
    decision = engine.evaluate(snapshot(bullish=True, volume=VolumeState.NORMAL, oi=OpenInterestState.FLAT))
    assert decision.direction == Direction.LONG
    assert decision.status == Status.ENTRY
    assert decision.grade == Grade.B
    print("PASS: long decision, both weak readings -> B\n")


def test_long_decision_vah_rejected_forces_wait():
    decision = engine.evaluate(snapshot(bullish=True, vah=LevelState.REJECTED))
    assert decision.status == Status.WAIT
    assert decision.direction == Direction.NEUTRAL
    assert decision.grade == Grade.IGNORE
    print("PASS: long decision, VAH rejected -> WAIT/Ignore\n")


def test_short_decision_ideal_is_a_plus():
    decision = engine.evaluate(snapshot(bullish=False))
    assert decision.direction == Direction.SHORT
    assert decision.status == Status.ENTRY
    assert decision.grade == Grade.A_PLUS
    print("PASS: short decision, ideal conditions -> A+\n")


def test_short_decision_val_rejected_forces_wait():
    decision = engine.evaluate(snapshot(bullish=False, val=LevelState.REJECTED))
    assert decision.status == Status.WAIT
    assert decision.direction == Direction.NEUTRAL
    print("PASS: short decision, VAL rejected -> WAIT\n")


def test_wait_decision_on_low_volume():
    decision = engine.evaluate(snapshot(bullish=True, volume=VolumeState.LOW))
    assert decision.status == Status.WAIT
    assert decision.direction == Direction.NEUTRAL
    assert decision.grade == Grade.IGNORE
    print("PASS: wait decision, volume too low -> WAIT/Ignore\n")


def test_wait_decision_on_falling_oi():
    decision = engine.evaluate(snapshot(bullish=True, oi=OpenInterestState.FALLING))
    assert decision.status == Status.WAIT
    print("PASS: wait decision, OI falling -> WAIT\n")


def test_wait_decision_on_mixed_signals():
    """Trend bullish but Pullback/Rejection bearish -> neither direction's
    mandatory chain fully passes."""
    s = SignalSnapshot(trend=TrendState.BULLISH, pullback=PullbackState.BEARISH,
                        rejection=RejectionState.BEARISH, volume=VolumeState.HIGH,
                        open_interest=OpenInterestState.RISING, poc=LevelState.AT,
                        vah=LevelState.BELOW, val=LevelState.ABOVE)
    decision = engine.evaluate(s)
    assert decision.status == Status.WAIT
    assert decision.direction == Direction.NEUTRAL
    print("PASS: wait decision, mixed/conflicting signals\n")


def test_wait_decision_on_unknown_signals():
    """UNKNOWN never equals a required state, so it can never satisfy a
    mandatory condition — this is what makes the engine 'never crash' on
    missing data instead of guessing a direction."""
    s = SignalSnapshot(trend=TrendState.UNKNOWN, pullback=PullbackState.UNKNOWN,
                        rejection=RejectionState.UNKNOWN, volume=VolumeState.UNKNOWN,
                        open_interest=OpenInterestState.UNKNOWN, poc=LevelState.UNKNOWN,
                        vah=LevelState.UNKNOWN, val=LevelState.UNKNOWN)
    decision = engine.evaluate(s)
    assert decision.status == Status.WAIT
    assert decision.direction == Direction.NEUTRAL
    assert decision.grade == Grade.IGNORE
    print("PASS: wait decision, all-UNKNOWN snapshot is handled safely\n")


if __name__ == "__main__":
    test_vwap_setup_alone_produces_an_entry()
    test_ema_setup_alone_is_labelled_ema()
    test_both_setups_passing_is_labelled_ema_plus_vwap()
    test_vwap_setup_short_side()
    test_grade_is_identical_whichever_setup_fires()
    test_setup_is_none_on_wait()
    test_shared_gates_block_both_setups()
    test_long_decision_ideal_is_a_plus()
    test_long_decision_one_weakness_is_a()
    test_long_decision_two_weaknesses_is_b()
    test_long_decision_vah_rejected_forces_wait()
    test_short_decision_ideal_is_a_plus()
    test_short_decision_val_rejected_forces_wait()
    test_wait_decision_on_low_volume()
    test_wait_decision_on_falling_oi()
    test_wait_decision_on_mixed_signals()
    test_wait_decision_on_unknown_signals()
    print("All tests passed.")
