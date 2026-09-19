"""
volume_profile.py

Approximates a session Volume Profile (POC / VAH / VAL) from 1-minute
OHLCV candles — no tick data required, since Kite's historical API
only provides OHLCV.

Method (documented explicitly so it can be validated against your own
chart readings, per the "multi-session validation" note):
  1. Build fixed-width price bins (default 5 points) spanning the
     session's traded range.
  2. Each candle's volume is spread EVENLY across every bin its
     high-low range touches. This is the standard approximation used
     when only OHLCV is available (no way to know exactly where within
     the candle's range the volume actually traded).
  3. POC = the single bin with the most volume.
  4. Value Area = expand outward from the POC, bin by bin, always
     adding whichever adjacent bin (above or below the current area)
     has more volume, until the accumulated volume reaches the target
     percentage (default 70%) of total session volume.
  5. VAL = lower edge of the lowest bin included in the value area.
     VAH = upper edge of the highest bin included in the value area.
     POC = midpoint of the single highest-volume bin.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import math


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class VolumeProfileResult:
    poc: float
    vah: float
    val: float
    total_volume: float
    bins: Dict[float, float]           # bin lower-edge -> volume
    value_area_bins: List[float]       # bin lower-edges included in VA


def build_bins(candles: List[Candle], bin_size: float = 5.0) -> Dict[float, float]:
    """Distribute each candle's volume evenly across bins it spans."""
    bins: Dict[float, float] = {}

    for c in candles:
        if c.volume <= 0 or c.high < c.low:
            continue

        lo_bin = math.floor(c.low / bin_size) * bin_size
        hi_bin = math.floor(c.high / bin_size) * bin_size

        span_bins = []
        b = lo_bin
        # guard against float drift with a rounded step count instead of
        # repeated float addition
        n_steps = int(round((hi_bin - lo_bin) / bin_size)) + 1
        for i in range(n_steps):
            span_bins.append(round(lo_bin + i * bin_size, 6))

        vol_per_bin = c.volume / len(span_bins)
        for b in span_bins:
            bins[b] = bins.get(b, 0.0) + vol_per_bin

    return dict(sorted(bins.items()))


def compute_value_area(
    bins: Dict[float, float],
    value_area_pct: float = 0.70,
    bin_size: float = 5.0,
) -> Optional[VolumeProfileResult]:
    """Given a completed bin histogram, compute POC / VAH / VAL."""
    if not bins:
        return None

    sorted_prices = sorted(bins.keys())
    total_volume = sum(bins.values())

    poc_price_bin = max(bins, key=bins.get)
    poc_idx = sorted_prices.index(poc_price_bin)

    low_idx = poc_idx
    high_idx = poc_idx
    accumulated = bins[poc_price_bin]
    target = total_volume * value_area_pct

    # Expand outward, always taking the richer side, until we hit target
    # volume or run out of bins on both sides.
    max_iterations = len(sorted_prices)
    for _ in range(max_iterations):
        if accumulated >= target:
            break

        next_high_idx = high_idx + 1
        next_low_idx = low_idx - 1

        vol_high = bins[sorted_prices[next_high_idx]] if next_high_idx < len(sorted_prices) else None
        vol_low = bins[sorted_prices[next_low_idx]] if next_low_idx >= 0 else None

        if vol_high is None and vol_low is None:
            break  # exhausted both directions — value area is the whole session

        if vol_low is None or (vol_high is not None and vol_high >= vol_low):
            high_idx = next_high_idx
            accumulated += vol_high
        else:
            low_idx = next_low_idx
            accumulated += vol_low

    poc = round(poc_price_bin + bin_size / 2, 2)
    val = round(sorted_prices[low_idx], 2)
    vah = round(sorted_prices[high_idx] + bin_size, 2)

    value_area_bins = sorted_prices[low_idx:high_idx + 1]

    return VolumeProfileResult(
        poc=poc,
        vah=vah,
        val=val,
        total_volume=total_volume,
        bins=bins,
        value_area_bins=value_area_bins,
    )


def compute_profile(
    candles: List[Candle],
    bin_size: float = 5.0,
    value_area_pct: float = 0.70,
) -> Optional[VolumeProfileResult]:
    """Convenience wrapper: candles -> bins -> POC/VAH/VAL in one call."""
    bins = build_bins(candles, bin_size=bin_size)
    return compute_value_area(bins, value_area_pct=value_area_pct, bin_size=bin_size)
