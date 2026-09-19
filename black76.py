"""
black76.py — Black-76 option pricing, Greeks, and implied-vol inversion.

Pure math. No Kite, no network, no I/O, no config import. Deliberately
separate from bench_latency.py: this is the one part of that test with a life
beyond it — if order-price-retuning is ever built, it needs the same pricing,
and importing it from a benchmark script would be worse than the alternative
of copying it, which is how two pricing models quietly drift apart. See
volume_profile.py for the same pattern already in this repo: pure math,
dataclass result, tested alongside.

Black-76 (not Black-Scholes) because the underlying here is a FUTURES price,
not spot — Bank Nifty options are cash-settled against the futures/index, and
Black-76 is the standard model for options on a forward/futures price.

    C = e^(-rT) [F*N(d1) - K*N(d2)]
    P = e^(-rT) [K*N(-d2) - F*N(-d1)]
    d1 = (ln(F/K) + 0.5*sigma^2*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)

where F = futures price, K = strike, T = time to expiry in years,
r = risk-free rate, sigma = implied volatility.
"""

import math
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Risk-free rate: a hardcoded constant, not fetched.
#
# No free Indian T-bill source was ever named (same shape as the FII/DII
# problem in premarket_brief_spec_v2.md), and for a short-dated option the
# rate barely matters: e^(-rT) is close to 1, and moving r from 6.5% to 7.0%
# shifts implied vol in the fourth decimal place. This module measures
# latency and correctness of the pricing math, not pricing accuracy against a
# live curve. A constant removes a fetch, a cache, and an unanswered question.
# Update by hand if the ambient rate moves meaningfully (e.g. a repo cut).
# ---------------------------------------------------------------------------
DEFAULT_RISK_FREE_RATE = 0.065

# Bisection bracket and tolerance for IV inversion. NOT Brent/scipy — scipy is
# not installed, and pending_order_black76_telegram_latency_test_v2.md
# measured this stage at 0.034-0.33ms and called it "never the bottleneck";
# pulling in a dependency to speed up the one part that isn't slow is the
# wrong trade. Bisection over this bracket is ~22 evaluations of a closed
# form — microseconds — with zero new dependencies.
IV_LOW = 0.01
IV_HIGH = 3.0
IV_TOLERANCE = 1e-6
IV_MAX_ITERATIONS = 100


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function — no scipy needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(F: float, K: float, T: float, sigma: float) -> tuple:
    if sigma <= 0 or T <= 0:
        raise ValueError(f"sigma and T must be positive (sigma={sigma}, T={T})")
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def price(F: float, K: float, T: float, r: float, sigma: float,
          is_call: bool) -> float:
    """Black-76 theoretical price. F, K > 0; T in years; sigma as a decimal
    (0.20 = 20%), not a percentage."""
    d1, d2 = _d1_d2(F, K, T, sigma)
    discount = math.exp(-r * T)
    if is_call:
        return discount * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return discount * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


@dataclass
class Greeks:
    delta: float
    gamma: float
    vega: float   # per 1.00 (100 vol points) change in sigma, i.e. per unit
    theta: float  # per year — divide by 365 for a per-day figure
    rho: float


def greeks(F: float, K: float, T: float, r: float, sigma: float,
          is_call: bool) -> Greeks:
    """Standard Black-76 Greeks. Same F/K/T/r/sigma conventions as price()."""
    d1, d2 = _d1_d2(F, K, T, sigma)
    discount = math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    pdf_d1 = _norm_pdf(d1)

    if is_call:
        delta = discount * _norm_cdf(d1)
        theta = (-discount * F * pdf_d1 * sigma / (2 * sqrt_t)
                + r * discount * K * _norm_cdf(d2) - r * discount * F * _norm_cdf(d1))
        rho = -T * discount * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    else:
        delta = -discount * _norm_cdf(-d1)
        theta = (-discount * F * pdf_d1 * sigma / (2 * sqrt_t)
                - r * discount * K * _norm_cdf(-d2) + r * discount * F * _norm_cdf(-d1))
        rho = -T * discount * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))

    gamma = discount * pdf_d1 / (F * sigma * sqrt_t)
    vega = discount * F * pdf_d1 * sqrt_t

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def implied_vol(F: float, K: float, T: float, r: float, market_price: float,
                is_call: bool) -> Optional[float]:
    """Solve Black76(F, K, T, r, sigma) = market_price for sigma via
    bisection. Returns None rather than raising when no root exists in the
    bracket — a stale or zero LTP with the market closed is exactly this case,
    and bench_latency.py must still record its stage timings when this
    happens, not abort the iteration.
    """
    if market_price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None

    try:
        lo_price = price(F, K, T, r, IV_LOW, is_call)
        hi_price = price(F, K, T, r, IV_HIGH, is_call)
    except ValueError:
        return None

    # market_price must lie between the prices at the bracket's endpoints, or
    # bisection has no root to find (e.g. a market_price above what even
    # 300% vol can produce — a bad/stale quote).
    if not (lo_price - IV_TOLERANCE <= market_price <= hi_price + IV_TOLERANCE):
        return None

    lo, hi = IV_LOW, IV_HIGH
    for _ in range(IV_MAX_ITERATIONS):
        mid = (lo + hi) / 2.0
        try:
            mid_price = price(F, K, T, r, mid, is_call)
        except ValueError:
            return None
        diff = mid_price - market_price
        if abs(diff) < IV_TOLERANCE:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0  # ran out of iterations; best estimate
