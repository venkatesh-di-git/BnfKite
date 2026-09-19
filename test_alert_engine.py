"""
Sanity tests for alert_engine.py: the duplicate-alert rule (using the
spec's own WAIT->A+->A+->WAIT->A+ example), confidence mapping, Error
Alert dedupe, and CSV round-tripping.

Redirects config.ALERT_LOG_FILE to a scratch path and blanks the
Telegram settings so runs are isolated and never hit the network.
"""

import csv
import glob
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta

import config

config.ALERT_LOG_FILE = os.path.join(tempfile.gettempdir(), "bnf_kite_test_alert_log.csv")
# The flip cooldown writes withheld alerts to their own log. Redirected for the
# same reason as ALERT_LOG_FILE: left alone, a test run appends into the real
# corpus, which is exactly how five stub rows once landed in csv/signal_log.csv.
config.SUPPRESSED_LOG_FILE = os.path.join(tempfile.gettempdir(),
                                          "bnf_kite_test_suppressed_log.csv")
config.TELEGRAM_BOT_TOKEN = ""
config.TELEGRAM_CHAT_ID = ""
for _path in (config.ALERT_LOG_FILE, config.SUPPRESSED_LOG_FILE):
    if os.path.exists(_path):
        os.remove(_path)

import alert_engine  # module handle, so requests.post can be stubbed on it
from alert_engine import AlertEngine, read_recent_alerts
from decision_engine import Decision, Direction, Grade, Status
from engine import engine_version, rotate_if_header_stale
from signal_engine import (
    LevelState, OpenInterestState, PullbackState, RejectionState,
    SignalSnapshot, TrendState, VolumeState,
)


def make_decision(status, direction, grade, details=None):
    snapshot = SignalSnapshot(
        trend=TrendState.BULLISH, pullback=PullbackState.BULLISH, rejection=RejectionState.BULLISH,
        volume=VolumeState.HIGH, open_interest=OpenInterestState.RISING,
        poc=LevelState.AT, vah=LevelState.BELOW, val=LevelState.ABOVE,
        details=details or {"trend": "Close above VWAP"},
    )
    return Decision(direction=direction, status=status, grade=grade, signals=snapshot)


def test_dedupe_sequence_matches_spec_example():
    """WAIT -> A+ (alert) -> A+ (no alert) -> WAIT (no alert) -> A+ (alert) —
    exactly the spec's own example, 2 alerts total."""
    engine = AlertEngine()
    wait = make_decision(Status.WAIT, Direction.NEUTRAL, Grade.IGNORE)
    a_plus_long = make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS)

    assert engine.process_decision(wait, 100) is None
    assert engine.process_decision(a_plus_long, 100) is not None
    assert engine.process_decision(a_plus_long, 101) is None
    assert engine.process_decision(wait, 101) is None
    assert engine.process_decision(a_plus_long, 102) is not None
    assert len(engine.history) == 2
    print("PASS: dedupe sequence matches spec's WAIT->A+->A+->WAIT->A+ example\n")


def test_direction_change_re_alerts_without_going_through_wait():
    engine = AlertEngine()
    long_a = make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS)
    short_a = make_decision(Status.ENTRY, Direction.SHORT, Grade.A_PLUS)
    assert engine.process_decision(long_a, 100) is not None
    assert engine.process_decision(short_a, 100) is not None
    print("PASS: Long->Short is a state change, alerts again\n")


def test_wait_and_ignore_never_alert():
    engine = AlertEngine()
    wait = make_decision(Status.WAIT, Direction.NEUTRAL, Grade.IGNORE)
    assert engine.process_decision(wait, 100) is None
    assert engine.history == []
    print("PASS: WAIT/Ignore never produces an alert\n")


def test_confidence_mapping():
    engine = AlertEngine()
    for grade, expected in [(Grade.A_PLUS, config.GRADE_CONFIDENCE["A+"]),
                             (Grade.A, config.GRADE_CONFIDENCE["A"]),
                             (Grade.B, config.GRADE_CONFIDENCE["B"])]:
        engine2 = AlertEngine()  # fresh engine so each grade counts as a state change
        record = engine2.process_decision(make_decision(Status.ENTRY, Direction.LONG, grade), 100)
        assert record.confidence == expected, f"{grade} -> {record.confidence}, expected {expected}"
    print("PASS: grade -> confidence mapping\n")


