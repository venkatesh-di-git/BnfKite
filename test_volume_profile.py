"""
Sanity tests for volume_profile.py using synthetic candles with a KNOWN
volume distribution, so we can check POC/VAH/VAL land where they should
before trusting this against real Kite data.
"""

from volume_profile import Candle, compute_profile


def make_candle(low, high, volume):
    mid = (low + high) / 2
    return Candle(open=mid, high=high, low=low, close=mid, volume=volume)


def test_symmetric_peak():
    """
    Volume peaks cleanly at 57450-57455. POC should land there, and
    VAH/VAL should expand roughly symmetrically around it.
    """
    candles = [
        make_candle(57400, 57405, 500),
        make_candle(57405, 57410, 800),
        make_candle(57410, 57415, 1200),
        make_candle(57415, 57420, 1800),
        make_candle(57420, 57425, 2500),
        make_candle(57425, 57430, 3500),
        make_candle(57430, 57435, 5000),
        make_candle(57435, 57440, 7000),
        make_candle(57440, 57445, 9000),
        make_candle(57445, 57450, 11000),  # peak
        make_candle(57450, 57455, 10500),
        make_candle(57455, 57460, 8500),
        make_candle(57460, 57465, 6500),
        make_candle(57465, 57470, 4500),
        make_candle(57470, 57475, 3000),
        make_candle(57475, 57480, 2000),
        make_candle(57480, 57485, 1300),
        make_candle(57485, 57490, 900),
        make_candle(57490, 57495, 600),
        make_candle(57495, 57500, 400),
    ]

    result = compute_profile(candles, bin_size=5.0, value_area_pct=0.70)

    print(f"POC: {result.poc}")
    print(f"VAH: {result.vah}")
    print(f"VAL: {result.val}")
    print(f"Total volume: {result.total_volume}")
    print(f"VA bins included: {len(result.value_area_bins)} of {len(result.bins)} total bins")

    va_volume = sum(result.bins[b] for b in result.value_area_bins)
    va_pct = va_volume / result.total_volume
    print(f"VA volume captured: {va_pct:.1%} (target 70%)")

    assert 57440 <= result.poc <= 57460, f"POC {result.poc} not near expected peak"
    assert result.val < result.poc < result.vah, "POC should sit inside VAL/VAH"
    assert va_pct >= 0.70, "Value area should capture at least the target volume"
    print("PASS: symmetric peak test\n")


def test_thin_session():
    """A session with very few candles shouldn't crash — should just
    widen the value area to cover most/all of the range."""
    candles = [
        make_candle(57000, 57005, 100),
        make_candle(57005, 57010, 150),
        make_candle(57010, 57015, 120),
    ]
    result = compute_profile(candles, bin_size=5.0, value_area_pct=0.70)
    print(f"Thin session -> POC {result.poc}, VAH {result.vah}, VAL {result.val}")
    assert result is not None
    print("PASS: thin session test\n")


def test_empty_session():
    result = compute_profile([], bin_size=5.0, value_area_pct=0.70)
    assert result is None
    print("PASS: empty session test\n")


def test_wide_candle_spans_multiple_bins():
    """A single volatile candle spanning many bins should spread its
    volume across all of them, not just one."""
    candles = [make_candle(57000, 57030, 3000)]  # touches bins 57000..57030 inclusive = 7 bins
    result = compute_profile(candles, bin_size=5.0, value_area_pct=0.70)
    assert len(result.bins) == 7, f"Expected 7 bins, got {len(result.bins)}"
    expected_per_bin = 3000 / 7
    for b, vol in result.bins.items():
        assert abs(vol - expected_per_bin) < 0.01, f"Expected even split of {expected_per_bin:.2f}/bin, got {vol}"
    print("PASS: wide candle spans multiple bins test\n")


if __name__ == "__main__":
    test_symmetric_peak()
    test_thin_session()
    test_empty_session()
    test_wide_candle_spans_multiple_bins()
    print("All tests passed.")
