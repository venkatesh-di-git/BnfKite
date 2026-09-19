"""
signal_engine.py

Implements SignalEngine per Signal_Decision_Engine_Specification_V1.md:
turns a Candle + IndicatorSnapshot into a structured SignalSnapshot.
Pure, stateless rules only — no scoring, no trade decisions, no Kite
access, no indicator math (that's engine.py's job). See
decision_engine.py for what consumes SignalSnapshot.

Numeric thresholds the spec leaves unspecified (OI Flat band, wick
significance) are documented where used and live in config.py so
they're easy to retune without touching rule logic.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

import config
from volume_profile import Candle

logger = logging.getLogger(__name__)


class TrendState(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"
    UNKNOWN = "Unknown"


class PullbackState(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NONE = "None"
    UNKNOWN = "Unknown"


class RejectionState(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NONE = "None"
    UNKNOWN = "Unknown"


class VolumeState(str, Enum):
    HIGH = "High"
    NORMAL = "Normal"
    LOW = "Low"
    UNKNOWN = "Unknown"


class OpenInterestState(str, Enum):
    RISING = "Rising"
    FALLING = "Falling"
    FLAT = "Flat"
    UNKNOWN = "Unknown"


class LevelState(str, Enum):
    """Shared shape for POC/VAH/VAL. POC uses ABOVE/BELOW/AT; VAH/VAL use
    ABOVE/BELOW/REJECTED — each level only ever produces 4 of these 5."""
    ABOVE = "Above"
    BELOW = "Below"
    AT = "At"
    REJECTED = "Rejected"
    UNKNOWN = "Unknown"


@dataclass
class IndicatorSnapshot:
    vwap: Optional[float] = None
    ema10: Optional[float] = None
    # EMA10 as of config.EMA_SLOPE_LOOKBACK_SECONDS ago — the other end of the
    # Trend rule's slope. Not the last closed bar's EMA; see engine.snapshot().
    ema10_prev: Optional[float] = None
    sma20_volume: Optional[float] = None
    current_oi: Optional[float] = None
    previous_oi: Optional[float] = None
    poc: Optional[float] = None
    vah: Optional[float] = None
    val: Optional[float] = None
    # Age of the candle being evaluated, in seconds — supplied by
    # engine.snapshot(), which reports BAR_SECONDS for a completed bar.
    # Absent means "treat as complete", i.e. no projection.
    elapsed_seconds: Optional[float] = None


@dataclass
class SignalSnapshot:
    trend: TrendState
    pullback: PullbackState
    rejection: RejectionState
    volume: VolumeState
    open_interest: OpenInterestState
    poc: LevelState
    vah: LevelState
    val: LevelState
    details: Dict[str, str] = field(default_factory=dict)
    # The VWAP setup: a SECOND setup alongside the EMA10 one above, not a
    # replacement. pullback/rejection measure retracement to EMA10; these
    # measure it to VWAP. The two levels are routinely tens of points apart
    # (32.5 on 06 Aug at 11:50), so a pullback to one is not a pullback to the
    # other. Defaulted and after `details` so every existing construction of
    # this dataclass keeps working unchanged.
    vwap_pullback: PullbackState = PullbackState.UNKNOWN
    vwap_rejection: RejectionState = RejectionState.UNKNOWN


class SignalEngine:
    """Public: evaluate_all(). Private: one evaluate_* per SignalSnapshot
    category, matching the spec's SignalEngine API section."""

    def evaluate_all(self, candle: Optional[Candle], indicators: IndicatorSnapshot,
                     previous: Optional[SignalSnapshot] = None) -> SignalSnapshot:
        """`previous` is the last snapshot this caller accepted. It is what the
        hysteresis rules read to know which side of a threshold they are already
        on — a Schmitt trigger needs its current state.

        Passing it in rather than storing it keeps this engine a pure function
        (same inputs, same output), as the spec requires; the caller owns the
        state. `previous=None` disables hysteresis entirely and reproduces the
        plain threshold behaviour.
        """
        trend, trend_detail = self._evaluate_trend(candle, indicators, previous)
        pullback, pullback_detail = self._evaluate_pullback(candle, indicators)
        rejection, rejection_detail = self._evaluate_rejection(candle, indicators)
        volume, volume_detail = self._evaluate_volume(candle, indicators, previous)
        oi, oi_detail = self._evaluate_open_interest(indicators)
        poc, vah, val, vp_details = self._evaluate_volume_profile(candle, indicators)
        vwap_pullback, vwap_pullback_detail = self._evaluate_vwap_pullback(candle, indicators)
        vwap_rejection, vwap_rejection_detail = self._evaluate_vwap_rejection(candle, indicators)

        snapshot = SignalSnapshot(
            trend=trend, pullback=pullback, rejection=rejection, volume=volume,
            open_interest=oi, poc=poc, vah=vah, val=val,
            vwap_pullback=vwap_pullback, vwap_rejection=vwap_rejection,
            details={
                "trend": trend_detail, "pullback": pullback_detail, "rejection": rejection_detail,
                "volume": volume_detail, "open_interest": oi_detail, **vp_details,
                "vwap_pullback": vwap_pullback_detail, "vwap_rejection": vwap_rejection_detail,
            },
        )
        logger.debug("evaluate_all candle=%s indicators=%s -> %s", candle, indicators, snapshot)
        return snapshot

    def _evaluate_trend(self, candle: Optional[Candle], ind: IndicatorSnapshot,
                        previous: Optional[SignalSnapshot] = None):
        if candle is None or ind.vwap is None or ind.ema10 is None or ind.ema10_prev is None:
            return TrendState.UNKNOWN, "Insufficient data for Trend"

        # ema10_prev is EMA10 as of config.EMA_SLOPE_LOOKBACK_SECONDS ago, so
        # this is a rate of change, not a distance from a fixed level.
        slope = ind.ema10 - ind.ema10_prev
        hysteresis = config.EMA_SLOPE_HYSTERESIS_POINTS
        was = previous.trend if previous else None

        # Schmitt trigger: the threshold depends on the state we are already in.
        # Entering a trend demands slope beyond +/-h; staying in one only demands
        # that slope hasn't reversed past the opposite edge. A slope hovering at
        # zero therefore cannot toggle the state.
        enter_up = slope > hysteresis
        hold_up = was == TrendState.BULLISH and slope > -hysteresis
        enter_down = slope < -hysteresis
        hold_down = was == TrendState.BEARISH and slope < hysteresis

        # Price only — EMA10's position relative to VWAP is deliberately NOT
        # required. EMA10 is a smoothed, lagging value: on a genuine reclaim
        # price crosses VWAP first and EMA10 follows minutes later, so demanding
        # both turned the gate into a direction lock. Measured on 05 Aug: close
        # held above VWAP on 41 rows, close AND ema10 on 1 — the EMA term threw
        # away 40 of 41 — while the mirror case (close below, ema10 above)
        # happened 0 times, so the lag runs one way only.
        above = candle.close > ind.vwap
        below = candle.close < ind.vwap

        if above and (enter_up or hold_up):
            edge = -hysteresis if hold_up and not enter_up else hysteresis
            return TrendState.BULLISH, (
                f"Close {candle.close:.2f} above VWAP {ind.vwap:.2f}, "
                f"slope {slope:+.2f} ({'holding' if hold_up and not enter_up else 'entered'}, "
                f"exits below {edge:+.2f})")
        if below and (enter_down or hold_down):
            edge = hysteresis if hold_down and not enter_down else -hysteresis
            return TrendState.BEARISH, (
                f"Close {candle.close:.2f} below VWAP {ind.vwap:.2f}, "
                f"slope {slope:+.2f} ({'holding' if hold_down and not enter_down else 'entered'}, "
                f"exits above {edge:+.2f})")
        return TrendState.NEUTRAL, f"Trend conditions not aligned (slope {slope:+.2f})"

    def _evaluate_pullback(self, candle: Optional[Candle], ind: IndicatorSnapshot):
        if candle is None or ind.ema10 is None:
            return PullbackState.UNKNOWN, "Insufficient data for Pullback"
        ema10 = ind.ema10
        if candle.low <= ema10 and candle.close > ema10:
            return PullbackState.BULLISH, f"Low {candle.low:.2f} <= EMA10 {ema10:.2f} < Close {candle.close:.2f}"
        if candle.high >= ema10 and candle.close < ema10:
            return PullbackState.BEARISH, f"High {candle.high:.2f} >= EMA10 {ema10:.2f} > Close {candle.close:.2f}"
        return PullbackState.NONE, "No pullback to EMA10"

    def _evaluate_rejection(self, candle: Optional[Candle], ind: IndicatorSnapshot):
        if candle is None or ind.ema10 is None:
            return RejectionState.UNKNOWN, "Insufficient data for Rejection"
        ema10 = ind.ema10
        lower_wick = candle.low < min(candle.open, candle.close)
        upper_wick = candle.high > max(candle.open, candle.close)
        bullish_candle = candle.close > candle.open
        bearish_candle = candle.close < candle.open
        if lower_wick and bullish_candle and candle.close > ema10:
            return RejectionState.BULLISH, "Lower wick + bullish candle + Close above EMA10"
        if upper_wick and bearish_candle and candle.close < ema10:
            return RejectionState.BEARISH, "Upper wick + bearish candle + Close below EMA10"
        return RejectionState.NONE, "No rejection pattern"

    def _evaluate_vwap_pullback(self, candle: Optional[Candle], ind: IndicatorSnapshot):
        """Retracement to VWAP — the EMA10 rule's counterpart for the VWAP setup.

        Uses a proximity band rather than the strict straddle _evaluate_pullback
        applies to EMA10. On 06 Aug price bottomed at 58014.00 against a VWAP of
        58012.49 and rallied 100 points; it never crossed, so `low <= vwap`
        would have recorded nothing on precisely the setup this rule exists for.
        Price approaching a level and turning is the event — a clean touch does
        not require a tick through it.
        """
        if candle is None or ind.vwap is None:
            return PullbackState.UNKNOWN, "Insufficient data for VWAP Pullback"
        vwap = ind.vwap
        band = config.LEVEL_PROXIMITY_POINTS
        if candle.low <= vwap + band and candle.close > vwap:
            return PullbackState.BULLISH, (
                f"Low {candle.low:.2f} within {band:.0f} of VWAP {vwap:.2f} < Close {candle.close:.2f}")
        if candle.high >= vwap - band and candle.close < vwap:
            return PullbackState.BEARISH, (
                f"High {candle.high:.2f} within {band:.0f} of VWAP {vwap:.2f} > Close {candle.close:.2f}")
        return PullbackState.NONE, f"No pullback to VWAP {vwap:.2f}"

    def _evaluate_vwap_rejection(self, candle: Optional[Candle], ind: IndicatorSnapshot):
        """Rejection at VWAP, measured by where the close sits in the bar's range.

        Deliberately NOT the wick test _evaluate_rejection uses. That needs
        low < min(open, close), which a bar opening at its low can never satisfy
        — and a V-reversal off VWAP produces exactly that bar. Relaxing it to <=
        is not a fix but a tautology, since low IS the bar minimum; the gate
        would collapse to "bullish candle above VWAP".

        Close position in range asks the question the wick was meant to ask —
        did price probe one way and get pushed back — without depending on where
        the open happened to land.
        """
        if candle is None or ind.vwap is None:
            return RejectionState.UNKNOWN, "Insufficient data for VWAP Rejection"
        vwap = ind.vwap
        bar_range = candle.high - candle.low
        if bar_range <= 0:
            # A bar at its first tick has no range to measure. NONE, not a
            # ZeroDivisionError — snapshot() hands one out every bar boundary.
            return RejectionState.NONE, "No range yet for VWAP Rejection"
        held = config.VWAP_REJECTION_CLOSE_PCT
        close_from_low = (candle.close - candle.low) / bar_range
        close_from_high = (candle.high - candle.close) / bar_range
        if candle.close > candle.open and candle.close > vwap and close_from_low >= held:
            return RejectionState.BULLISH, (
                f"Close {candle.close:.2f} held {close_from_low:.0%} of range above VWAP {vwap:.2f}")
        if candle.close < candle.open and candle.close < vwap and close_from_high >= held:
            return RejectionState.BEARISH, (
                f"Close {candle.close:.2f} held {close_from_high:.0%} of range below VWAP {vwap:.2f}")
        return RejectionState.NONE, "No rejection at VWAP"

    def _evaluate_volume(self, candle: Optional[Candle], ind: IndicatorSnapshot,
                         previous: Optional[SignalSnapshot] = None):
        """Relative volume on a PROJECTED full-bar basis, so a bar is judged
        on its participation rate rather than on how far into it we are.

        Two guards stop a small numerator being multiplied into a fake spike:
        a minimum elapsed time, which bounds the multiplier, and an absolute
        floor on volume actually traded, which bounds the numerator. Both
        report Unknown, which fails the Decision Engine's mandatory Volume
        gate — so early-bar WAIT is explicit rather than a misleading Low.

        The floor is applied AFTER the bands, and only to High and Normal.
        Those are the two states that open the Decision Engine's volume gate
        (`volume in (HIGH, NORMAL)`), so they are the only ones an untrusted
        projection can turn into a trade. Low is exempt: it blocks entry
        exactly as Unknown does, and a bar too thin to clear the floor is its
        own evidence of thin volume. Applying the floor to Low as well meant a
        genuinely quiet bar still read "too early to project" at 285s of 300 —
        the state was hidden right up to the close, for no decision benefit.
        """
        if candle is None or not ind.sma20_volume:
            return VolumeState.UNKNOWN, "Insufficient data for Volume"

        elapsed = ind.elapsed_seconds if ind.elapsed_seconds is not None else config.BAR_SECONDS
        elapsed = min(max(elapsed, 0.0), config.BAR_SECONDS)

        if elapsed < config.VOLUME_PROJECTION_MIN_ELAPSED_SECONDS:
            return VolumeState.UNKNOWN, (
                f"Only {elapsed:.0f}s into bar (min "
                f"{config.VOLUME_PROJECTION_MIN_ELAPSED_SECONDS:.0f}s) — too early to project"
            )

        # max(elapsed, 1.0) guards division if MIN_ELAPSED is ever set to 0.0.
        projected = candle.volume * (config.BAR_SECONDS / max(elapsed, 1.0))
        relative_volume = projected / ind.sma20_volume
        pace = f"{candle.volume:.0f} in {elapsed:.0f}s -> projected {projected:.0f}"

        # Schmitt trigger on each band edge: once High, the ratio must fall a
        # further VOLUME_HYSTERESIS before dropping back, so a ratio resting on
        # 1.30 cannot toggle the state every tick.
        was = previous.volume if previous else None
        h = config.VOLUME_HYSTERESIS
        # Each edge moves AWAY from the state currently held: harder to leave
        # High (edge drops), harder to leave Low (edge rises).
        high_edge = config.VOLUME_HIGH_THRESHOLD - (h if was == VolumeState.HIGH else 0.0)
        low_edge = config.VOLUME_LOW_THRESHOLD + (h if was == VolumeState.LOW else 0.0)

        if relative_volume >= high_edge:
            state = VolumeState.HIGH
            detail = f"Projected relative volume {relative_volume:.2f} >= {high_edge:.2f} ({pace})"
        elif relative_volume >= low_edge:
            state = VolumeState.NORMAL
            detail = f"Projected relative volume {relative_volume:.2f} in Normal band ({pace})"
        else:
            # Thin bar, thin reading — consistent, so report it rather than
            # hiding it behind the floor.
            return VolumeState.LOW, f"Projected relative volume {relative_volume:.2f} < {low_edge:.2f} ({pace})"

        floor = config.VOLUME_PROJECTION_FLOOR_PCT * ind.sma20_volume
        if candle.volume < floor:
            return VolumeState.UNKNOWN, (
                f"Traded {candle.volume:.0f} < floor {floor:.0f} "
                f"({config.VOLUME_PROJECTION_FLOOR_PCT:.0%} of average) — "
                f"projected {state.value} at {relative_volume:.2f} not trusted"
            )
        return state, detail

    def _evaluate_open_interest(self, ind: IndicatorSnapshot):
        if ind.current_oi is None or not ind.previous_oi:
            return OpenInterestState.UNKNOWN, "Insufficient data for Open Interest"
        change_pct = (ind.current_oi - ind.previous_oi) / abs(ind.previous_oi)
        if change_pct > config.OI_FLAT_THRESHOLD_PCT:
            return OpenInterestState.RISING, f"OI {ind.previous_oi:.0f} -> {ind.current_oi:.0f} ({change_pct:+.2%})"
        if change_pct < -config.OI_FLAT_THRESHOLD_PCT:
            return OpenInterestState.FALLING, f"OI {ind.previous_oi:.0f} -> {ind.current_oi:.0f} ({change_pct:+.2%})"
        return OpenInterestState.FLAT, f"OI {ind.previous_oi:.0f} -> {ind.current_oi:.0f} ({change_pct:+.2%})"

    def _evaluate_volume_profile(self, candle: Optional[Candle], ind: IndicatorSnapshot):
        if candle is None or ind.poc is None or ind.vah is None or ind.val is None:
            unknown = LevelState.UNKNOWN
            return unknown, unknown, unknown, {
                "poc": "Insufficient data for POC", "vah": "Insufficient data for VAH", "val": "Insufficient data for VAL",
            }
        close = candle.close
        tolerance = config.LEVEL_PROXIMITY_POINTS

        if abs(close - ind.poc) <= tolerance:
            poc_state, poc_detail = LevelState.AT, f"Close {close:.2f} at POC {ind.poc:.2f}"
        elif close > ind.poc:
            poc_state, poc_detail = LevelState.ABOVE, f"Close {close:.2f} above POC {ind.poc:.2f}"
        else:
            poc_state, poc_detail = LevelState.BELOW, f"Close {close:.2f} below POC {ind.poc:.2f}"

        if close > ind.vah:
            vah_state, vah_detail = LevelState.ABOVE, f"Close {close:.2f} above VAH {ind.vah:.2f}"
        elif ind.vah - tolerance <= close <= ind.vah:
            vah_state, vah_detail = LevelState.REJECTED, f"Close {close:.2f} rejected at VAH {ind.vah:.2f}"
        else:
            vah_state, vah_detail = LevelState.BELOW, f"Close {close:.2f} below VAH {ind.vah:.2f}"

        if close < ind.val:
            val_state, val_detail = LevelState.BELOW, f"Close {close:.2f} below VAL {ind.val:.2f}"
        elif ind.val <= close <= ind.val + tolerance:
            val_state, val_detail = LevelState.REJECTED, f"Close {close:.2f} rejected at VAL {ind.val:.2f}"
        else:
            val_state, val_detail = LevelState.ABOVE, f"Close {close:.2f} above VAL {ind.val:.2f}"

        return poc_state, vah_state, val_state, {"poc": poc_detail, "vah": vah_detail, "val": val_detail}