def test_reason_list_built_from_signal_details():
    details = {"trend": "Close above VWAP", "volume": "Relative volume 1.5x"}
    decision = make_decision(Status.ENTRY, Direction.LONG, Grade.A, details=details)
    record = AlertEngine().process_decision(decision, 100)
    assert "Trend: Close above VWAP" in record.reason_list
    assert "Volume: Relative volume 1.5x" in record.reason_list
    print("PASS: reason_list derived from SignalSnapshot.details\n")


def test_error_alert_dedupe_and_clear():
    engine = AlertEngine()
    assert engine.process_error("WebSocket disconnected") is not None
    assert engine.process_error("WebSocket disconnected") is None  # same message, no repeat
    engine.clear_error()
    assert engine.process_error("WebSocket disconnected") is not None  # re-armed after clear
    print("PASS: error alert dedupe + clear_error re-arming\n")


def test_telegram_noop_when_unconfigured():
    """TELEGRAM_BOT_TOKEN/CHAT_ID are blanked at the top of this file —
    process_decision must not raise even though delivery is skipped."""
    engine = AlertEngine()
    record = engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100)
    assert record is not None
    print("PASS: Telegram delivery no-ops safely when unconfigured\n")


def test_read_recent_alerts_round_trips_through_csv():
    engine = AlertEngine()
    # Timestamps are explicit and an hour apart because the second alert is a
    # B-grade direction flip — exactly what the flip cooldown withholds. This
    # test is about CSV round-tripping, so it must not accidentally depend on
    # cooldown behaviour in either direction.
    t = datetime(2026, 8, 14, 10, 0, 0)
    engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100, now=t)
    engine.process_decision(make_decision(Status.WAIT, Direction.NEUTRAL, Grade.IGNORE), 100, now=t)
    engine.process_decision(make_decision(Status.ENTRY, Direction.SHORT, Grade.B), 99,
                            now=t + timedelta(hours=1))
    rows = read_recent_alerts(20)
    assert len(rows) >= 2
    assert rows[0]["direction"] == "Short"  # newest first
    print("PASS: read_recent_alerts round-trips through the CSV log, newest first\n")


def _with_telegram_configured(fn):
    """Temporarily un-blank the Telegram settings this module clears at import,
    so the delivery path is exercised. requests.post is always stubbed by the
    caller — no test ever reaches the network."""
    config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID = "test-token", "test-chat"
    try:
        fn()
    finally:
        config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID = "", ""


def test_process_decision_does_not_block_on_slow_telegram():
    """The regression: requests.post used to run inline, so a slow Telegram
    response stalled app.py's 1s timer — and with it the event loop."""
    def body():
        posted = threading.Event()

        def slow_post(*args, **kwargs):
            posted.set()
            time.sleep(3)
            raise AssertionError("should never be awaited by the caller")

        original_post, alert_engine.requests.post = alert_engine.requests.post, slow_post
        engine = AlertEngine()
        try:
            started = time.monotonic()
            record = engine.process_decision(
                make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100)
            elapsed = time.monotonic() - started

            assert record is not None
            assert elapsed < 0.5, f"process_decision blocked for {elapsed:.2f}s"
            # The record is in history and on disk immediately — only the
            # network call was deferred.
            assert len(engine.history) == 1
            assert any(r["id"] == record.id for r in read_recent_alerts(20))
            assert posted.wait(timeout=5), "delivery worker never ran"
        finally:
            alert_engine.requests.post = original_post

    _with_telegram_configured(body)
    print("PASS: a slow Telegram response never blocks process_decision\n")


