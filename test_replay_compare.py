"""
Tests for replay/compare.py — the match rule.

These matter more than they look. The claim that justified switching tick models
("10 of 13 reproduced") was an alert COUNT dressed up as a match count, produced by
a script nobody could re-run. If the rule below is wrong, every figure in the spec
is wrong in the same invisible way.
"""

from datetime import date, datetime, timedelta

import pytest

import config
from engine import engine_version
from replay.compare import (PRICE_TOLERANCE, TIME_TOLERANCE, ceiling,
                            compare_day, load_live_alerts, live_session_start,
                            live_versions, match, version_warning)
from replay.ticks import CLOSE_ONLY_MODE, OHLC_MODE

pytestmark = pytest.mark.skipif(
    not config.DRY_RUN, reason="replay requires BN_DRY_RUN=1")

T0 = datetime(2026, 8, 14, 9, 48, 3)


def a(t=T0, direction="long", setup="ema+vwap", price=57799.0, grade="A"):
    return {"t": t, "direction": direction, "setup": setup, "price": price,
            "grade": grade}


# ------------------------------------------------------------- the strict rule

def test_an_exact_pair_matches():
    pairs, missed, extra = match([a()], [a(t=T0 + timedelta(seconds=11), price=57800.0)])
    assert len(pairs) == 1 and not missed and not extra


def test_price_beyond_tolerance_does_not_match():
    """The real 14 Aug case that was wrongly reported as a match: live 10:30:42 at
    57735.0 against replay 10:30:44 at 57730.0 — two seconds apart, five points."""
    pairs, missed, _ = match([a(price=57735.0)],
                             [a(t=T0 + timedelta(seconds=2), price=57730.0)])
    assert not pairs and len(missed) == 1


def test_price_exactly_at_tolerance_matches():
    pairs, _, _ = match([a(price=57799.0)], [a(price=57799.0 + PRICE_TOLERANCE)])
    assert len(pairs) == 1


def test_setup_mismatch_does_not_match():
    """live 15:11:05 ema+vwap against replay 15:11:14 vwap — nine seconds apart and
    still not the same signal."""
    pairs, missed, _ = match([a(setup="ema+vwap")],
                             [a(t=T0 + timedelta(seconds=9), setup="vwap")])
    assert not pairs and len(missed) == 1


def test_time_beyond_tolerance_does_not_match():
    pairs, missed, _ = match([a()], [a(t=T0 + TIME_TOLERANCE + timedelta(seconds=1))])
    assert not pairs and len(missed) == 1


def test_direction_mismatch_never_matches():
    pairs, missed, _ = match([a(direction="long")], [a(direction="short")])
    assert not pairs and len(missed) == 1


def test_grade_is_not_part_of_the_rule():
    """Deliberate: replay reaching the same setup at a different grade is a
    reproduction, and grading is a downstream scoring question."""
    pairs, _, _ = match([a(grade="A")], [a(grade="B")])
    assert len(pairs) == 1


# --------------------------------------------------------------- one-to-one

def test_one_replay_alert_cannot_claim_two_live_alerts():
    """Without this, a single replay alert in a busy minute inflates the score by
    matching every live alert around it."""
    live = [a(t=T0), a(t=T0 + timedelta(seconds=4))]
    pairs, missed, _ = match(live, [a(t=T0 + timedelta(seconds=1))])
    assert len(pairs) == 1 and len(missed) == 1


def test_the_nearest_candidate_wins():
    live = [a(t=T0)]
    rep = [a(t=T0 + timedelta(seconds=40)), a(t=T0 + timedelta(seconds=3))]
    pairs, _, extra = match(live, rep)
    assert pairs[0][1]["t"] == T0 + timedelta(seconds=3)
    assert len(extra) == 1


def test_unmatched_replay_alerts_are_reported_as_extra():
    _, _, extra = match([], [a(), a(t=T0 + timedelta(minutes=5))])
    assert len(extra) == 2


# ------------------------------------------------------------------ ceiling

def test_ceiling_ignores_price_and_setup():
    pairs, _, _ = ceiling([a(setup="ema+vwap", price=57799.0)],
                          [a(setup="vwap", price=57730.0,
                             t=T0 + timedelta(seconds=80))])
    assert len(pairs) == 1


def test_ceiling_still_requires_direction():
    pairs, missed, _ = ceiling([a(direction="long")], [a(direction="short")])
    assert not pairs and len(missed) == 1


def test_ceiling_is_never_smaller_than_strict():
    live = [a(), a(t=T0 + timedelta(minutes=3), direction="short", setup="vwap")]
    rep = [a(t=T0 + timedelta(seconds=5), price=57799.0),
           a(t=T0 + timedelta(minutes=3, seconds=70), direction="short",
             setup="ema", price=57700.0)]
    assert len(ceiling(live, rep)[0]) >= len(match(live, rep)[0])


# ------------------------------------------------------------- the live log

def test_live_alerts_load_for_a_known_day():
    live = load_live_alerts(date(2026, 8, 14))
    assert len(live) == 13
    assert live[0]["t"].strftime("%H:%M:%S") == "09:21:18"


def test_live_session_start_is_read_from_the_manifest():
    """13 Aug seeded at 09:58:30 after a token expiry. Without this window,
    replay is charged for the 43 minutes live never ran."""
    assert live_session_start(date(2026, 8, 13)).strftime("%H:%M:%S") == "09:58:30"
    assert live_session_start(date(2026, 8, 14)).strftime("%H:%M:%S") == "09:15:00"


def test_unknown_day_has_no_window():
    assert live_session_start(date(2026, 7, 29)) is None


# ----------------------------------------------------------------- the score

def test_fourteenth_august_scores_as_measured():
    """Regression on the headline figures. If these move, the spec's §1 table is
    stale and must be regenerated — not quietly left in place.

    These are POST-COOLDOWN figures, and they are lower than the pre-cooldown ones
    (10 fired, 3 strict, 8 ceiling) for a reason that is not a fidelity regression:
    the flip cooldown withholds 09:56:44 B Long, which had been one of the three
    strict matches. The live log was produced by an engine WITHOUT the cooldown, so
    replay is now being scored against different rules — see version_warning()."""
    row = compare_day("BANKNIFTY26AUGFUT", date(2026, 8, 14),
                      [OHLC_MODE, CLOSE_ONLY_MODE])
    assert len(row["live"]) == 13
    ohlc, close = row["modes"][OHLC_MODE], row["modes"][CLOSE_ONLY_MODE]
    assert (ohlc["fired"], len(ohlc["pairs"]), ohlc["ceiling"]) == (9, 2, 7)
    assert (close["fired"], len(close["pairs"]), close["ceiling"]) == (2, 0, 2)


def test_a_version_mismatch_against_the_live_log_is_flagged():
    """The live log is a fixed historical record and the engine moves. Once a rule
    change ships, a reproduction score silently becomes a comparison between two
    different engines — which is exactly what §10 forbids elsewhere. Warned rather
    than refused, because the comparison is still worth running."""
    days = [date(2026, 8, 14)]
    assert live_versions(days) == {"b3620733"}
    warning = version_warning(days)
    assert warning is not None
    assert "b3620733" in warning and engine_version() in warning
    assert "reproduction score" in warning


def test_close_only_is_kept_as_the_control():
    """5/56 only means something next to a 0/56 measured the same way."""
    row = compare_day("BANKNIFTY26AUGFUT", date(2026, 8, 14), [CLOSE_ONLY_MODE])
    assert row["modes"][CLOSE_ONLY_MODE]["ticks"] == 375


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
