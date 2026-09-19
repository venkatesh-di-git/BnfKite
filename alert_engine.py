"""
alert_engine.py

Implements the Alert Engine per Alert_Dashboard_Spec_V1-1.md: consumes a
Decision (from decision_engine.py) and distributes it — Alert History
(in-memory + CSV) and Telegram. Never calculates indicators, signals, or
decisions, and never modifies what the Decision Engine produced.
"""

import csv
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

import requests

import config
from decision_engine import Decision, Status
from engine import engine_version, rotate_if_header_stale
from telegram_format import (format_error_alert, format_startup_alert,
                             format_trading_alert)

logger = logging.getLogger(__name__)

# Grade ordering for the latch's ceiling. Ignore is absent by construction —
# anything without a rank is not alertable, so this single mapping answers both
# "does it alert" and "is it an upgrade". Deriving TRADING_GRADES from it keeps
# the two from drifting apart the way two parallel literals would.
#
# Lives here rather than in config.py on purpose: engine_version() hashes this
# module's source, so a retuned ranking is fingerprinted automatically. In
# config.py it would be invisible unless separately added to _VERSIONED_CONSTANTS.
ALERT_GRADE_RANK = {"B": 1, "A": 2, "A+": 3}
TRADING_GRADES = tuple(ALERT_GRADE_RANK)

# --- Flip cooldown ---
#
# Withhold an alert that REVERSES DIRECTION within this many minutes of the last
# delivered one. Here rather than in config.py for the same reason as
# ALERT_GRADE_RANK: engine_version() hashes this module's source, so retuning
# either is fingerprinted automatically. In config.py it would be invisible
# unless separately added to _VERSIONED_CONSTANTS.
#
# WHY 5 AND NOT 12. Re-derived over the 56 live alerts of 11-14 Aug
# (replay/cooldown_study.py). The eight B-grade flip gaps are 1, 2, 2, 10, 11,
# 12, 17 and 29 minutes — a cluster, then a hole. Every value from 3 to 10
# minutes therefore suppresses exactly the same three alerts, and each of the
# four sessions agrees on that plateau individually. 12 minutes sits ONE MINUTE
# past a step edge, in the densest part of the distribution, where an alert
# arriving thirty seconds differently changes the outcome. 5 is mid-plateau.
#
# WHAT IT ACTUALLY DOES, stated plainly so it is not mistaken for more. Since
# every escaping grade is exempt, only B-grade alerts are suppressible at all:
# this is "withhold B-grade direction flips inside 5 minutes". It catches 4 of
# the 18 same-day flips in the live log — a noise filter, NOT a whipsaw fix. The
# other 14 flips are graded A or A+ and escape by design.
#
# Set to 0 to disable; behaviour then matches the pre-cooldown engine exactly.
FLIP_COOLDOWN_MINUTES = 5

# Grades that ignore the cooldown entirely. The best signals are the ones least
# worth withholding, and this is what keeps the mechanism conservative — at the
# cost of it catching under a quarter of flips.
COOLDOWN_ESCAPING_GRADES = ("A", "A+")

ALERT_LOG_FIELDS = ["id", "timestamp", "type", "direction", "grade", "confidence",
                    "setup", "current_price", "reason_list", "engine_version"]

SUPPRESSED_LOG_FIELDS = ["timestamp", "direction", "grade", "setup", "current_price",
                         "seconds_since_last", "blocked_by_direction",
                         "blocked_by_grade", "blocked_by_timestamp", "engine_version"]

_SHUTDOWN = object()  # sentinel that stops the delivery worker


