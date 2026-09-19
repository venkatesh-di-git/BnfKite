"""
Tests for black76.py.

Pure math, so these need no Kite session, no network, no market hours — they
run anywhere, anytime. The tests that matter: put-call parity (catches a sign
error in either price() branch), a round-trip through implied_vol() (catches
a bisection bracket/convergence bug), and the documented "no root" case that
bench_latency.py depends on to keep recording timings when the market is
closed and quotes are stale.
"""

import math

import pytest

import black76


F = 57800.0    # Bank Nifty futures, illustrative
K = 57800.0    # ATM
T = 15 / 365   # 15 days to expiry
R = black76.DEFAULT_RISK_FREE_RATE
SIGMA = 0.14   # 14% vol, roughly realistic for Bank Nifty


def test_put_call_parity():
    """C - P = e^(-rT)(F - K). Holds regardless of sigma, so this is the
    cheapest check that both price() branches (and their signs) are correct."""
    call = black76.price(F, K, T, R, SIGMA, is_call=True)
    put = black76.price(F, K, T, R, SIGMA, is_call=False)
    expected = math.exp(-R * T) * (F - K)
    assert call - put == pytest.approx(expected, abs=1e-6)


def test_deep_itm_call_converges_to_intrinsic():
    """A call struck far below the futures price is worth ~ the discounted
    intrinsic value regardless of vol — a sanity bound, not a vol-specific
    number."""
    deep_itm = black76.price(F, K=F * 0.5, T=T, r=R, sigma=SIGMA, is_call=True)
    intrinsic = math.exp(-R * T) * (F - F * 0.5)
    assert deep_itm == pytest.approx(intrinsic, rel=1e-4)


def test_deep_otm_put_is_nearly_worthless():
    deep_otm = black76.price(F, K=F * 0.5, T=T, r=R, sigma=SIGMA, is_call=False)
    assert deep_otm < 0.01


@pytest.mark.parametrize("is_call", [True, False])
@pytest.mark.parametrize("sigma", [0.08, 0.14, 0.25, 0.60])
def test_implied_vol_round_trips_through_price(is_call, sigma):
    """Price at a known sigma, then invert — must recover it within the
    documented tolerance. This is the real test of the bisection bracket."""
    market_price = black76.price(F, K, T, R, sigma, is_call)
    recovered = black76.implied_vol(F, K, T, R, market_price, is_call)
    assert recovered is not None
    assert recovered == pytest.approx(sigma, abs=1e-4)


def test_implied_vol_returns_none_for_an_unreachable_price():
    """A market_price above what even 300% vol can produce — a stale or
    corrupted quote. bench_latency.py's Mode A depends on this returning None
    rather than raising, so a bad quote with the market closed still lets the
    iteration record its stage timings instead of aborting."""
    absurd_price = F * 10  # far beyond any price at IV_HIGH
    assert black76.implied_vol(F, K, T, R, absurd_price, is_call=True) is None


def test_implied_vol_returns_none_for_non_positive_inputs():
    assert black76.implied_vol(0, K, T, R, 100, True) is None
    assert black76.implied_vol(F, 0, T, R, 100, True) is None
    assert black76.implied_vol(F, K, 0, R, 100, True) is None
    assert black76.implied_vol(F, K, T, R, 0, True) is None
    assert black76.implied_vol(F, K, T, R, -5, True) is None


def test_greeks_delta_is_bounded():
    """Discounted delta for a call is in (0, e^(-rT)); for a put, in
    (-e^(-rT), 0). Catches a sign flip without pinning an exact figure."""
    discount = math.exp(-R * T)
    call_g = black76.greeks(F, K, T, R, SIGMA, is_call=True)
    put_g = black76.greeks(F, K, T, R, SIGMA, is_call=False)
    assert 0 < call_g.delta < discount
    assert -discount < put_g.delta < 0


def test_greeks_gamma_and_vega_are_identical_for_call_and_put():
    """Gamma and vega do not depend on option type in Black-76 — a symmetry
    check that would catch a copy-paste error between the two branches."""
    call_g = black76.greeks(F, K, T, R, SIGMA, is_call=True)
    put_g = black76.greeks(F, K, T, R, SIGMA, is_call=False)
    assert call_g.gamma == pytest.approx(put_g.gamma, rel=1e-9)
    assert call_g.vega == pytest.approx(put_g.vega, rel=1e-9)


def test_greeks_vega_matches_a_finite_difference_bump():
    """Vega should match a small numerical bump in sigma — an independent
    check of the closed-form derivative against the function it differentiates."""
    call_g = black76.greeks(F, K, T, R, SIGMA, is_call=True)
    bump = 1e-4
    p_up = black76.price(F, K, T, R, SIGMA + bump, is_call=True)
    p_down = black76.price(F, K, T, R, SIGMA - bump, is_call=True)
    numerical_vega = (p_up - p_down) / (2 * bump)
    assert call_g.vega == pytest.approx(numerical_vega, rel=1e-3)