def test_queued_message_is_delivered_and_survives_failure():
    """Delivery happens on the worker, and one failure must not kill it —
    otherwise a single network blip would silence every later alert."""
    def body():
        calls = []
        delivered = threading.Event()

        def flaky_post(*args, **kwargs):
            calls.append(kwargs.get("json", {}).get("text", ""))
            if len(calls) == 1:
                raise ConnectionError("simulated network blip")
            delivered.set()
            return None

        original_post, alert_engine.requests.post = alert_engine.requests.post, flaky_post
        engine = AlertEngine()
        try:
            # An hour apart: the second is a B-grade flip, and this test needs it
            # DELIVERED to prove the worker survived the first failure.
            t = datetime(2026, 8, 14, 10, 0, 0)
            engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS),
                                    100, now=t)
            engine.process_decision(make_decision(Status.ENTRY, Direction.SHORT, Grade.B),
                                    99, now=t + timedelta(hours=1))
            assert delivered.wait(timeout=5), "worker died after the first failure"
            assert len(calls) == 2
            engine.close(timeout=5)
            assert engine._delivery_thread is not None and not engine._delivery_thread.is_alive()
        finally:
            alert_engine.requests.post = original_post

    _with_telegram_configured(body)
    print("PASS: queued messages deliver on the worker; a failure doesn't kill it\n")


def test_no_worker_thread_when_telegram_unconfigured():
    """Token/chat are blank here, so nothing should be queued or spawned."""
    engine = AlertEngine()
    engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100)
    assert engine._delivery_thread is None
    assert engine._delivery_queue.empty()
    print("PASS: no delivery thread is spawned when Telegram is unconfigured\n")


# --- the once_per_bar latch -------------------------------------------------
#
# Everything above passes bar_start=None and therefore exercises the FALLBACK
# path. These pass a real bar and exercise the latch, which is the authority
# whenever a bar is available.

BAR = datetime(2026, 8, 6, 10, 15)
NEXT_BAR = datetime(2026, 8, 6, 10, 20)


def test_same_bar_same_grade_repeatedly_alerts_once():
    engine = AlertEngine()
    entry = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    assert engine.process_decision(entry, 100, bar_start=BAR) is not None
    for _ in range(6):
        assert engine.process_decision(entry, 100, bar_start=BAR) is None
    assert len(engine.history) == 1
    print("PASS: repeated identical ENTRYs in one bar produce a single alert\n")


def test_grade_upgrade_re_fires_within_the_same_bar():
    """B -> A -> A+ is the observed trajectory in 7 of 8 multi-alert bars. A
    strict latch would hand back B on a bar that became A+."""
    engine = AlertEngine()
    for grade in (Grade.B, Grade.A, Grade.A_PLUS):
        decision = make_decision(Status.ENTRY, Direction.SHORT, grade)
        assert engine.process_decision(decision, 100, bar_start=BAR) is not None, grade
    assert [r.grade for r in engine.history] == ["B", "A", "A+"]
    print("PASS: B -> A -> A+ re-fires on each upgrade, 3 alerts\n")


def test_downgrade_does_not_re_fire():
    """The ceiling only ratchets upward — this is what bounds the bar at 3."""
    engine = AlertEngine()
    a_plus = make_decision(Status.ENTRY, Direction.SHORT, Grade.A_PLUS)
    a = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    assert engine.process_decision(a_plus, 100, bar_start=BAR) is not None
    assert engine.process_decision(a, 100, bar_start=BAR) is None
    assert len(engine.history) == 1
    print("PASS: A+ -> A does not re-fire\n")


def test_wait_between_identical_entries_does_not_re_arm():
    """THE bug this phase exists to fix. Pullback/Rejection flip on every tick
    near EMA10; each flip produced a WAIT, which was a different key, which
    cleared the old guard and re-armed the next ENTRY."""
    engine = AlertEngine()
    entry = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    wait = make_decision(Status.WAIT, Direction.NEUTRAL, Grade.IGNORE)

    assert engine.process_decision(entry, 100, bar_start=BAR) is not None
    assert engine.process_decision(wait, 100, bar_start=BAR) is None
    assert engine.process_decision(entry, 100, bar_start=BAR) is None
    assert len(engine.history) == 1
    print("PASS: a WAIT between two identical ENTRYs no longer re-arms\n")


def test_ceiling_is_per_direction():
    """A Long A+ must not suppress a Short A in the same bar — they are
    different setups that happen to share a five-minute window."""
    engine = AlertEngine()
    long_a_plus = make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS)
    short_a = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    assert engine.process_decision(long_a_plus, 100, bar_start=BAR) is not None
    assert engine.process_decision(short_a, 100, bar_start=BAR) is not None
    # ...and each keeps its own independent ceiling
    assert engine.process_decision(short_a, 100, bar_start=BAR) is None
    assert len(engine.history) == 2
    print("PASS: latch ceiling is per direction, not per bar\n")


