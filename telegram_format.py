"""
telegram_format.py — the trader-facing Telegram message.

DELIBERATELY OUTSIDE THE HASHED SET. engine_version() hashes signal_engine.py,
decision_engine.py and alert_engine.py. Wording is presentation, and a reworded
message should never move the fingerprint or force a VM re-baseline. Keeping the
formatter here means this file can change freely forever; only the one-line call
in alert_engine.py is hashed.

A VIEW, NOT A SECOND ENGINE. Every line below is a direct read of something the
engine already decided. Two fields were deliberately left out of this message
because they are NOT direct reads, and printing them would put a second copy of a
live rule in the presentation layer:

  Volume as a ratio ("1.31x") — the projected relative volume exists only inside
  SignalEngine._evaluate_volume, behind a minimum-elapsed guard, a clamp, an
  absolute floor and Schmitt banding. measured_fields() carries the INPUTS
  (bar_volume, sma20_volume, elapsed_seconds), never the result. Recomputing
  projected/sma20 here would print "1.31x" on a bar the engine itself called
  Unknown. The state word is what the engine actually concluded.

  VP from a price comparison — SignalSnapshot.vah/.val are already LevelState,
  decided by _evaluate_volume_profile. That enum includes REJECTED, which a naive
  `price > vah` can never produce, so the comparison would disagree with the logs
  on exactly the bars that matter.

Missing values render as MISSING rather than being reconstructed. A formatter that
fills in a blank is a formatter that has started calculating.

See Markdowns/Telegram_Alert_Format_V2_Detailed-1.md.
"""

from typing import Optional

MISSING = "—"

# Decision.setup is "ema", "vwap" or "ema+vwap" — all three occur. In the 11-14
# Aug live log: vwap 25, ema+vwap 17, ema 14, so leaving the combined case
# unmapped would have hit 30% of alerts.
SETUP_LABELS = {
    "ema": "EMA Pullback + Rejection",
    "vwap": "VWAP Pullback + Rejection",
    # "(both)" because a bare "+" reads as a list of alternatives. This value
    # means both six-gate setups passed AT ONCE — price pulled back to EMA10 and
    # to VWAP and rejected from both — which is the strongest confirmation the
    # engine expresses, and the two levels are routinely tens of points apart.
    "ema+vwap": "EMA + VWAP (both)",
}

DIRECTION_ICONS = {"Long": "🟢", "Short": "🔴"}


def _price(value: Optional[float]) -> str:
    """Whole points, no thousands separator — matches the agreed sample format.
    Bank Nifty ticks in 0.05 but nobody reads a phone alert to two decimals, and
    the exact value stays in alert_log.csv."""
    return MISSING if value is None else f"{value:.0f}"


def _signed(value: Optional[float]) -> str:
    return MISSING if value is None else f"{value:+.2f}"


def _arrow(value: Optional[float], reference: Optional[float]) -> str:
    """Which side of the level price sits — presentation of two values already
    on the record, not a new comparison rule: nothing downstream reads it and no
    gate depends on it."""
    if value is None or reference is None:
        return ""
    return " ↑" if reference > value else (" ↓" if reference < value else "")


def _state(value) -> str:
    """Enum -> its display string. SignalSnapshot fields are `str, Enum`, so the
    value IS the label the engine chose; Unknown becomes MISSING so an
    undetermined gate never reads as a real state."""
    if value is None:
        return MISSING
    text = getattr(value, "value", str(value))
    return MISSING if text == "Unknown" else text


def volume_profile_status(vah, val) -> str:
    """Compact VP position, read off the LevelState the engine already set.

    VAH Above means close finished above the value-area high; VAL Below means
    below the low. Anything else — including REJECTED, where price reached a
    level and was turned away — is inside or at the value area, which is the
    honest description of both.
    """
    vah_text, val_text = _state(vah), _state(val)
    if vah_text == "Above":
        return "Above VAH"
    if val_text == "Below":
        return "Below VAL"
    if vah_text == MISSING and val_text == MISSING:
        return MISSING
    return "Inside Value"


def format_trading_alert(record) -> str:
    """AlertRecord -> the message. Pure: reads only the record."""
    icon = DIRECTION_ICONS.get(record.direction, "")
    direction = (record.direction or MISSING).upper()
    setup = SETUP_LABELS.get(record.setup, record.setup or MISSING)
    snapshot = getattr(record, "signals", None)

    lines = [
        f"{icon} {record.grade or MISSING} {direction}".strip(),
        "",
        f"Price: {_price(record.current_price)}",
        f"Setup: {setup}",
        "",
        f"VWAP: {_price(record.vwap)}{_arrow(record.vwap, record.current_price)}",
        f"EMA10: {_price(record.ema10)}{_arrow(record.ema10, record.current_price)}",
        # Named, not just "Slope". It is EMA10's change over
        # EMA_SLOPE_LOOKBACK_SECONDS (60s) — nothing to do with VWAP, which has no
        # slope anywhere in the engine. Printed under a VWAP line, a bare "Slope"
        # invites exactly the misreading the format spec warned about, and the
        # "/min" is what makes a points figure mean anything.
        f"EMA10 slope: {_signed(record.ema_slope)} /min",
    ]
    if snapshot is not None:
        lines += [
            f"Volume: {_state(snapshot.volume)}",
            f"OI: {_state(snapshot.open_interest)}",
            f"VP: {volume_profile_status(snapshot.vah, snapshot.val)}",
        ]
    return "\n".join(lines)


def format_error_alert(record) -> str:
    return f"⚠️ {record.reason_list[0] if record.reason_list else 'Unknown error'}"


def format_startup_alert(record) -> str:
    """Built from the record's STRUCTURED fields at send time.

    The emoji sentence is never persisted: alert_log.csv stores
    `seed_bars=36 expected_bars=36`, which greps and parses as a number, while
    this renders the human version for the phone. Falls back to the stored ASCII
    line for any record that predates those fields.
    """
    if record.seed_count is None:
        return record.reason_list[0] if record.reason_list else "Session started."
    return startup_message(record.tradingsymbol, record.seed_count,
                           record.expected_seed)


def startup_message(tradingsymbol: Optional[str], seed_bars: int,
                    expected_bars: int) -> str:
    """One plain sentence confirming the session is live, with the seed count.

    THE SEED COUNT IS THE POINT. An under-seeded session is invisible to every
    other check: write_output() keeps firing on schedule so the health probe stays
    green, while the volume gate measures against a one-bar average all day. That
    is what happened on 11 Aug, and until now the only evidence was a print in the
    journal that nobody reads at 09:15.

    The warning triggers on ANY shortfall rather than a separate threshold.
    `expected_bars` is config.SEED_BARS, which replay already asserts equality
    against — so live and replay agree on what "properly seeded" means, and there
    is no second number to keep in sync. A threshold like "warn under 15" would
    have stayed silent across the whole 15-35 range, every value of which is
    already a degraded session.
    """
    symbol = tradingsymbol or "Session"
    bars = "bar" if seed_bars == 1 else "bars"
    if seed_bars < expected_bars:
        return (f"🟢 {symbol} session started. Seed: {seed_bars} {bars} "
                f"— that's low, expected {expected_bars}, check it.")
    return f"🟢 {symbol} session started. Seed: {seed_bars} {bars}."
