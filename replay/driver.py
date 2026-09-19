"""
replay/driver.py — feed one past session through the live engine.

The loop drives SessionRunner.tick(), it does NOT reassemble
signal -> decision -> alert. Before the 12 Aug extraction the engine existed twice
and a replay loop had to build its own; rebuilding it here would make replay the
fourth copy, which is the thing the extraction removed. session_runner was written
synchronous and NiceGUI-free precisely so this could call it.

Safe with no feed: _guard_session_date compares the session's date against t (same
day), _watch_feed returns at its first branch when state["feed"] is None, and
_advance_and_write needs only state["contract"].

See Markdowns/replay_engine_spec_close_only.md §7.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import config
from engine import LiveFiveMinuteSession, SignalStateLog
from session_runner import SessionRunner

from replay.data import (aggregate_five_minute, assert_front_month,
                         fetch_minute_candles, front_month_window,
                         previous_trading_day)
from replay.ticks import BUILDERS, TICK_MODE, build_ticks

IST = ZoneInfo("Asia/Kolkata")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "replay_out")


class ReplayResult:
    def __init__(self, day, tradingsymbol, alerts, nearmiss, bars, ticks, tick_mode):
        self.day, self.tradingsymbol = day, tradingsymbol
        self.alerts, self.nearmiss = alerts, nearmiss
        self.bars, self.ticks = bars, ticks
        self.tick_mode = tick_mode

    def __repr__(self):
        return (f"<ReplayResult {self.day} {self.tradingsymbol} {self.tick_mode} "
                f"{len(self.alerts)} alerts, {len(self.nearmiss)} near-miss, "
                f"{self.bars} bars from {self.ticks} ticks>")


def _session_bounds(day: date):
    open_at = datetime(day.year, day.month, day.day,
                       config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE, tzinfo=IST)
    close_at = datetime(day.year, day.month, day.day,
                        config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE, tzinfo=IST)
    return open_at, close_at


def build_seed(tradingsymbol: str, day: date) -> List:
    """SEED_BARS five-minute bars from the PRIOR archived session.

    Deviates from the spec, which calls fetch_session_five_minute_candles(). That
    needs a live token and a live API call, which would make replay non-hermetic and
    break the "a cache hit issues no API call" test. The archive already holds the
    prior session at finer resolution, so the seed is derived from it instead.

    Same contract as live: the last SEED_BARS bars before today's open, warming EMA10
    and the volume baseline ONLY. They must never reach VWAP or the profile — the
    session keeps them in .seed for exactly that reason.
    """
    prev = previous_trading_day(tradingsymbol, day)
    if prev is None:
        raise RuntimeError(
            f"no archived session before {day} for {tradingsymbol} — cannot seed. "
            f"Replaying the first day of a contract would run with relative volume "
            f"pinned near 1.00 until the 20th bar, and nothing would report it.")
    # Warn, don't raise, when the seed session predates the front-month window.
    # The replayed day itself is asserted; this is the softer, known hazard that
    # instruments.py already flags for live — the volume baseline is warmed from a
    # thinly-traded far-month session, so relative volume reads high and Volume can
    # grade A+ on ordinary activity all morning.
    start, _ = front_month_window(tradingsymbol)
    if prev < start:
        print(f"WARNING: {day} seeds from {prev}, before {tradingsymbol} became the "
              f"front month on {start}. The volume baseline is far-month thin, so "
              f"relative volume will read high. Treat this session's grades with "
              f"suspicion.")

    bars = aggregate_five_minute(fetch_minute_candles(None, tradingsymbol, 0, prev,
                                                      allow_api=False))
    return bars[-config.SEED_BARS:]


def replay_day(tradingsymbol: str, day: date, contract=None,
               write_signal_log: bool = True,
               tick_mode: Optional[str] = None) -> ReplayResult:
    if not config.DRY_RUN:
        raise RuntimeError(
            "replay requires BN_DRY_RUN=1. Ungated it would append a replayed "
            "session into the live csv/ corpus — the very thing replay exists to "
            "analyse — and attempt Telegram delivery for a session that has ended.")

    # Before anything is fetched: a far-month day would replay without error and
    # produce numbers that look ordinary.
    assert_front_month(tradingsymbol, day)

    open_at, close_at = _session_bounds(day)
    candles = fetch_minute_candles(None, tradingsymbol, 0, day, allow_api=False)
    if not candles:
        raise RuntimeError(f"{tradingsymbol} {day}: no archived candles")
    tick_mode = tick_mode or TICK_MODE
    ticks = build_ticks(candles, mode=tick_mode)

    seed = build_seed(tradingsymbol, day)
    # Assert rather than skip. A short seed is the 11 Aug failure shape: nothing
    # errors, write_output fires on schedule, and the volume gate measures against
    # a one-bar average all day. Skipping N sessions silently is a policy someone
    # has to remember; this fails on the day it matters.
    assert len(seed) == config.SEED_BARS, (
        f"{day}: seed has {len(seed)} bars, expected {config.SEED_BARS}")

    session = LiveFiveMinuteSession(
        historical_bars=[], now=open_at, bin_size=config.BIN_SIZE,
        value_area_pct=config.VALUE_AREA_PCT, ema_period=config.EMA_PERIOD,
        seed_bars=seed)
    # Without this the guard at engine.py:270 drops the FIRST tick's volume, because
    # last_cumulative_volume is None and there is nothing to diff against.
    session.last_cumulative_volume = 0.0

    # Fresh instances per day (D5): a SessionRunner owns one of each engine, so a new
    # runner is what guarantees no latch, hysteresis or dedupe state crosses the
    # boundary when several days are replayed in one process.
    runner = SessionRunner()
    runner.state["contract"] = contract or _StubContract(tradingsymbol)
    runner.state["live_session"] = session
    runner.state["feed"] = None
    if write_signal_log:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # Tick mode in the filename: the two modes produce different signal streams
        # for the same day, and a shared path would let whichever ran last silently
        # stand in for the other.
        runner.signal_log = SignalStateLog(
            path=os.path.join(OUTPUT_DIR,
                              f"replay_signal_{day:%Y-%m-%d}_{tick_mode}.csv"))

    nearmiss, i, t = [], 0, open_at
    step = timedelta(seconds=config.SNAPSHOT_INTERVAL_SECONDS)
    while t <= close_at:
        # Drained BEFORE tick() so a tick stamped inside the window lands in the bar
        # it belongs to. Never read a candle later than t — lookahead is what makes a
        # backtest look brilliant and a live system lose money.
        while i < len(ticks) and ticks[i].timestamp <= t:
            k = ticks[i]
            session.apply_tick(k.price, k.cumulative_volume, k.oi, k.timestamp)
            i += 1
        runner.tick(t)
        _record_nearmiss(nearmiss, runner.state, t)
        t += step

    return ReplayResult(day, tradingsymbol, list(runner.alert_engine.history),
                        nearmiss, session_bar_count(session), len(ticks), tick_mode)


def session_bar_count(session) -> int:
    return len(session.completed)


class _StubContract:
    """_advance_and_write only needs tradingsymbol; the token is never used because
    replay reads candles from the archive."""
    def __init__(self, tradingsymbol):
        self.tradingsymbol = tradingsymbol
        self.instrument_token = 0


def _record_nearmiss(out: list, state: dict, t: datetime) -> None:
    """EVERY failing gate on every blocked setup, one row per gate.

    This used to record only bars where exactly ONE gate failed, on the reasoning
    that with two or more failing, "which one blocked it" has no answer. That
    reasoning is fine, and the metric built on it was still actively misleading:

    replaying 14 Aug close-only, rejection blocked 11 of the 13 live alerts — and
    appeared in this log ONCE, because rejection and pullback fail TOGETHER when a
    bar has no wicks. The metric under-reported precisely the gate that had broken
    the run, and it took a per-alert audit to find what the histogram had hidden.

    So the raw record is now complete, and `blocker_count` lets a consumer recover
    the old single-blocker view as a FILTER. It must never be the only view again:
    any two gates that fail together are invisible to it.
    """
    decision = state.get("decision")
    if decision is None:
        return
    for side, attr in (("long", "long_gates"), ("short", "short_gates"),
                       ("long", "long_vwap_gates"), ("short", "short_vwap_gates")):
        gates = getattr(decision, attr, None)
        if gates is None or gates.passed:
            continue
        failed = gates.failed
        for gate in failed:
            out.append({"timestamp": t.isoformat(), "bar_start": state.get("bar_start"),
                        "direction": side, "setup": attr.replace("_gates", ""),
                        "blocking_gate": gate, "blocker_count": len(failed)})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Replay one archived session.")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--symbol", default="BANKNIFTY26AUGFUT")
    p.add_argument("--mode", default=TICK_MODE, choices=sorted(BUILDERS),
                   help="tick model; close-only is the control, see replay/ticks.py")
    a = p.parse_args(argv)

    result = replay_day(a.symbol, date.fromisoformat(a.date), tick_mode=a.mode)
    print(f"{result}\n  tick_mode={result.tick_mode}")
    for rec in result.alerts:
        print(f"  {rec.timestamp:%H:%M:%S} {rec.grade or '':3} {rec.direction or '':5} "
              f"{(rec.setup or ''):9} {rec.current_price}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
