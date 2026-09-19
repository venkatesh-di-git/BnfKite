"""
session_runner.py — the live engine, shared by main.py and app.py.

Exists because the same engine was maintained twice and drifted three ways in a
single week: the session-date guard became an in-place teardown in app.py and a
sys.exit(1) in main.py; feed rebuild ended up in three places; and the auth retry
landed only in app.py, so the headless runner could detect a dead socket and
never fix it.

**This module must never import NiceGUI.** That is the structural half of the
10 Aug fix. Engine work used to live inside ui.timer callbacks, which NiceGUI
cancels when no browser attaches — the scanner ran 14 hours and produced nothing
while every check reported healthy. A module with no access to the UI toolkit
cannot hold a stray ui.notify, enforced by the import graph rather than by a test
someone has to remember to run.

Everything here is SYNCHRONOUS. main.py calls it directly from its loop; app.py
wraps each call in run.io_bound so the event loop stays free. That also makes it
callable from a replay harness later without a third copy appearing.
"""

import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import ai_overlay
import config
from alert_engine import AlertEngine
from decision_engine import DecisionEngine
from engine import (LiveFiveMinuteSession, SignalStateLog, fetch_session_five_minute_candles,
                    is_market_hours, measured_fields, write_output)
from kite_ticker import LiveTickFeed
from signal_engine import IndicatorSnapshot, SignalEngine

IST = ZoneInfo("Asia/Kolkata")


def initial_state() -> dict:
    """The shared state dict. app.py aliases this — `state = runner.state` — so
    its ~123 existing `state[...]` readers keep working untouched. The runner
    must therefore only ever MUTATE self.state, never rebind it, or the
    dashboard silently detaches and stops updating."""
    return {
        "kite": None, "contract": None, "profile": None, "result": None,
        "current_price": None, "vwap": None, "ema_10": None, "oi": None,
        "oi_pattern": None, "last_updated": None, "last_write_at": None,
        "candle_count": 0, "error": None, "polling_enabled": True,
        "login_url": None, "live_session": None, "feed": None,
        "last_logged_candle_count": 0, "signal_snapshot": None, "decision": None,
        "current_alert": None, "feed_was_stale": False,
        # When the current feed was started — lets the recovery check measure
        # silence for a feed that has never delivered a tick, which
        # seconds_since_last_tick() reports as None rather than a duration.
        "feed_started_at": None,
        # Popups queued for whichever browser is attached, if any. The event
        # itself goes to Telegram via alert_engine; this is only the extra.
        "notices": [],
        # Premarket context, loaded once at startup from premarket_state.json
        # and read ONLY by the AI overlay. Nothing in the engine consults these:
        # no gate, grade or latch may ever depend on a premarket figure — that
        # was measured and rejected (Markdowns/Session_Bias_Engine_V1.md).
        "india_vix": None, "premarket_summary": None,
    }