def test_new_bar_re_fires_same_direction_and_grade():
    """Guards the reason the latch had to REPLACE the old key rather than sit
    behind it: (ENTRY, Short, A) is the same 3-tuple on both bars, so the old
    guard would have swallowed the second one."""
    engine = AlertEngine()
    entry = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    assert engine.process_decision(entry, 100, bar_start=BAR) is not None
    assert engine.process_decision(entry, 100, bar_start=NEXT_BAR) is not None
    assert len(engine.history) == 2
    print("PASS: a new bar re-fires the same direction and grade\n")


def test_missing_bar_start_falls_back_to_last_decision_key():
    """Before the session's first bar there is nothing to latch against.
    Without the fallback this path would alert on every 2s evaluation."""
    engine = AlertEngine()
    entry = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    assert engine.process_decision(entry, 100, bar_start=None) is not None
    assert engine.process_decision(entry, 100, bar_start=None) is None
    assert engine.process_decision(entry, 100, bar_start=None) is None
    assert len(engine.history) == 1
    assert engine._latch == {}, "the None path must not populate the latch"
    print("PASS: bar_start=None falls back to the old key, does not storm\n")


def test_error_alerts_are_exempt_from_the_latch():
    engine = AlertEngine()
    entry = make_decision(Status.ENTRY, Direction.SHORT, Grade.A_PLUS)
    engine.process_decision(entry, 100, bar_start=BAR)
    assert engine.process_error("WebSocket disconnected") is not None
    assert engine._latch == {(BAR, "Short"): 3}, "error alerts must not touch the latch"
    # ...and the trading latch is still intact afterwards
    assert engine.process_decision(entry, 100, bar_start=BAR) is None
    print("PASS: error alerts bypass the latch and never populate it\n")


def test_latch_evicts_bars_that_are_past():
    engine = AlertEngine()
    entry = make_decision(Status.ENTRY, Direction.SHORT, Grade.A)
    engine.process_decision(entry, 100, bar_start=BAR)
    engine.process_decision(entry, 100, bar_start=NEXT_BAR)
    assert list(engine._latch) == [(NEXT_BAR, "Short")]
    print("PASS: latch keys for past bars are evicted\n")


def test_every_alertable_grade_has_a_rank():
    """Catches a Grade added to the enum without a rank — which would make it
    silently unalertable, since ALERT_GRADE_RANK is also the alertable set."""
    for grade in Grade:
        if grade is Grade.IGNORE:
            assert grade.value not in alert_engine.ALERT_GRADE_RANK
        else:
            assert grade.value in alert_engine.ALERT_GRADE_RANK, grade
    assert alert_engine.TRADING_GRADES == tuple(alert_engine.ALERT_GRADE_RANK)
    print("PASS: every alertable Grade has a rank; Ignore has none\n")


def test_archived_alert_log_replayed_through_the_latch_yields_15():
    """The 32 -> 15 claim the policy was chosen on, checked against the real
    file rather than restated. Skips if the archive isn't present."""
    archive = _find_archived_alert_log()
    if archive is None:
        print("SKIP: no archived alert_log found\n")
        return

    with open(archive, "r", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["type"] == "Trading"]
    assert len(rows) == 32, f"expected the 32-alert corpus, got {len(rows)}"

    latch, fired = {}, 0
    for row in rows:
        stamp = datetime.fromisoformat(row["timestamp"])
        bar = stamp.replace(minute=stamp.minute - stamp.minute % 5, second=0, microsecond=0)
        key = (bar, row["direction"])
        rank = alert_engine.ALERT_GRADE_RANK[row["grade"]]
        if latch.get(key) is None or rank > latch[key]:
            latch[key] = rank
            fired += 1
    assert fired == 15, f"latch would have fired {fired}, expected 15"
    print(f"PASS: archived corpus 32 -> {fired} under the latch\n")


def _find_archived_alert_log():
    """The live alert log, wherever it ended up after this phase's rotation.
    config.ALERT_LOG_FILE is redirected to a temp path at import, so this
    looks in the real csv/ directory instead."""
    csv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csv")
    candidates = sorted(glob.glob(os.path.join(csv_dir, "alert_log*.csv")))
    for path in candidates:
        with open(path, "r", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("type") == "Trading"]
        if len(rows) == 32:
            return path
    return None


