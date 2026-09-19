"""
Tests for replay/cooldown_study.py.

This file decides whether the flip cooldown ships as specified, so the rule it
models has to be the rule that will be built. The subtle one is that a suppressed
alert must NOT advance the cooldown window — otherwise a burst of blocked alerts
silently extends the suppression far beyond the configured duration.
"""

from datetime import datetime, timedelta

import pytest

from replay.cooldown_study import (ESCAPING_GRADES, apply_cooldown, flips,
                                   load, load_corpus)

T0 = datetime(2026, 8, 14, 9, 20, 0)


def a(minutes=0, direction="Long", grade="B", day="2026-08-14", seconds=0):
    return {"t": T0 + timedelta(minutes=minutes, seconds=seconds), "day": day,
            "direction": direction, "grade": grade, "setup": "vwap",
            "price": "57800.0", "version": "b3620733"}


# --------------------------------------------------------------------- flips

def test_flip_is_a_direction_change_within_a_day():
    assert len(flips([a(0, "Long"), a(1, "Short"), a(2, "Short"), a(3, "Long")])) == 2


def test_the_first_alert_of_a_day_is_never_a_flip():
    """Otherwise every session opens with a phantom flip against yesterday."""
    assert flips([a(0, "Long", day="2026-08-13"), a(1, "Short", day="2026-08-14")]) == []


def test_the_live_log_has_eighteen_flips_across_fifty_six_alerts():
    """Regression on the corrected figures. The log holds 57 rows, one of which is
    an Error (13 Aug's token failure) with an empty direction — counting it as an
    alert gives 57 and turns its blank direction into a 19th flip.

    load_corpus() pins 11-14 Aug. Using load() here would make this test fail the
    first time a new session lands — for the one reason that is not a regression."""
    alerts = load_corpus()
    assert len(alerts) == 56
    assert len(flips(alerts)) == 18
    assert all(x["direction"] in ("Long", "Short") for x in alerts)


# ----------------------------------------------------------------- the rule

def test_an_alert_inside_the_window_is_suppressed():
    assert len(apply_cooldown([a(0, "Long"), a(3, "Short")], 12, "flip")) == 1


def test_an_alert_outside_the_window_is_delivered():
    assert apply_cooldown([a(0, "Long"), a(13, "Short")], 12, "flip") == []


def test_the_boundary_is_exclusive():
    """Exactly at the window edge the alert is delivered, so a 12m cooldown means
    'less than 12m', not '12m or less'."""
    assert apply_cooldown([a(0, "Long"), a(12, "Short")], 12, "flip") == []
    assert len(apply_cooldown([a(0, "Long"), a(11, "Short", seconds=59)], 12, "flip")) == 1


def test_escaping_grades_are_never_suppressed():
    for grade in ESCAPING_GRADES:
        assert apply_cooldown([a(0, "Long"), a(1, "Short", grade=grade)],
                              12, "flip") == []


def test_a_suppressed_alert_does_not_advance_the_window():
    """The load-bearing detail. If `last` advanced on suppression, three alerts two
    minutes apart would extend the block to 12m from the LAST one — turning a 12m
    cooldown into an unbounded one during a burst."""
    alerts = [a(0, "Long"), a(2, "Short"), a(4, "Short"), a(13, "Short")]
    suppressed = apply_cooldown(alerts, 12, "flip")
    assert [s["t"] for s in suppressed] == [a(2)["t"], a(4)["t"]]
    # 13m after the delivered alert at 0m, so it survives.
    assert a(13)["t"] not in [s["t"] for s in suppressed]


def test_state_resets_on_date_change():
    alerts = [a(0, "Long", day="2026-08-13"),
              {**a(1, "Short"), "t": datetime(2026, 8, 14, 9, 20), "day": "2026-08-14"}]
    assert apply_cooldown(alerts, 12, "flip") == []


def test_flip_scope_ignores_same_direction_alerts():
    assert apply_cooldown([a(0, "Long"), a(2, "Long")], 12, "flip") == []


def test_any_scope_catches_same_direction_alerts():
    assert len(apply_cooldown([a(0, "Long"), a(2, "Long")], 12, "any")) == 1


def test_any_scope_is_never_smaller_than_flip_scope():
    alerts = [a(0, "Long"), a(2, "Short"), a(4, "Short"), a(6, "Long")]
    assert len(apply_cooldown(alerts, 12, "any")) >= len(
        apply_cooldown(alerts, 12, "flip"))


# ------------------------------------------------------------ the live figures

def test_the_planning_figure_of_four_reproduces():
    """The one claim from cooldown planning that survives re-derivation: 12m,
    flip scope, A/A+ escaping suppresses 4 of 56."""
    assert len(apply_cooldown(load_corpus(), 12, "flip")) == 4


def test_the_cooldown_catches_only_a_fifth_of_flips():
    """The figure that matters and was never stated: the mechanism exists to damp
    direction flips and, as specified, leaves 14 of 18 untouched."""
    caught = [s for s in apply_cooldown(load_corpus(), 12, "flip") if s["was_flip"]]
    assert len(caught) == 4
    assert len(flips(load_corpus())) == 18


def test_duration_barely_matters_between_five_and_fifteen_minutes():
    """5m and 15m differ by ONE alert out of 56, so the 5-vs-12-vs-30 tuning
    debate was about a parameter with almost no leverage at this scope."""
    counts = {m: len(apply_cooldown(load_corpus(), m, "flip")) for m in (5, 10, 12, 15)}
    assert counts == {5: 3, 10: 3, 12: 4, 15: 4}


def test_grade_escaping_does_most_of_the_suppression_work():
    """Without escaping, 12m/flip suppresses 11 rather than 4 — so the escape rule,
    not the duration, is the dominant parameter."""
    assert len(apply_cooldown(load_corpus(), 12, "flip", escaping=())) == 11


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
