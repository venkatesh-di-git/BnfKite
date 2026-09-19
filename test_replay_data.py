"""
Tests for the front-month guard in replay/data.py.

The guard exists because far-month candles fetch fine and error on nothing. They
reproduce a session the engine never saw: the volume gate compares against a
far-month baseline, the profile is built from thin trade, and OI moves for
rollover rather than conviction. Nothing in the output looks wrong.
"""

from datetime import date

import pytest

from replay.data import (archived_days, assert_front_month, contract_expiry,
                         front_month_window, monthly_expiry)

SYMBOL = "BANKNIFTY26AUGFUT"


def test_expiry_is_the_last_tuesday():
    """Verified against a known settle date rather than assumed: BANKNIFTY26AUGFUT
    settles 25 Aug 2026, which is the last TUESDAY — the last Thursday is the 27th.
    Getting this wrong shifts the whole window by two days."""
    assert contract_expiry(SYMBOL) == date(2026, 8, 25)
    assert date(2026, 8, 25).strftime("%A") == "Tuesday"


def test_monthly_expiry_handles_a_month_ending_on_the_weekday():
    assert monthly_expiry(2026, 7) == date(2026, 7, 28)
    assert monthly_expiry(2026, 12) == date(2026, 12, 29)


def test_front_month_window_opens_the_day_after_the_previous_expiry():
    """29 Jul, not 31 Jul. The archive's OI agrees independently: it ramps through
    28 Jul (x1.29, to 2.18M) and then sits flat near 2.1M from 29 Jul onward."""
    assert front_month_window(SYMBOL) == (date(2026, 7, 29), date(2026, 8, 25))


def test_january_contract_looks_back_a_year():
    start, end = front_month_window("BANKNIFTY27JANFUT")
    assert (start, end) == (date(2026, 12, 30), date(2027, 1, 26))


def test_far_month_days_are_refused():
    for day in (date(2026, 7, 20), date(2026, 7, 28), date(2026, 5, 27)):
        with pytest.raises(ValueError, match="front month only"):
            assert_front_month(SYMBOL, day)


def test_post_expiry_days_are_refused():
    with pytest.raises(ValueError, match="front month only"):
        assert_front_month(SYMBOL, date(2026, 8, 26))


def test_front_month_days_are_accepted():
    assert_front_month(SYMBOL, date(2026, 7, 29), date(2026, 8, 14),
                       date(2026, 8, 25))


def test_the_error_names_every_offending_day():
    with pytest.raises(ValueError) as e:
        assert_front_month(SYMBOL, date(2026, 8, 14), date(2026, 7, 1),
                           date(2026, 7, 2))
    assert "2026-07-01" in str(e.value) and "2026-07-02" in str(e.value)
    assert "2026-08-14" not in str(e.value)


def test_unparseable_symbol_raises():
    with pytest.raises(ValueError, match="cannot parse"):
        front_month_window("NIFTY26AUGFUT")


def test_the_faithful_corpus_is_thirteen_sessions():
    """The spec said 11 sessions from 31 Jul. Both figures were wrong: the window
    opens 29 Jul, giving 13 archived sessions through 14 Aug."""
    start, end = front_month_window(SYMBOL)
    faithful = [d for d in archived_days(SYMBOL) if start <= d <= end]
    assert len(faithful) == 13
    assert faithful[0] == date(2026, 7, 29)
    assert faithful[-1] == date(2026, 8, 14)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