# --- log rotation and provenance --------------------------------------------

def test_stale_alert_log_header_is_rotated_not_appended_to():
    scratch = os.path.join(tempfile.mkdtemp(), "alert_log.csv")
    with open(scratch, "w", newline="") as f:
        csv.writer(f).writerow(["id", "timestamp", "type", "direction",
                                "grade", "confidence", "current_price", "reason_list"])
        csv.writer(f).writerow(["old-id"] + [""] * 7)

    original, config.ALERT_LOG_FILE = config.ALERT_LOG_FILE, scratch
    try:
        AlertEngine().process_decision(
            make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100, bar_start=BAR)
        with open(scratch, "r", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == alert_engine.ALERT_LOG_FIELDS, "new file must carry the new header"
        assert len(rows) == 2, "the old row must not have been appended to"
        archived = glob.glob(os.path.join(os.path.dirname(scratch), "alert_log.*.csv"))
        assert len(archived) == 1, "the old log must be preserved, not deleted"
    finally:
        config.ALERT_LOG_FILE = original
    print("PASS: a stale alert-log header is rotated, not appended to\n")


def test_rotate_is_noop_when_header_matches_as_a_tuple():
    """csv.reader always yields a list. Comparing it against a tuple of the
    same strings is False, so a bare `header == fields` would rotate the log on
    EVERY process start — silently starting a fresh empty file each launch."""
    scratch = os.path.join(tempfile.mkdtemp(), "some_log.csv")
    fields = ("a", "b", "c")
    with open(scratch, "w", newline="") as f:
        csv.writer(f).writerow(fields)

    rotate_if_header_stale(scratch, fields)
    assert os.path.exists(scratch)
    assert glob.glob(os.path.join(os.path.dirname(scratch), "some_log.*.csv")) == []
    print("PASS: rotate_if_header_stale is a no-op for a matching tuple header\n")


def test_alert_rows_carry_engine_version():
    engine = AlertEngine()
    engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS),
                            100, bar_start=BAR)
    row = read_recent_alerts(1)[0]
    assert row["engine_version"] == engine_version()
    assert row["engine_version"]
    print("PASS: every alert row carries a non-empty engine_version\n")


def test_alert_row_records_the_setup():
    engine = AlertEngine()
    decision = make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS)
    decision.setup = "vwap"
    record = engine.process_decision(decision, 100, bar_start=BAR)
    assert record.setup == "vwap"
    assert read_recent_alerts(1)[0]["setup"] == "vwap"
    print("PASS: the alert row records which setup fired\n")


def test_both_setups_in_one_bar_share_the_latch_ceiling():
    """A deliberate choice: the latch keys on (bar_start, direction) only, so
    the bound stays 3 per bar per direction rather than 6. The consequence is
    that the second setup's alert is suppressed unless it upgrades the grade —
    signal_log.csv keeps the full picture for analysis."""
    engine = AlertEngine()
    ema = make_decision(Status.ENTRY, Direction.LONG, Grade.A)
    ema.setup = "ema"
    vwap = make_decision(Status.ENTRY, Direction.LONG, Grade.A)
    vwap.setup = "vwap"

    assert engine.process_decision(ema, 100, bar_start=BAR) is not None
    assert engine.process_decision(vwap, 100, bar_start=BAR) is None
    # ...but a genuine upgrade from the other setup still gets through
    vwap_better = make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS)
    vwap_better.setup = "vwap"
    assert engine.process_decision(vwap_better, 100, bar_start=BAR) is not None
    assert len(engine.history) == 2
    print("PASS: the two setups share one latch ceiling per bar and direction\n")


# --- replay prerequisites: injected clock, explicit Telegram switch ----------

def test_injected_now_is_used_verbatim():
    """Replay must produce byte-identical output across runs, so the record
    timestamp cannot come from the wall clock."""
    engine = AlertEngine()
    stamp = datetime(2026, 8, 3, 15, 10, 22, 82906)
    record = engine.process_decision(
        make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100,
        bar_start=BAR, now=stamp)
    assert record.timestamp == stamp
    assert read_recent_alerts(1)[0]["timestamp"] == stamp.isoformat()
    print("PASS: an injected now is used verbatim in the record and the CSV\n")