@dataclass
class AlertRecord:
    id: str
    timestamp: datetime
    type: str  # "Trading" | "Error" | "Startup"
    direction: Optional[str]
    grade: Optional[str]
    confidence: Optional[int]
    current_price: Optional[float]
    reason_list: List[str] = field(default_factory=list)
    # Which setup earned the alert — "ema", "vwap" or "ema+vwap". The latch keys
    # on (bar_start, direction) only, so when both setups fire in one bar the
    # second is suppressed; signal_log.csv keeps the full picture for analysis.
    setup: Optional[str] = None

    # --- Display transport. Not decision inputs. ---
    #
    # The Telegram message wants VWAP, EMA10 and slope as numbers, and they were
    # not reachable: IndicatorSnapshot is an INPUT to SignalEngine.evaluate_all()
    # and is never stored on SignalSnapshot or Decision, so the values survived
    # downstream only as prose inside details["trend"]. Parsing that sentence was
    # the alternative, and it is the wrong architecture.
    #
    # Carried HERE rather than added to SignalSnapshot on purpose: SignalSnapshot
    # holds evaluated signals, IndicatorSnapshot holds raw indicator values, and
    # blurring that to make a message easier would be the wrong trade. These are
    # the exact values measured_fields() already writes to signal_log.csv
    # (engine.py:479) — the same numbers the rules compared, not a recomputation.
    #
    # `signals` is the SignalSnapshot itself, so the message can show the states
    # the engine concluded (Volume High, OI Falling, VP Above VAH) instead of
    # re-deriving them from raw values in the formatter.
    vwap: Optional[float] = None
    ema10: Optional[float] = None
    ema_slope: Optional[float] = None
    signals: Optional[object] = None

    # --- Startup records only ---
    #
    # Structured, so the CSV stores a number and the emoji sentence is built for
    # Telegram at send time and never persisted. `grep seed_bars= alert_log.csv`
    # then answers "was every session properly seeded" without parsing prose.
    tradingsymbol: Optional[str] = None
    seed_count: Optional[int] = None
    expected_seed: Optional[int] = None


