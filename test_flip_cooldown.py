"""
Tests for the flip cooldown in alert_engine.py.

Imports from test_alert_engine so the log-file redirection at that module's import
applies here too — without it these tests would append into the real corpus, which
is how five stub rows once landed in csv/signal_log.csv.

The two that matter most are the ones guarding failures that produce no error:
a withheld alert must not extend its own window, and must not raise the bar latch.
"""

import csv
from datetime import datetime, timedelta

import pytest

import alert_engine
import config
from alert_engine import AlertEngine, read_recent_alerts
from decision_engine import Direction, Grade, Status
from engine import engine_version
from test_alert_engine import make_decision   # also applies the log redirection

T = datetime(2026, 8, 14, 10, 0, 0)


def entry(direction, grade):
    return make_decision(Status.ENTRY, direction, grade)


@pytest.fixture
def engine():
    e = AlertEngine()
    e.suppressed.clear()
    return e


def test_the_shipped_constants_are_the_re_derived_ones():
    """5 minutes, not 12: the eight B-grade flip gaps in the live log are 1, 2, 2,
    10, 11, 12, 17, 29, so every value from 3 to 10 minutes behaves identically
    while 12 sits one minute past a step edge."""
    assert alert_engine.FLIP_COOLDOWN_MINUTES == 5
    assert alert_engine.COOLDOWN_ESCAPING_GRADES == ("A", "A+")


def test_flip_inside_the_cooldown_is_withheld(engine):
    assert engine.process_decision(entry(Direction.LONG, Grade.A), 100, now=T) is not None
    assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                                   now=T + timedelta(minutes=1)) is None
    assert len(engine.history) == 1


def test_the_boundary_is_exclusive(engine):
    engine.process_decision(entry(Direction.LONG, Grade.A), 100, now=T)
    at_edge = T + timedelta(minutes=alert_engine.FLIP_COOLDOWN_MINUTES)
    assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                                   now=at_edge) is not None


@pytest.mark.parametrize("grade", [Grade.A, Grade.A_PLUS])
def test_escaping_grades_ignore_the_cooldown(grade):
    """A and A+ are exempt by design — the best signals are the ones least worth
    withholding. It is also why the mechanism catches only 4 of 18 live flips."""
    e = AlertEngine()
    e.process_decision(entry(Direction.LONG, Grade.A), 100, now=T)
    assert e.process_decision(entry(Direction.SHORT, grade), 99,
                              now=T + timedelta(seconds=30)) is not None


def test_same_direction_alerts_are_not_the_cooldowns_business(engine):
    """Flip scope. Same-direction repeats are the latch's job; widening this to
    every alert withheld three more on the live log and caught no extra flips."""
    engine.process_decision(entry(Direction.LONG, Grade.A), 100, bar_start=T, now=T)
    assert engine.process_decision(entry(Direction.LONG, Grade.A_PLUS), 101,
                                   bar_start=T, now=T + timedelta(seconds=30)) is not None


def test_a_withheld_alert_does_not_extend_the_window(engine):
    """If the cooldown restarted on a withheld alert, a burst would walk the
    deadline forward and turn 5 minutes into an unbounded block.

    bar_start is passed explicitly so the LATCH path is exercised. Left as None,
    every repeat of the same (status, direction, grade) is stopped by the legacy
    dedupe key before the cooldown is ever consulted, and the test proves nothing.
    """
    def bar(t):
        return t.replace(minute=t.minute // 5 * 5, second=0, microsecond=0)

    engine.process_decision(entry(Direction.LONG, Grade.A), 100,
                            bar_start=bar(T), now=T)
    for minute in (1, 2, 3, 4):
        when = T + timedelta(minutes=minute)
        assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                                       bar_start=bar(when), now=when) is None
    assert len(engine.suppressed) == 4

    # Five minutes after the DELIVERED alert, not after the last withheld one.
    when = T + timedelta(minutes=5)
    assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                                   bar_start=bar(when), now=when) is not None


def test_a_withheld_alert_does_not_raise_the_bar_latch(engine):
    """Why the cooldown check sits between the latch's read and its write. If a
    withheld B raised the bar's ceiling, the A+ following in the same bar would be
    compared against a grade that never went out, and silently dropped."""
    engine.process_decision(entry(Direction.LONG, Grade.A), 100, bar_start=T, now=T)
    assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99, bar_start=T,
                                   now=T + timedelta(seconds=30)) is None
    upgraded = engine.process_decision(entry(Direction.SHORT, Grade.A_PLUS), 98,
                                       bar_start=T, now=T + timedelta(seconds=40))
    assert upgraded is not None, "a withheld alert poisoned the bar's latch"


def test_cooldown_state_resets_on_date_change(engine):
    """Otherwise a session's first alert is measured against yesterday's close —
    meaningless, and on a Monday a 72-hour gap that only works by accident."""
    engine.process_decision(entry(Direction.LONG, Grade.A), 100,
                            now=datetime(2026, 8, 13, 15, 20, 0))
    assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                                   now=datetime(2026, 8, 14, 9, 20, 0)) is not None


def test_withheld_alerts_are_recorded_not_silently_dropped(engine):
    """The cooldown ships without evidence that suppressing helps, so the record of
    what it withheld IS the evidence collection."""
    engine.process_decision(entry(Direction.LONG, Grade.A), 100, now=T)
    engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                            now=T + timedelta(seconds=90))
    assert len(engine.suppressed) == 1
    e = engine.suppressed[0]
    assert (e["grade"], e["direction"]) == ("B", "Short")
    assert e["seconds_since_last"] == 90.0
    assert (e["blocked_by_grade"], e["blocked_by_direction"]) == ("A", "Long")
    assert e["engine_version"] == engine_version()

    with open(config.SUPPRESSED_LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows and rows[-1]["grade"] == "B"


def test_withheld_alerts_never_reach_the_alert_log(engine):
    """suppressed_log.csv is separate on purpose: alert_log.csv is what alerting
    policy gets scored against, and undelivered rows would change every count ever
    taken from it."""
    delivered = engine.process_decision(entry(Direction.LONG, Grade.A), 100,
                                        now=datetime(2026, 8, 14, 11, 0, 0))
    engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                            now=datetime(2026, 8, 14, 11, 0, 30))
    assert delivered.id in {r["id"] for r in read_recent_alerts(50)}
    assert len(engine.history) == 1


def test_setting_the_cooldown_to_zero_restores_pre_cooldown_behaviour(monkeypatch,
                                                                      engine):
    """The escape hatch. Behaviour matches the pre-cooldown engine exactly; only the
    source hash differs, so a rollback needs no code edit."""
    monkeypatch.setattr(alert_engine, "FLIP_COOLDOWN_MINUTES", 0)
    engine.process_decision(entry(Direction.LONG, Grade.A), 100, now=T)
    assert engine.process_decision(entry(Direction.SHORT, Grade.B), 99,
                                   now=T + timedelta(seconds=1)) is not None
    assert engine.suppressed == []


def test_the_live_log_would_have_lost_three_alerts(engine):
    """End-to-end against the real 11-14 Aug log, driven through the engine's own
    rule rather than the study's re-implementation of it. Three, not the four the
    12-minute proposal would have withheld."""
    from replay.cooldown_study import apply_cooldown, load_corpus
    assert len(apply_cooldown(load_corpus(), alert_engine.FLIP_COOLDOWN_MINUTES, "flip",
                              alert_engine.COOLDOWN_ESCAPING_GRADES)) == 3


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