class SessionRunner:
    """Owns the live session, feed and engines. One instance per process."""

    def __init__(self, alert_engine: Optional[AlertEngine] = None):
        self.state = initial_state()
        self.signal_engine = SignalEngine()
        self.decision_engine = DecisionEngine()
        # Dry-run wiring lives HERE rather than in alert_engine.py, which is one
        # of the three modules engine_version() hashes — putting it there would
        # move b3620733 for a change that alters no rule.
        self.alert_engine = alert_engine or AlertEngine(enable_telegram=not config.DRY_RUN)
        self.signal_log = SignalStateLog()
        # ONE lock across both entry points, not a flag per method.
        #
        # NiceGUI timers do not overlap with themselves — Timer._run_in_loop
        # awaits the invocation before sleeping again — so tick() cannot race
        # tick(). But ensure_started() and tick() are SEPARATE timers (30s and
        # 1s), and ensure_started can be several seconds inside a REST bootstrap
        # while tick reaches advance() and write_output on the same state.
        self._lock = threading.RLock()

    # ---------------------------------------------------------------
    # Feed lifecycle
    # ---------------------------------------------------------------
    def _bootstrap_session(self, now: datetime) -> LiveFiveMinuteSession:
        seed_bars, today_bars = fetch_session_five_minute_candles(
            self.state["kite"], self.state["contract"].instrument_token, now)
        return LiveFiveMinuteSession(today_bars, now, config.BIN_SIZE, config.VALUE_AREA_PCT,
                                     ema_period=config.EMA_PERIOD, seed_bars=seed_bars)

    def stop(self) -> None:
        feed = self.state.get("feed")
        if feed:
            feed.stop()
        self.state["feed"] = None
        self.state["live_session"] = None
        self.state["feed_started_at"] = None

    def ensure_started(self, now: Optional[datetime] = None) -> None:
        """Start the feed if it should be running and isn't. Safe to call on a
        timer — returns immediately when a feed already exists."""
        now = now or datetime.now(IST)
        with self._lock:
            if not self.state["polling_enabled"]:
                return
            if not self.state["kite"] or not self.state["contract"] or self.state["feed"]:
                return
            if not is_market_hours(now):
                return
            try:
                session = self._bootstrap_session(now)
                # The second number is the one that matters, and it is the only
                # evidence there is. An under-seeded session is invisible to
                # every other check: write_output() keeps firing on schedule so
                # latest_volume_profile.json stays fresh and the health probe
                # stays green, while the volume gate measures against a one-bar
                # average. That cost 11 Aug, and healthcheck.py names this line
                # as what covers its blind spot. Printed BEFORE the websocket, so
                # it survives a feed that fails to connect.
                print(f"Seeded {len(session.completed)} bar(s) from today's history so far, plus "
                      f"{len(session.seed)} prior-session bar(s) warming EMA10 and the volume baseline.")
                # LiveTickFeed's on_tick signature — (price, cumulative_volume,
                # oi, timestamp) — matches apply_tick exactly, so it passes
                # straight through with no wrapping.
                feed = LiveTickFeed(config.KITE_API_KEY, self.state["kite"].access_token,
                                    self.state["contract"].instrument_token,
                                    on_tick=session.apply_tick)
                self.state["live_session"] = session
                self.state["feed"] = feed
                self.state.update(session.snapshot())
                feed.start()
                print("Websocket starting — waiting for ticks...\n")
                self.state["feed_started_at"] = datetime.now(IST)
                self.state["error"] = None
                # Telegram gets the same seed count the print above carries. Sent
                # AFTER feed.start() on purpose: this is "the session is live",
                # and a socket that failed to connect raises out of here into the
                # error alert instead, which is the more useful message.
                #
                # Not latched, by decision. _watch_feed's auto-recovery calls
                # rebuild_feed(), which clears state["feed"] and re-enters here,
                # so a flapping feed WILL resend this alongside the "reconnected
                # automatically" notice.
                # Structured values, not a sentence: the wording is built for
                # Telegram at send time, so alert_log.csv keeps a number.
                self.alert_engine.process_startup(
                    self.state["contract"].tradingsymbol, len(session.seed),
                    config.SEED_BARS)
            except Exception as e:
                self.state["live_session"] = None
                self.state["feed"] = None
                self.state["feed_started_at"] = None
                self.state["error"] = f"Could not start live market stream: {e}"
                # The event, not just the screen: this fires from a timer, so an
                # engine that cannot start would otherwise fail in silence. Same
                # string each time, so process_error's de-dupe suppresses repeats
                # and clear_error() re-arms it.
                self.alert_engine.process_error(self.state["error"])

    def rebuild_feed(self, now: Optional[datetime] = None) -> bool:
        """Tear down and start fresh. Returns whether a feed is running after."""
        now = now or datetime.now(IST)
        with self._lock:
            self.stop()
            self.ensure_started(now)
            return self.state["feed"] is not None

    # ---------------------------------------------------------------
    # The per-tick engine step
    # ---------------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(IST)
        with self._lock:
            self._guard_session_date(now)
            self._advance_and_write(now)
            self._watch_feed(now)
            self.evaluate_signals(now)

    def _guard_session_date(self, now: datetime) -> None:
        """A session must never outlive the day it was seeded for: self.completed
        would accumulate across the boundary, giving a multi-day POC/VAH/VAL and
        a VWAP cumulative from yesterday — wrong numbers that raise nothing and
        that alerts then fire on. Checked BEFORE advance(), or the stale session
        takes one more step."""
        session = self.state["live_session"]
        if session and session.is_from_a_previous_day(now):
            self.stop()
            self.state["error"] = "Session was from a previous day — rebuilding."

    def _advance_and_write(self, now: datetime) -> None:
        session = self.state["live_session"]
        if not session:
            return
        self.state.update(session.advance(now))
        self.state["last_updated"] = now
        result = self.state["result"]
        bar_just_closed = bool(result) and self.state["candle_count"] > self.state["last_logged_candle_count"]
        due = (self.state["last_write_at"] is None or
               (now - self.state["last_write_at"]).total_seconds() >= config.LOG_WRITE_INTERVAL_SECONDS)
        if result and (bar_just_closed or due):
            write_output(result, self.state["contract"].tradingsymbol, now,
                         current_price=self.state["current_price"], vwap=self.state["vwap"],
                         ema_10=self.state["ema_10"], oi=self.state.get("oi"),
                         oi_pattern=self.state.get("oi_pattern"))
            self.state["last_write_at"] = now
            if bar_just_closed:
                self.state["last_logged_candle_count"] = self.state["candle_count"]

    def _watch_feed(self, now: datetime) -> None:
        """Report staleness, then rebuild a feed that has gone quiet for good.
        KiteTicker retries internally, but once it gives up nothing else recovers
        the connection — the loop just keeps advancing a session receiving no
        ticks while write_output carries on, so the health probe stays green."""
        feed = self.state.get("feed")
        stale_for = feed.seconds_since_last_tick() if feed else None
        is_stale = stale_for is not None and stale_for > config.TICK_STALE_SECONDS
        if is_stale and not self.state["feed_was_stale"]:
            self.alert_engine.process_error(f"WebSocket disconnected: no tick for {stale_for:.0f}s")
        elif not is_stale and self.state["feed_was_stale"]:
            self.alert_engine.clear_error()
        self.state["feed_was_stale"] = is_stale

        if not (feed and self.state["polling_enabled"] and is_market_hours(now)):
            return
        # Measure from the last tick, or from feed start when none ever arrived —
        # seconds_since_last_tick() reports None there, which the staleness check
        # above treats as "not stale".
        silence_for = stale_for
        if silence_for is None and self.state["feed_started_at"]:
            silence_for = (now - self.state["feed_started_at"]).total_seconds()
        if silence_for is not None and silence_for > config.FEED_RECOVERY_SECONDS:
            if self.rebuild_feed(now):
                msg = f"Feed was silent {silence_for:.0f}s — reconnected automatically"
                self.alert_engine.process_error(msg)
                self.state["notices"].append((msg, "warning"))

    def evaluate_signals(self, now: Optional[datetime] = None) -> None:
        """Signal -> Decision -> Alert -> log. Neither engine talks to Kite or
        the UI. Runs every tick rather than from a panel, so alerts keep their
        cadence regardless of whether anything is being rendered."""
        now = now or datetime.now(IST)
        s = self.state
        if not s["live_session"]:
            return
        # Already an immutable copy taken inside the session's lock — safe to
        # read while the websocket thread mutates the forming bar.
        candle = s.get("current_candle")
        result = s.get("result")
        indicators = IndicatorSnapshot(
            vwap=s.get("vwap"), ema10=s.get("ema_10"), ema10_prev=s.get("ema_10_prev"),
            sma20_volume=s.get("average_bar_volume"),
            current_oi=s.get("oi"), previous_oi=s.get("previous_oi"),
            poc=result.poc if result else None, vah=result.vah if result else None,
            val=result.val if result else None, elapsed_seconds=s.get("elapsed_seconds"),
        )
        # The previous snapshot is what the hysteresis rules read to know which
        # side of a threshold they are already on — the engine stays stateless.
        snapshot = self.signal_engine.evaluate_all(candle, indicators, s.get("signal_snapshot"))
        decision = self.decision_engine.evaluate(snapshot)
        # now= is what makes replay reproducible: without it the record falls back
        # to datetime.now(), so two replays of the same day can never be
        # byte-identical and the golden file is impossible.
        #
        # tzinfo stripped deliberately. `now` is IST-aware; alert_log.csv
        # timestamps have always been naive IST (datetime.now() on an
        # IST-configured box), so passing the aware value would append "+05:30" to
        # every new row and split the file into two formats mid-stream.
        # indicators is passed for DISPLAY only — the Telegram message shows VWAP,
        # EMA10 and slope as numbers, and this is the one place where both the
        # decision and the indicators it was evaluated against are in scope. The
        # same object already feeds measured_fields() three lines below, so the
        # message and signal_log.csv report identical values by construction
        # rather than by two paths that agree until one is edited.
        alert = self.alert_engine.process_decision(decision, s.get("current_price"),
                                                   bar_start=s.get("bar_start"),
                                                   now=now.replace(tzinfo=None),
                                                   indicators=indicators)
        # current_price stays separate from candle.close: with no bar forming,
        # snapshot() hands back the last COMPLETED candle while current_price is
        # still the live tick.
        self.signal_log.record(snapshot, decision, now,
                               current_price=s.get("current_price"),
                               bar_start=s.get("bar_start"),
                               **measured_fields(candle, indicators))
        s["signal_snapshot"] = snapshot
        s["decision"] = decision
        if alert:
            s["current_alert"] = alert
            # Trailing AI comment on A/A+ only. Fire-and-forget on its own
            # thread — nothing here waits on it, and it cannot delay, gate or
            # suppress the alert that already went out above.
            #
            # Lives here rather than in alert_engine.py because that module is
            # hashed by engine_version(); this call site is not, so the overlay
            # can change without re-freezing goldens. Same reasoning as the
            # dry-run wiring in __init__.
            #
            # enable_telegram, not config.DRY_RUN directly: a replay or test
            # engine constructed with enable_telegram=False must not reach the
            # network either, and that flag is the one the AlertEngine itself
            # honours.
            ai_overlay.maybe_send(alert, s,
                                  enabled=self.alert_engine.enable_telegram)

    def drain_notices(self) -> list:
        """Popups for whoever is watching. Discarding these headless is correct —
        the event already went to Telegram."""
        with self._lock:
            out, self.state["notices"] = self.state["notices"], []
            return out

    def close(self) -> None:
        self.stop()
        self.alert_engine.close()
