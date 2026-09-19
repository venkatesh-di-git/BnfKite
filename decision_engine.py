"""
decision_engine.py

Implements DecisionEngine per Signal_Decision_Engine_Specification_V1.md:
consumes a SignalSnapshot only (no indicators, no Kite, no alerting) and
produces a Decision: Direction (Long/Short/Neutral), Status (ENTRY/WAIT),
Grade (A+/A/B/Ignore).

Decision flow (spec): Trend, Pullback, Rejection, Volume, Open Interest,
Volume Profile are the six mandatory checks. All must pass for the
candidate direction -> ENTRY; any failure (including UNKNOWN, which
never equals a required state) -> WAIT. The spec's Suggested Logic only
names VAH (Long) / VAL (Short) "not rejected" as the Volume Profile
gate — POC's state is informational only in SignalSnapshot, not itself
a pass/fail condition here.

Grading (spec gives prose, not numbers): the two "or" clauses in the
Suggested Logic — Volume in {High, Normal}, OI in {Rising, Flat} — are
the only place the spec allows a weaker-but-still-passing reading, so
grade is built from how many of those two land on the stronger side:
  A+  : Volume=High and OI=Rising (both ideal)
  A   : exactly one of {Volume=Normal, OI=Flat} (one minor weakness)
  B   : Volume=Normal and OI=Flat (both at the weaker reading)
  Ignore : Status=WAIT
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from signal_engine import (
    OpenInterestState, LevelState, PullbackState, RejectionState,
    SignalSnapshot, TrendState, VolumeState,
)

logger = logging.getLogger(__name__)


class Direction(str, Enum):
    LONG = "Long"
    SHORT = "Short"
    NEUTRAL = "Neutral"


class Status(str, Enum):
    ENTRY = "ENTRY"
    WAIT = "WAIT"


class Grade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    IGNORE = "Ignore"


GATE_NAMES = ("trend", "pullback", "rejection", "volume", "open_interest", "level")

# Two setups, evaluated independently and OR'd. They differ ONLY in which level
# the pullback and rejection gates measure against: EMA10 or VWAP. Trend,
# volume, open interest and level are shared, and so is grading.
#
# The VWAP setup exists because the two levels are routinely tens of points
# apart, so a retracement to one is simply not a retracement to the other — on
# 06 Aug price pulled back to VWAP and reversed 100 points while sitting 31
# points BELOW EMA10, which the EMA10 setup reads as a breakdown, not a pullback.
SETUP_EMA = "ema"
SETUP_VWAP = "vwap"
SETUPS = (SETUP_EMA, SETUP_VWAP)


@dataclass(frozen=True)
class GateResults:
    """Per-gate outcome for one candidate direction.

    The six booleans used to be collapsed into a single `and` and discarded.
    WHICH gate refused is the near-miss signal: a setup where direction is right
    and exactly one gate blocks says a threshold may be mistuned, while one where
    nothing aligns says nothing at all. Exposing it here rather than recomputing
    the checks downstream is what stops a copy of the gate rules drifting out of
    sync with these — same reasoning as engine.measured_fields().
    """

    trend: bool
    pullback: bool
    rejection: bool
    volume: bool
    open_interest: bool
    level: bool

    @property
    def passed(self) -> bool:
        return all(getattr(self, name) for name in GATE_NAMES)

    @property
    def failed(self) -> tuple:
        """Names of blocking gates, in GATE_NAMES order. Empty when passed."""
        return tuple(name for name in GATE_NAMES if not getattr(self, name))


@dataclass
class Decision:
    direction: Direction
    status: Status
    grade: Grade
    signals: SignalSnapshot
    # Defaulted and trailing, so the existing keyword constructions in the tests
    # are unaffected. Optional because a Decision built by hand may omit them.
    #
    # long_gates/short_gates stay the EMA10 setup's results — renaming them would
    # churn every reader for no gain, and the EMA10 setup is still the primary one.
    long_gates: Optional[GateResults] = None
    short_gates: Optional[GateResults] = None
    long_vwap_gates: Optional[GateResults] = None
    short_vwap_gates: Optional[GateResults] = None
    # Which setup produced an ENTRY: "ema", "vwap", "ema+vwap", or None on WAIT.
    setup: Optional[str] = None


class DecisionEngine:
    def evaluate(self, snapshot: SignalSnapshot) -> Decision:
        long_gates = self._evaluate_gates(snapshot, bullish=True, setup=SETUP_EMA)
        short_gates = self._evaluate_gates(snapshot, bullish=False, setup=SETUP_EMA)
        long_vwap_gates = self._evaluate_gates(snapshot, bullish=True, setup=SETUP_VWAP)
        short_vwap_gates = self._evaluate_gates(snapshot, bullish=False, setup=SETUP_VWAP)

        # Either setup is sufficient — they are alternative routes to the same
        # trade, not conditions on one another.
        long_ok = long_gates.passed or long_vwap_gates.passed
        short_ok = short_gates.passed or short_vwap_gates.passed

        if long_ok and not short_ok:
            direction, status = Direction.LONG, Status.ENTRY
            setup = self._setup_name(long_gates, long_vwap_gates)
        elif short_ok and not long_ok:
            direction, status = Direction.SHORT, Status.ENTRY
            setup = self._setup_name(short_gates, short_vwap_gates)
        else:
            direction, status = Direction.NEUTRAL, Status.WAIT
            setup = None

        grade = self._grade(status, snapshot)
        decision = Decision(direction=direction, status=status, grade=grade, signals=snapshot,
                            long_gates=long_gates, short_gates=short_gates,
                            long_vwap_gates=long_vwap_gates, short_vwap_gates=short_vwap_gates,
                            setup=setup)
        logger.debug("evaluate snapshot=%s -> %s", snapshot, decision)
        return decision

    @staticmethod
    def _setup_name(ema_gates: GateResults, vwap_gates: GateResults) -> str:
        """Which setup earned the ENTRY. Both can, and knowing that matters:
        it is the only way to tell whether the two agree in practice."""
        if ema_gates.passed and vwap_gates.passed:
            return f"{SETUP_EMA}+{SETUP_VWAP}"
        return SETUP_EMA if ema_gates.passed else SETUP_VWAP

    def _evaluate_gates(self, s: SignalSnapshot, bullish: bool, setup: str = SETUP_EMA) -> GateResults:
        trend_want = TrendState.BULLISH if bullish else TrendState.BEARISH
        pullback_want = PullbackState.BULLISH if bullish else PullbackState.BEARISH
        rejection_want = RejectionState.BULLISH if bullish else RejectionState.BEARISH

        # The ONLY difference between the setups: which level the two bar-shape
        # gates measure retracement against. Everything below is shared.
        if setup == SETUP_VWAP:
            pullback, rejection = s.vwap_pullback, s.vwap_rejection
        else:
            pullback, rejection = s.pullback, s.rejection

        trend_ok = s.trend == trend_want
        pullback_ok = pullback == pullback_want
        rejection_ok = rejection == rejection_want
        volume_ok = s.volume in (VolumeState.HIGH, VolumeState.NORMAL)
        oi_ok = s.open_interest in (OpenInterestState.RISING, OpenInterestState.FLAT)
        # Long: price must not be rejected at VAH. Short: not rejected at VAL.
        level_ok = s.vah != LevelState.REJECTED if bullish else s.val != LevelState.REJECTED

        return GateResults(
            trend=trend_ok, pullback=pullback_ok, rejection=rejection_ok,
            volume=volume_ok, open_interest=oi_ok, level=level_ok,
        )

    def _grade(self, status: Status, s: SignalSnapshot) -> Grade:
        if status == Status.WAIT:
            return Grade.IGNORE

        weak_count = 0
        if s.volume == VolumeState.NORMAL:
            weak_count += 1
        if s.open_interest == OpenInterestState.FLAT:
            weak_count += 1

        if weak_count == 0:
            return Grade.A_PLUS
        if weak_count == 1:
            return Grade.A
        if weak_count == 2:
            return Grade.B
        return Grade.IGNORE
