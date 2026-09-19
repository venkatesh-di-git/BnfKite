"""
test_decision_engine_equivalence.py

Proves the gate-exposure refactor is behaviourally inert.

_mandatory_conditions_pass used to collapse six booleans into a single `and`
and throw the individuals away. It is now _evaluate_gates, returning a
GateResults whose .passed is the same `and`. The gate expressions were not
meant to change — this test is the evidence, not the claim.

The decision engine consumes nothing but a SignalSnapshot of eight enums, so
its input space is finite and small enough to enumerate completely:

    4 trend x 4 pullback x 4 rejection x 4 volume x 4 open_interest  = 1,024
    5 poc x 5 vah x 5 val                                           =   125
                                                                    -------
                                                                    128,000

Every one is checked against a frozen copy of the prior implementation, for
both candidate directions. Not a sample — the entire discrete state space.

The frozen copy below is a verbatim paste of decision_engine.py as it stood
before the change. It must NEVER be updated to track the new code: the moment
it is, the test compares the implementation against itself and proves nothing.
If a gate rule legitimately changes, this file gets deleted, not edited.
"""

import itertools

from decision_engine import (GATE_NAMES, DecisionEngine, Direction, GateResults,
                             Status)
from signal_engine import (
    LevelState, OpenInterestState, PullbackState, RejectionState,
    SignalSnapshot, TrendState, VolumeState,
)


# --- frozen: decision_engine._mandatory_conditions_pass, pre-refactor ---------
def _mandatory_conditions_pass_frozen(s: SignalSnapshot, bullish: bool) -> bool:
    trend_want = TrendState.BULLISH if bullish else TrendState.BEARISH
    pullback_want = PullbackState.BULLISH if bullish else PullbackState.BEARISH
    rejection_want = RejectionState.BULLISH if bullish else RejectionState.BEARISH

    trend_ok = s.trend == trend_want
    pullback_ok = s.pullback == pullback_want
    rejection_ok = s.rejection == rejection_want
    volume_ok = s.volume in (VolumeState.HIGH, VolumeState.NORMAL)
    oi_ok = s.open_interest in (OpenInterestState.RISING, OpenInterestState.FLAT)
    # Long: price must not be rejected at VAH. Short: not rejected at VAL.
    level_ok = s.vah != LevelState.REJECTED if bullish else s.val != LevelState.REJECTED

    return trend_ok and pullback_ok and rejection_ok and volume_ok and oi_ok and level_ok
# --- end frozen ---------------------------------------------------------------


engine = DecisionEngine()


def all_snapshots():
    """Every reachable SignalSnapshot for the EMA10 setup. details={} throughout:
    it is a display string bag the gates never read.

    vwap_pullback/vwap_rejection are left at their UNKNOWN defaults ON PURPOSE.
    That holds the EMA10 setup's input space exactly as it was before the VWAP
    setup existed, so this sweep still measures what it was written to measure.
    UNKNOWN never equals a required state, so the VWAP gates cannot pass here and
    cannot perturb evaluate()'s output.

    Do NOT widen this to enumerate all ten enums — that is 2,048,000 combinations
    for no extra assurance about the EMA path. The VWAP rules are tested directly
    in test_signal_engine.py instead.
    """
    for trend, pullback, rejection, volume, oi, poc, vah, val in itertools.product(
        TrendState, PullbackState, RejectionState, VolumeState,
        OpenInterestState, LevelState, LevelState, LevelState,
    ):
        yield SignalSnapshot(
            trend=trend, pullback=pullback, rejection=rejection, volume=volume,
            open_interest=oi, poc=poc, vah=vah, val=val, details={},
        )


def test_state_space_is_the_expected_size():
    """Guards the claim above. If an enum gains a member this fails loudly
    rather than quietly shrinking the coverage the other tests rely on."""
    assert len(TrendState) == 4
    assert len(PullbackState) == 4
    assert len(RejectionState) == 4
    assert len(VolumeState) == 4
    assert len(OpenInterestState) == 4
    assert len(LevelState) == 5
    assert sum(1 for _ in all_snapshots()) == 128_000


def test_gates_match_frozen_implementation_across_full_state_space():
    """The whole point of the commit: 128,000 combinations, zero mismatches."""
    mismatches = []
    for snapshot in all_snapshots():
        for bullish in (True, False):
            expected = _mandatory_conditions_pass_frozen(snapshot, bullish)
            actual = engine._evaluate_gates(snapshot, bullish=bullish).passed
            if actual != expected:
                mismatches.append((snapshot, bullish, expected, actual))
    assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[0]}"


def test_evaluate_directions_match_frozen_implementation():
    """The refactor also rewired evaluate() to read .passed off the two
    GateResults. Checks the wiring, not just the helper — a swapped
    long/short would sail past the test above.

    Also pins that adding the VWAP setup left the EMA10 path alone: with the
    VWAP states UNKNOWN, evaluate()'s direction and status must still be exactly
    what the frozen single-setup implementation produced."""
    for snapshot in all_snapshots():
        decision = engine.evaluate(snapshot)
        long_ok = _mandatory_conditions_pass_frozen(snapshot, bullish=True)
        short_ok = _mandatory_conditions_pass_frozen(snapshot, bullish=False)
        assert decision.long_gates.passed == long_ok
        assert decision.short_gates.passed == short_ok
        # The second setup must be inert here, not merely unused.
        assert not decision.long_vwap_gates.passed
        assert not decision.short_vwap_gates.passed
        if long_ok and not short_ok:
            assert (decision.direction, decision.status) == (Direction.LONG, Status.ENTRY)
            assert decision.setup == "ema"
        elif short_ok and not long_ok:
            assert (decision.direction, decision.status) == (Direction.SHORT, Status.ENTRY)
            assert decision.setup == "ema"
        else:
            assert (decision.direction, decision.status) == (Direction.NEUTRAL, Status.WAIT)
            assert decision.setup is None


def test_failed_is_empty_exactly_when_passed():
    """The near-miss log keys off len(failed); this pins the two properties
    against each other so `passed` can never disagree with `failed == ()`."""
    for combo in itertools.product((True, False), repeat=len(GATE_NAMES)):
        gates = GateResults(*combo)
        assert gates.passed == (gates.failed == ())
        assert len(gates.failed) == sum(1 for flag in combo if not flag)


def test_failed_reports_gate_names_in_declared_order():
    gates = GateResults(trend=False, pullback=True, rejection=False,
                        volume=True, open_interest=False, level=True)
    assert gates.failed == ("trend", "rejection", "open_interest")


def test_failed_names_are_all_real_attributes():
    """GATE_NAMES is read via getattr, so a typo there would silently
    AttributeError only on the paths that happen to hit it."""
    gates = GateResults(*([True] * len(GATE_NAMES)))
    for name in GATE_NAMES:
        assert isinstance(getattr(gates, name), bool)


if __name__ == "__main__":
    test_state_space_is_the_expected_size()
    print("PASS: state space is 128,000 combinations")
    test_gates_match_frozen_implementation_across_full_state_space()
    print("PASS: gates match frozen implementation across full state space")
    test_evaluate_directions_match_frozen_implementation()
    print("PASS: evaluate() wires long/short gates correctly")
    test_failed_is_empty_exactly_when_passed()
    print("PASS: failed is empty exactly when passed")
    test_failed_reports_gate_names_in_declared_order()
    print("PASS: failed reports gate names in declared order")
    test_failed_names_are_all_real_attributes()
    print("PASS: GATE_NAMES all resolve to real attributes")