class AlertEngine:
    """Duplicate rule: one alert per (bar, direction), re-firing only when the
    grade improves — TradingView's alert.freq_once_per_bar plus an upgrade
    path.

    The old rule keyed on (status, direction, grade) alone. Pullback and
    Rejection are bar-shape rules read off a FORMING bar, so near EMA10 they
    flip on every tick; each flip produced a WAIT, which is a different key,
    which cleared the guard and re-armed the next ENTRY. Measured on
    alert_log.csv: 32 alerts across 9 unique (bar, direction) pairs, one bar
    producing 7.

    A strict latch would have been wrong. In 7 of 8 multi-alert bars the FIRST
    alert carried the worst grade and the ceiling only climbed (B -> A -> A+),
    so latching on first sight hands you B on a bar that became A+. Re-firing
    on improvement keeps every upgrade and is bounded at 3 per bar per
    direction by construction, since the ceiling never ratchets down.

    Error conditions dedupe separately by message, since they aren't part of
    the Decision Engine's state, and are never latched.
    """

    def __init__(self, enable_telegram: bool = True):
        # An explicit switch rather than relying on a blank token: replay must
        # never deliver, and "the config happened to be empty" is not a
        # guarantee — a harness run on a configured machine would fire real
        # alerts for a session that ended weeks ago.
        self.enable_telegram = enable_telegram
        # Retained only for the bar_start-is-None path below. The latch is the
        # authority everywhere else: this key cannot distinguish bar N from bar
        # N+1, so leaving it in the main path would suppress the first alert of
        # every new bar whenever the grade happened to repeat.
        self._last_decision_key = None
        self._latch: dict = {}  # (bar_start, direction) -> best grade rank seen
        self._last_error_message = None
        self._prepared = False
        self.history: List[AlertRecord] = []
        # The last DELIVERED trading alert — what the flip cooldown measures
        # against. Never assigned from a withheld alert.
        self._last_alert: Optional[AlertRecord] = None
        self.suppressed: List[dict] = []
        self._suppressed_prepared = False
        # Telegram delivery runs on a worker thread. requests.post blocks for
        # its full timeout, and process_decision() is reached from app.py's 1s
        # UI timer — a slow Telegram response on that path stalls the event
        # loop, freezing the dashboard and pausing bar advancement. The CSV
        # write in _store() stays synchronous: it is fast local append I/O and
        # callers rely on an alert being readable back immediately.
        self._delivery_queue: queue.Queue = queue.Queue()
        self._delivery_thread: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()

    def process_decision(self, decision: Decision, current_price: Optional[float],
                         bar_start=None, now: Optional[datetime] = None,
                         indicators=None) -> Optional[AlertRecord]:
        """bar_start comes from engine.snapshot() and must be the bar the
        evaluated candle came from, never five_minute_start(now) — see the
        comment on that key for what a clock-derived value silently breaks.

        `now` is the record timestamp. Defaulted so live callers are unchanged;
        replay passes simulated time, which is what makes its output
        reproducible byte for byte and therefore diffable against a reference.

        `indicators` is the IndicatorSnapshot the decision was evaluated against,
        carried onto the record for DISPLAY only — nothing here reads it, and no
        gate, grade or latch depends on it. Optional so every existing caller and
        test keeps working unchanged; when absent the message shows em-dashes
        rather than inventing values.
        """
        key = (decision.status.value, decision.direction.value, decision.grade.value)
        self._last_decision_key, previous_key = key, self._last_decision_key

        if decision.status != Status.ENTRY or decision.grade.value not in TRADING_GRADES:
            return None  # WAIT/Ignore transitions update dedupe state but never alert

        rank = ALERT_GRADE_RANK[decision.grade.value]
        stamp = now or datetime.now()
        latch_key = None
        if bar_start is None:
            # No bar to latch against — before the session's first bar. Fall
            # back to the old key so this path can't alert on every evaluation.
            if key == previous_key:
                return None
        else:
            latch_key = (bar_start, decision.direction.value)
            best = self._latch.get(latch_key)
            if best is not None and rank <= best:
                return None  # already alerted this bar at this grade or better

        # THE COOLDOWN SITS BETWEEN THE LATCH'S READ AND ITS WRITE, and the order
        # is load-bearing. Write the latch first and a withheld alert still raises
        # the bar's ceiling — so a genuine upgrade later in the same bar would be
        # compared against a grade that was never delivered, and silently dropped.
        # The latch must only ever record alerts that actually went out.
        if self._withheld_by_cooldown(decision, stamp, current_price):
            return None

        if latch_key is not None:
            self._latch[latch_key] = rank
            # Bound memory: once a bar is past, its ceiling can never be
            # consulted again. Keyed per direction, so a Long ceiling never
            # suppresses a Short in the same bar.
            self._evict_stale_latch(bar_start)

        ema10 = getattr(indicators, "ema10", None) if indicators else None
        ema10_prev = getattr(indicators, "ema10_prev", None) if indicators else None
        record = AlertRecord(
            id=str(uuid.uuid4()), timestamp=stamp, type="Trading",
            direction=decision.direction.value, grade=decision.grade.value,
            confidence=config.GRADE_CONFIDENCE.get(decision.grade.value),
            current_price=current_price, setup=getattr(decision, "setup", None),
            # reason_list is UNCHANGED. alert_log.csv is the analysis corpus and
            # every study run against it depends on this column; the message got
            # shorter, the record did not.
            reason_list=[f"{k.replace('_', ' ').title()}: {v}" for k, v in decision.signals.details.items()],
            vwap=getattr(indicators, "vwap", None) if indicators else None,
            ema10=ema10,
            # The same subtraction _evaluate_trend and measured_fields() both do.
            # None until the lookback fills, rather than 0.0, which would read as
            # a flat trend instead of an unknown one.
            ema_slope=(ema10 - ema10_prev)
                      if (ema10 is not None and ema10_prev is not None) else None,
            signals=decision.signals,
        )
        self._store(record)
        # Only DELIVERED alerts start a cooldown. A withheld one must not extend
        # its own window, or a burst of blocked alerts walks the deadline forward
        # and turns a 5-minute cooldown into an unbounded one.
        self._last_alert = record
        self._send_telegram(record)
        logger.info("Trading alert: %s", record)
        return record

    def process_error(self, message: str, current_price: Optional[float] = None,
                      now: Optional[datetime] = None) -> Optional[AlertRecord]:
        """Fires once per distinct error message — callers pass a stable
        string per condition (e.g. always the same text for 'feed
        disconnected') so this naturally de-dupes like process_decision."""
        if message == self._last_error_message:
            return None
        self._last_error_message = message

        record = AlertRecord(
            id=str(uuid.uuid4()), timestamp=now or datetime.now(), type="Error",
            direction=None, grade=None, confidence=None,
            current_price=current_price, reason_list=[message],
        )
        self._store(record)
        self._send_telegram(record)
        logger.warning("Error alert: %s", record)
        return record

    def _withheld_by_cooldown(self, decision, stamp: datetime,
                              current_price: Optional[float]) -> bool:
        """True if this alert reverses direction too soon after the last delivered
        one. Records what it withheld — never a silent drop."""
        if FLIP_COOLDOWN_MINUTES <= 0:
            return False

        last = self._last_alert
        # Reset on date change. Without this the first alert of a session is
        # measured against yesterday's close, which is both meaningless and, on a
        # Monday, a 72-hour "gap" that happens to work by accident.
        if last is not None and last.timestamp.date() != stamp.date():
            self._last_alert = last = None
        if last is None:
            return False

        # Flip scope: same-direction alerts are the latch's business, not the
        # cooldown's. Measured on the live log, widening this to every alert
        # withheld three more and caught no additional flips.
        if decision.direction.value == last.direction:
            return False
        if decision.grade.value in COOLDOWN_ESCAPING_GRADES:
            return False

        elapsed = stamp - last.timestamp
        if elapsed >= timedelta(minutes=FLIP_COOLDOWN_MINUTES):
            return False

        self._record_suppression(decision, stamp, current_price, last, elapsed)
        return True

    def _record_suppression(self, decision, stamp: datetime,
                            current_price: Optional[float], blocked_by: AlertRecord,
                            elapsed: timedelta) -> None:
        entry = {
            "timestamp": stamp.isoformat(), "direction": decision.direction.value,
            "grade": decision.grade.value, "setup": getattr(decision, "setup", None) or "",
            "current_price": current_price,
            "seconds_since_last": round(elapsed.total_seconds(), 3),
            "blocked_by_direction": blocked_by.direction,
            "blocked_by_grade": blocked_by.grade,
            "blocked_by_timestamp": blocked_by.timestamp.isoformat(),
            "engine_version": engine_version(),
        }
        self.suppressed.append(entry)
        logger.info("Flip cooldown withheld %s %s %.0fs after %s %s",
                    entry["grade"], entry["direction"], elapsed.total_seconds(),
                    blocked_by.grade, blocked_by.direction)

        if not self._suppressed_prepared:
            self._suppressed_prepared = True
            rotate_if_header_stale(config.SUPPRESSED_LOG_FILE, SUPPRESSED_LOG_FIELDS)
        exists = os.path.exists(config.SUPPRESSED_LOG_FILE)
        with open(config.SUPPRESSED_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SUPPRESSED_LOG_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(entry)

    def process_startup(self, tradingsymbol: Optional[str], seed_count: int,
                        expected_seed: int,
                        now: Optional[datetime] = None) -> AlertRecord:
        """Confirm a session came up, carrying its seed count as a NUMBER.

        A SEPARATE TYPE, NOT process_error. A "started fine" message tagged
        "Error" would pollute every later error query — and process_error's
        de-dupe keys on the message string, which would silently swallow a second
        startup whose sentence happened to match the first.

        TAKES STRUCTURED VALUES, NOT A SENTENCE. The Telegram wording is built at
        send time by telegram_format and never persisted, so alert_log.csv stores
        `seed_bars=36` rather than an emoji sentence someone has to parse. It also
        keeps the emoji out of a CSV entirely, rather than relying on every reader
        of that file choosing the right encoding.

        `reason_list` carries the same values as ASCII key=value text rather than
        a new CSV column, deliberately: ALERT_LOG_FIELDS is compared against the
        existing header by rotate_if_header_stale, so widening it would rename the
        VM's 57-row history out from under every study that reads alert_log.csv.

        Never latched and never de-duped: the caller decides when this fires.
        `_last_alert` is deliberately NOT touched, so a startup message cannot
        open a flip-cooldown window against the session's first real alert.
        """
        record = AlertRecord(
            id=str(uuid.uuid4()), timestamp=now or datetime.now(), type="Startup",
            direction=None, grade=None, confidence=None, current_price=None,
            tradingsymbol=tradingsymbol, seed_count=seed_count,
            expected_seed=expected_seed,
            reason_list=[f"Session started symbol={tradingsymbol or '?'} "
                         f"seed_bars={seed_count} expected_bars={expected_seed}"],
        )
        self._store(record)
        self._send_telegram(record)
        logger.info("Startup alert: %s", record.reason_list[0])
        return record

    def _evict_stale_latch(self, current_bar_start):
        for key in [k for k in self._latch if k[0] < current_bar_start]:
            del self._latch[key]

    def clear_error(self):
        """Call when the error condition resolves, so the same message
        can re-alert if it happens again later."""
        self._last_error_message = None

    def _store(self, record: AlertRecord):
        self.history.append(record)

        # Once per instance, not per row. app.py and main.py each build exactly
        # one engine, so this is effectively once per process — but keying it to
        # the instance is what makes the rotation testable.
        if not self._prepared:
            self._prepared = True
            rotate_if_header_stale(config.ALERT_LOG_FILE, ALERT_LOG_FIELDS)

        log_exists = False
        try:
            with open(config.ALERT_LOG_FILE, "r", encoding="utf-8"):
                log_exists = True
        except FileNotFoundError:
            pass
        with open(config.ALERT_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not log_exists:
                writer.writerow(ALERT_LOG_FIELDS)
            writer.writerow([
                record.id, record.timestamp.isoformat(), record.type, record.direction,
                record.grade, record.confidence, record.setup or "",
                record.current_price, " | ".join(record.reason_list),
                # This file is what alerting policy gets scored against, and the
                # latch changes alert counts by design. Rows written either side
                # of that boundary are indistinguishable without this.
                engine_version(),
            ])

    def _send_telegram(self, record: AlertRecord):
        """Hands the record to the delivery worker and returns immediately —
        never performs the HTTP call on the caller's thread."""
        if not self.enable_telegram:
            logger.debug("Telegram disabled on this engine — not delivering alert %s", record.id)
            return
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            logger.warning("Telegram not configured — skipping delivery for alert %s", record.id)
            return
        self._ensure_delivery_worker()
        self._delivery_queue.put(record)

    def _ensure_delivery_worker(self):
        """Started lazily, so an engine that never delivers (unconfigured
        Telegram, or a test run) never spawns a thread."""
        with self._worker_lock:
            if self._delivery_thread is None or not self._delivery_thread.is_alive():
                self._delivery_thread = threading.Thread(
                    target=self._delivery_worker, name="alert-telegram", daemon=True,
                )
                self._delivery_thread.start()

    def _delivery_worker(self):
        while True:
            record = self._delivery_queue.get()
            try:
                if record is _SHUTDOWN:
                    return
                self._deliver_telegram(record)
            finally:
                self._delivery_queue.task_done()

    def _deliver_telegram(self, record: AlertRecord):
        """The blocking HTTP call — worker thread only. Never raises: a
        delivery failure must not kill the worker and silence later alerts."""
        # Formatting lives in telegram_format.py, which engine_version() does NOT
        # hash — so the message can be reworded without moving the fingerprint or
        # forcing a VM re-baseline. Only this call is inside the hashed set.
        formatter = {"Trading": format_trading_alert,
                     "Startup": format_startup_alert}.get(record.type,
                                                          format_error_alert)
        text = formatter(record)
        try:
            requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
                timeout=5,
            )
        except Exception as e:
            logger.warning("Telegram delivery failed for alert %s: %s", record.id, e)

    def close(self, timeout: float = 5.0):
        """Drain queued deliveries and stop the worker. Wired to shutdown in
        app.py and main.py; the daemon flag means an engine that is never
        closed still won't hold up interpreter exit."""
        with self._worker_lock:
            thread = self._delivery_thread
        if thread is None or not thread.is_alive():
            return
        self._delivery_queue.put(_SHUTDOWN)
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("Telegram delivery worker did not stop within %.0fs", timeout)

    def send_test_telegram_message(self) -> bool:
        """Verifies TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID actually work.
        Deliberately bypasses AlertRecord/history/CSV entirely — this is
        a config connectivity check, not a real trading alert, so it
        must never show up in Alert History. Returns False (never
        raises) so the dashboard can show a plain pass/fail.

        Also bypasses the delivery queue on purpose: the caller needs the
        pass/fail synchronously, and app.py already runs it off the event
        loop via run.io_bound."""
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            logger.warning("Telegram not configured — cannot send test message")
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": config.TELEGRAM_CHAT_ID,
                      "text": "Hi Venky! Telegram delivery is configured correctly. You will recieve trading alerts here."},
                timeout=5,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Telegram test message failed: %s", e)
            return False


def read_recent_alerts(n: int = 20) -> List[dict]:
    """Returns the last n rows from the alert CSV log, newest first.
    Empty list if the log doesn't exist yet — mirrors engine.read_recent_log."""
    try:
        with open(config.ALERT_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return list(reversed(rows[-n:]))