def test_injected_now_applies_to_error_alerts_too():
    engine = AlertEngine()
    stamp = datetime(2026, 8, 3, 15, 40, 52)
    record = engine.process_error("WebSocket disconnected", now=stamp)
    assert record.timestamp == stamp
    print("PASS: an injected now applies to error alerts too\n")


def test_omitting_now_still_uses_the_wall_clock():
    """Live callers pass nothing and must be unaffected."""
    before = datetime.now()
    record = AlertEngine().process_decision(
        make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS), 100, bar_start=BAR)
    assert before <= record.timestamp <= datetime.now()
    print("PASS: omitting now falls back to the wall clock\n")


def test_telegram_disabled_never_enqueues():
    """A blank token is not a guarantee — a replay run on a configured machine
    would otherwise fire real alerts for a session that ended weeks ago."""
    def body():
        engine = AlertEngine(enable_telegram=False)
        engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS),
                                100, bar_start=BAR)
        assert engine._delivery_thread is None
        assert engine._delivery_queue.empty()
        # ...and the alert itself is still recorded, just not delivered.
        assert len(engine.history) == 1
    _with_telegram_configured(body)
    print("PASS: enable_telegram=False records but never enqueues\n")


def test_telegram_enabled_by_default_for_live_callers():
    def body():
        engine = AlertEngine()
        assert engine.enable_telegram is True
        engine.process_decision(make_decision(Status.ENTRY, Direction.LONG, Grade.A_PLUS),
                                100, bar_start=BAR)
        assert engine._delivery_thread is not None
        engine.close(timeout=5)
    original, alert_engine.requests.post = alert_engine.requests.post, lambda *a, **k: None
    try:
        _with_telegram_configured(body)
    finally:
        alert_engine.requests.post = original
    print("PASS: Telegram stays enabled by default for live callers\n")


def test_engine_version_covers_alert_engine():
    """This phase changes what is DELIVERED. Without alert_engine.py in the
    hash, two runs with radically different alert behaviour would report an
    identical version string."""
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_engine.py")
    with open(source, "rb") as f:
        original = f.read()
    before = engine_version()
    try:
        with open(source, "ab") as f:
            f.write(b"\n# version-hash probe\n")
        assert engine_version() != before
    finally:
        with open(source, "wb") as f:
            f.write(original)
    assert engine_version() == before
    print("PASS: engine_version() changes when alert_engine.py changes\n")


if __name__ == "__main__":
    test_dedupe_sequence_matches_spec_example()
    test_direction_change_re_alerts_without_going_through_wait()
    test_wait_and_ignore_never_alert()
    test_confidence_mapping()
    test_reason_list_built_from_signal_details()
    test_error_alert_dedupe_and_clear()
    test_telegram_noop_when_unconfigured()
    test_read_recent_alerts_round_trips_through_csv()
    test_process_decision_does_not_block_on_slow_telegram()
    test_queued_message_is_delivered_and_survives_failure()
    test_no_worker_thread_when_telegram_unconfigured()
    test_same_bar_same_grade_repeatedly_alerts_once()
    test_grade_upgrade_re_fires_within_the_same_bar()
    test_downgrade_does_not_re_fire()
    test_wait_between_identical_entries_does_not_re_arm()
    test_ceiling_is_per_direction()
    test_new_bar_re_fires_same_direction_and_grade()
    test_missing_bar_start_falls_back_to_last_decision_key()
    test_error_alerts_are_exempt_from_the_latch()
    test_latch_evicts_bars_that_are_past()
    test_every_alertable_grade_has_a_rank()
    test_archived_alert_log_replayed_through_the_latch_yields_15()
    test_stale_alert_log_header_is_rotated_not_appended_to()
    test_rotate_is_noop_when_header_matches_as_a_tuple()
    test_alert_rows_carry_engine_version()
    test_alert_row_records_the_setup()
    test_both_setups_in_one_bar_share_the_latch_ceiling()
    test_injected_now_is_used_verbatim()
    test_injected_now_applies_to_error_alerts_too()
    test_omitting_now_still_uses_the_wall_clock()
    test_telegram_disabled_never_enqueues()
    test_telegram_enabled_by_default_for_live_callers()
    test_engine_version_covers_alert_engine()
    print("All tests passed.")
