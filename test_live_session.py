"""
Tests for LiveFiveMinuteSession (engine.py) — specifically that a
5-minute bucket, once closed, can never be reopened.

Bar closure is driven by two independent clocks: advance() uses the local
wall clock (passed in by app.py's 1s timer), while apply_tick() uses the
tick's exchange_timestamp, which lags real time during a lull. When those
straddle a boundary they can fight — advance() closes the bar, a late tick
reopens the same bucket, advance() closes it again — appending a duplicate
fragment bar to `completed` on every spin.

That corrupts everything derived from completed bars: the EMA takes an
extra step per fragment, the SMA20 volume average is diluted by
near-zero-volume fragments, and _oi_pattern() compares against a fragment
from seconds ago instead of the previous 5-minute bar.

The replay helpers below therefore keep tick timestamps and wall-clock
times INDEPENDENTLY controllable — that separation is the whole point.

Assertions read through snapshot() wherever possible, since that dict is
what app.py and main.py actually consume.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
from engine import FiveMinuteCandle, LiveFiveMinuteSession, five_minute_start

IST = ZoneInfo("Asia/Kolkata")

TODAY_BAR_VOLUME = 2000.0
PRIMED_BAR_VOLUME = 500.0  # volume the 15:00 bar accumulates in prime_current_bar()


def at(hour, minute, second=0):
    """A timestamp on the session's trading day."""
    return datetime(2026, 8, 3, hour, minute, second, tzinfo=IST)


def todays_bars(count=20):
    """`count` completed 5-minute bars from 09:15, all well before 15:00 so
    LiveFiveMinuteSession treats every one of them as already closed."""
    bars = []
    start = at(9, 15)
    for i in range(count):
        price = 57900.0 + i
        bars.append(FiveMinuteCandle(
            start=start, open=price, high=price + 5, low=price - 5, close=price,
            volume=TODAY_BAR_VOLUME, oi_open=2_000_000.0, oi_close=2_000_000.0 + i * 100,
        ))
        start += timedelta(minutes=5)
    return bars


def make_session(now=None, bar_count=20):
    return LiveFiveMinuteSession(
        todays_bars(bar_count), now or at(15, 0), config.BIN_SIZE, config.VALUE_AREA_PCT,
        ema_period=config.EMA_PERIOD,
    )


def prime_current_bar(session):
    """Open the 15:00 bar with two in-bucket ticks. The first only sets the
    cumulative-volume baseline (no delta is attributable yet), so the bar
    ends up holding exactly PRIMED_BAR_VOLUME."""
    session.apply_tick(57900.0, 500_000.0, 2_002_000.0, at(15, 0, 10))
    session.apply_tick(57905.0, 500_000.0 + PRIMED_BAR_VOLUME, 2_002_100.0, at(15, 0, 30))


def replay_lagging_boundary(session, spins=15):
    """Reproduce the observed failure: the wall clock has crossed into 15:05
    while exchange_timestamps are still inside 15:00. app.py's 1s timer calls
    advance() throughout. Returns the profile's total_volume after each spin."""
    totals = []
    for i in range(spins):
        session.advance(at(15, 5, 1 + i))
        session.apply_tick(57906.0 + i * 0.1, 500_600.0 + i * 10, 2_002_150.0 + i, at(15, 0, 40 + i))
        totals.append(session.snapshot()["result"].total_volume)
    return totals


def test_bar_start_is_the_bar_handed_out_not_the_wall_clock():
    """The alert latch keys on bar_start, so it must name the bar that
    produced current_candle — never five_minute_start(now).

    advance() nulls self.current when it closes a bar, so the snapshot from
    that very call carries the COMPLETED bar while the wall clock has already
    rolled into the next window. A clock-derived key would stamp the old bar's
    alert with the NEW bar's window, latch it, and then silently swallow the
    first genuine alert of that new bar — a missed setup with no error.

    Same trap already documented for elapsed_seconds.
    """
    session = make_session()
    prime_current_bar(session)
    assert session.snapshot()["bar_start"] == at(15, 0)

    # Wall clock crosses into 15:05; the 15:00 bar closes and current is None.
    snap = session.advance(at(15, 5, 1))
    assert session.current is None, "precondition: advance() closed the bar"
    assert snap["bar_start"] == at(15, 0), "must name the completed bar handed out"
    assert snap["bar_start"] != five_minute_start(at(15, 5, 1)), "must not follow the clock"
    assert snap["current_candle"].close == 57905.0, "and it is that bar's candle"

    # Once a tick opens the new bucket, bar_start follows the new bar.
    session.apply_tick(57910.0, 501_000.0, 2_002_200.0, at(15, 5, 10))
    assert session.snapshot()["bar_start"] == at(15, 5)
    print("PASS: bar_start names the bar handed out, not the wall clock\n")


def test_bar_start_is_none_before_any_bar_exists():
    """The latch's documented fallback case — nothing to key on yet."""
    session = LiveFiveMinuteSession([], at(9, 15), config.BIN_SIZE, config.VALUE_AREA_PCT,
                                    ema_period=config.EMA_PERIOD)
    assert session.snapshot()["bar_start"] is None
    print("PASS: bar_start is None before any bar exists\n")


def test_late_tick_does_not_reopen_closed_bucket():
    """The core bug: 15 spins of (advance past the boundary, late tick) must
    close the 15:00 bar exactly ONCE, not once per spin."""
    session = make_session()
    prime_current_bar(session)
    before = session.snapshot()["candle_count"]

    replay_lagging_boundary(session, spins=15)

    after = session.snapshot()["candle_count"]
    assert after == before + 1, f"candle_count grew by {after - before}, expected 1"
    print("PASS: a lagging tick cannot reopen a closed bucket\n")


def test_ema_takes_one_step_per_bar():
    """Each duplicate close runs another EMA step, which drags EMA10 onto
    spot price. Exactly one step should happen across the boundary."""
    session = make_session()
    prime_current_bar(session)
    # completed_ema directly, not snapshot()["ema_10_prev"] — that key now
    # carries EMA10 as of the slope lookback window, not the last bar's close.
    ema_before = session.completed_ema
    closing_price = session.current.close

    replay_lagging_boundary(session, spins=15)

    multiplier = 2 / (config.EMA_PERIOD + 1)
    expected = (closing_price - ema_before) * multiplier + ema_before
    actual = session.completed_ema
    assert abs(actual - expected) < 0.01, f"EMA {actual:.4f}, expected one step to {expected:.4f}"
    print("PASS: EMA advances exactly one step per closed bar\n")


def test_sma20_volume_not_diluted_by_late_ticks():
    """The Signal Engine's Volume rule divides by this average. Fragment bars
    carry almost no volume, so duplicates collapse it and inflate relative
    volume past the High threshold."""
    session = make_session()
    prime_current_bar(session)

    replay_lagging_boundary(session, spins=15)

    # Last 20 completed = 19 seeded bars + the single real 15:00 bar.
    expected = (19 * TODAY_BAR_VOLUME + PRIMED_BAR_VOLUME) / 20
    actual = session.snapshot()["average_bar_volume"]
    assert abs(actual - expected) < 0.01, f"SMA20 volume {actual:.2f}, expected {expected:.2f}"
    print("PASS: SMA20 volume average is not diluted by late ticks\n")


def test_profile_total_volume_stable_across_boundary():
    """Each duplicate close recomputes the profile over a longer `completed`
    list, so POC/VAH/VAL and total_volume visibly flip-flop."""
    session = make_session()
    prime_current_bar(session)

    totals = replay_lagging_boundary(session, spins=15)

    assert len(set(totals)) == 1, f"total_volume oscillated across {len(set(totals))} values: {sorted(set(totals))}"
    print("PASS: profile total_volume is stable across the boundary\n")


def test_normal_rollover_still_closes_one_bar():
    """Guards against over-clamping — an in-sync tick in the next bucket must
    still close the old bar and open the new one."""
    session = make_session()
    prime_current_bar(session)
    before = session.snapshot()["candle_count"]

    session.apply_tick(57910.0, 501_000.0, 2_002_200.0, at(15, 5, 3))

    assert session.snapshot()["candle_count"] == before + 1
    assert session.current.start == at(15, 5), f"opened bucket {session.current.start}, expected 15:05"
    print("PASS: an in-sync rollover still closes exactly one bar\n")


def test_tick_two_buckets_ahead_opens_correct_bucket():
    """A gap in trading must not be clamped back to the bucket immediately
    after the last closed one."""
    session = make_session()
    prime_current_bar(session)
    session.advance(at(15, 5, 1))  # closes 15:00

    session.apply_tick(57920.0, 502_000.0, 2_002_400.0, at(15, 10, 7))

    assert session.current.start == at(15, 10), f"opened bucket {session.current.start}, expected 15:10"
    print("PASS: a tick two buckets ahead opens its own bucket\n")


def test_repeated_advance_within_bucket_is_noop():
    """The 1s timer calls advance() constantly inside a bar; none of those
    calls should close anything."""
    session = make_session()
    prime_current_bar(session)
    before = session.snapshot()["candle_count"]

    for second in range(10, 60, 5):
        session.advance(at(15, 0, second))

    assert session.snapshot()["candle_count"] == before
    print("PASS: advance() inside the current bucket never closes a bar\n")


def replay_seconds(session, price_at, seconds, start=None, base_volume=700_000.0):
    """Drive one second of wall clock per step: apply a tick, then advance().
    Stays inside the 15:00 bucket so no bar closes. Returns the slope
    (ema_10 - ema_10_prev) after each step, skipping steps where the lookback
    window hasn't filled yet."""
    origin = start or at(15, 0)
    slopes = []
    for t in range(seconds):
        moment = origin + timedelta(seconds=t)
        session.apply_tick(price_at(t), base_volume + t * 10, 2_002_000.0 + t, moment)
        snap = session.advance(moment)
        if snap["ema_10"] is not None and snap["ema_10_prev"] is not None:
            slopes.append(snap["ema_10"] - snap["ema_10_prev"])
    return slopes


def _sign_changes(values, tolerance=1e-9):
    signs = [(1 if v > tolerance else -1 if v < -tolerance else 0) for v in values]
    signs = [s for s in signs if s != 0]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def test_slope_is_positive_while_price_rises():
    session = make_session()
    base = session.completed_ema

    slopes = replay_seconds(session, lambda t: base + 0.5 * t, 150)

    assert slopes, "lookback window never filled"
    assert all(s > 0 for s in slopes), f"expected all-positive slopes, got {slopes[:5]}"
    print("PASS: slope is positive throughout a rising series\n")


def test_slope_is_negative_while_price_falls():
    session = make_session()
    base = session.completed_ema

    slopes = replay_seconds(session, lambda t: base - 0.5 * t, 150)

    assert slopes and all(s < 0 for s in slopes), f"expected all-negative slopes, got {slopes[:5]}"
    print("PASS: slope is negative throughout a falling series\n")


def test_slope_does_not_flip_on_chop_around_the_closed_ema():
    """The regression. Price oscillates +/-15 around the last closed bar's EMA
    while drifting up 0.2/s. The old slope was alpha * (price - closed_ema), so
    it flipped sign on every oscillation; measured across a window it tracks the
    drift instead."""
    session = make_session()
    base = session.completed_ema

    def price_at(t):
        # Square wave, period 20s — an exact number of periods fits the 60s
        # lookback, so the oscillation cancels and only the drift remains.
        return base + 0.2 * t + (15.0 if (t // 10) % 2 == 0 else -15.0)

    slopes = replay_seconds(session, price_at, 200)

    flips = _sign_changes(slopes)
    assert flips == 0, f"slope changed sign {flips} times on a choppy tape"
    assert all(s > 0 for s in slopes), "slope should follow the upward drift"
    print("PASS: slope holds its sign through chop around the closed EMA\n")


def test_slope_unknown_until_the_lookback_window_fills():
    session = make_session()
    base = session.completed_ema
    half = config.EMA_SLOPE_LOOKBACK_SECONDS // 2

    for t in range(half):
        moment = at(15, 0) + timedelta(seconds=t)
        session.apply_tick(base + t, 700_000.0 + t * 10, 2_002_000.0, moment)
        snap = session.advance(moment)
        assert snap["ema_10_prev"] is None, f"slope available after only {t}s"

    print("PASS: ema_10_prev stays None until the lookback window fills\n")


def test_lookback_window_is_time_based_not_sample_count():
    """main.py advances every SNAPSHOT_INTERVAL_SECONDS, app.py every second.
    A fixed-length buffer would span different durations in the two runners."""
    lookback = config.EMA_SLOPE_LOOKBACK_SECONDS
    base = make_session().completed_ema

    def slope_after(step_seconds):
        session = make_session()
        elapsed, last = 0, None
        while elapsed <= lookback + step_seconds:
            moment = at(15, 0) + timedelta(seconds=elapsed)
            session.apply_tick(base + elapsed, 700_000.0 + elapsed * 10, 2_002_000.0, moment)
            last = session.advance(moment)
            elapsed += step_seconds
        return last["ema_10"] - last["ema_10_prev"]

    one_second = slope_after(1)
    two_second = slope_after(2)

    # Same price ramp and same window, a quarter of the samples — the slope
    # should agree to within one sampling step's worth of drift.
    assert abs(one_second - two_second) < abs(one_second) * 0.15, \
        f"1s cadence gave {one_second:.4f}, 2s cadence {two_second:.4f}"
    print("PASS: the lookback window spans wall-clock time, not sample count\n")


def test_ema_history_does_not_grow_unbounded():
    session = make_session()
    base = session.completed_ema

    replay_seconds(session, lambda t: base + 0.1 * t, 280)

    # Retention is 2x the lookback, so the buffer can't accumulate a session.
    assert len(session._ema_history) <= config.EMA_SLOPE_LOOKBACK_SECONDS * 2 + 2, \
        f"buffer held {len(session._ema_history)} samples"
    print("PASS: the EMA sample buffer is bounded by the retention window\n")


def test_snapshot_candle_is_an_immutable_copy():
    """snapshot() used to hand out the live `current` bar, which the websocket
    thread keeps mutating. A consumer mid-evaluation must not see it change."""
    session = make_session()
    prime_current_bar(session)

    candle = session.snapshot()["current_candle"]
    before = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    assert candle is not session.current, "snapshot handed out the live bar itself"

    session.apply_tick(57950.0, 500_000.0 + PRIMED_BAR_VOLUME + 300, 2_002_200.0, at(15, 0, 40))

    after = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    assert after == before, f"an already-returned candle mutated: {before} -> {after}"
    print("PASS: snapshot() returns an immutable copy of the forming bar\n")


def test_snapshot_volume_fields_agree():
    """current_bar_volume and current_candle.volume come from the same copy,
    so they can never disagree the way two separate reads could."""
    session = make_session()
    prime_current_bar(session)

    snap = session.snapshot()

    assert snap["current_bar_volume"] == snap["current_candle"].volume
    print("PASS: current_bar_volume agrees with current_candle.volume\n")


# ---------------------------------------------------------------
# Prior-session seeding: seed bars warm EMA10 and the volume baseline,
# and must never reach VWAP, the Volume Profile, or the OI pattern.
# ---------------------------------------------------------------

SEED_BAR_VOLUME = 9000.0
SEED_BAR_PRICE = 50000.0  # deliberately far from today's ~57900 level


def prior_session_bars(count=36, volume=SEED_BAR_VOLUME, price=SEED_BAR_PRICE):
    """Yesterday's bars, ending just before today's open."""
    bars = []
    start = at(9, 15) - timedelta(days=1)
    for i in range(count):
        bars.append(FiveMinuteCandle(
            start=start, open=price, high=price + 5, low=price - 5, close=price,
            volume=volume, oi_open=5_000_000.0, oi_close=5_000_000.0,
        ))
        start += timedelta(minutes=5)
    return bars


def seeded_session(seed=None, today_bar_count=1):
    return LiveFiveMinuteSession(
        todays_bars(today_bar_count), at(15, 0), config.BIN_SIZE, config.VALUE_AREA_PCT,
        ema_period=config.EMA_PERIOD, seed_bars=prior_session_bars() if seed is None else seed,
    )


def test_ema_is_not_todays_first_bar_close():
    """Unseeded, _rebuild_completed_state sets completed_ema to the first
    bar's close outright — so EMA10 at 09:20 *is* that close."""
    unseeded = LiveFiveMinuteSession(todays_bars(1), at(15, 0), config.BIN_SIZE,
                                     config.VALUE_AREA_PCT, ema_period=config.EMA_PERIOD)
    first_close = unseeded.completed[0].close
    assert unseeded.completed_ema == first_close, "precondition: unseeded EMA is the first close"

    seeded = seeded_session()

    assert abs(seeded.completed_ema - first_close) > 100, \
        f"seeded EMA {seeded.completed_ema:.2f} still tracks the first close {first_close:.2f}"
    print("PASS: seeding stops EMA10 being today's first bar close\n")


def test_volume_baseline_is_full_from_the_first_bar():
    session = seeded_session(today_bar_count=1)

    expected = (19 * SEED_BAR_VOLUME + TODAY_BAR_VOLUME) / 20
    actual = session.snapshot()["average_bar_volume"]

    assert abs(actual - expected) < 0.01, f"SMA20 volume {actual:.2f}, expected {expected:.2f}"
    print("PASS: the volume baseline holds 20 bars from today's first bar\n")


def test_vwap_excludes_seed_bars():
    """VWAP is session-anchored — seeding it would give a multi-day VWAP."""
    with_seed = seeded_session()
    without_seed = LiveFiveMinuteSession(todays_bars(1), at(15, 0), config.BIN_SIZE,
                                         config.VALUE_AREA_PCT, ema_period=config.EMA_PERIOD)

    assert with_seed.snapshot()["vwap"] == without_seed.snapshot()["vwap"]
    print("PASS: VWAP is identical with and without seed bars\n")


def test_volume_profile_excludes_seed_bars():
    """The multi-day-profile trap: seed bars at 50000 would drag POC/VAH/VAL
    away from today's traded range entirely."""
    with_seed = seeded_session(today_bar_count=5).snapshot()["result"]
    without = LiveFiveMinuteSession(todays_bars(5), at(15, 0), config.BIN_SIZE,
                                    config.VALUE_AREA_PCT,
                                    ema_period=config.EMA_PERIOD).snapshot()["result"]

    assert (with_seed.poc, with_seed.vah, with_seed.val) == (without.poc, without.vah, without.val)
    assert with_seed.total_volume == without.total_volume
    print("PASS: POC/VAH/VAL and total volume ignore seed bars\n")


def test_oi_pattern_never_spans_the_overnight_gap():
    """Comparing yesterday's last bar against today's first would measure the
    OVERNIGHT OI change and read it as fresh conviction on bar one."""
    session = seeded_session(today_bar_count=1)
    session.apply_tick(57950.0, 500_000.0, 2_002_000.0, at(15, 0, 5))

    snap = session.snapshot()

    # Seed OI is 5,000,000; today's bars are ~2,000,000. Neither the reported
    # OI nor its comparison point may come from the seed.
    assert snap["oi"] is None or snap["oi"] < 3_000_000, f"OI {snap['oi']} came from a seed bar"
    if snap["previous_oi"] is not None:
        assert snap["previous_oi"] < 3_000_000, "OI compared against a seed bar"
    print("PASS: the OI pattern never compares across the overnight gap\n")


def test_empty_seed_reproduces_previous_behaviour():
    """Safety net for when prior-session history is unavailable."""
    without = LiveFiveMinuteSession(todays_bars(3), at(15, 0), config.BIN_SIZE,
                                    config.VALUE_AREA_PCT, ema_period=config.EMA_PERIOD)
    explicit_none = LiveFiveMinuteSession(todays_bars(3), at(15, 0), config.BIN_SIZE,
                                          config.VALUE_AREA_PCT, ema_period=config.EMA_PERIOD,
                                          seed_bars=None)

    assert without.completed_ema == explicit_none.completed_ema
    assert without.snapshot()["average_bar_volume"] == explicit_none.snapshot()["average_bar_volume"]
    print("PASS: an empty seed reproduces the previous behaviour exactly\n")


def test_fewer_seed_bars_than_lookback_is_safe():
    session = seeded_session(seed=prior_session_bars(count=5), today_bar_count=3)

    average = session.snapshot()["average_bar_volume"]

    expected = (5 * SEED_BAR_VOLUME + 3 * TODAY_BAR_VOLUME) / 8
    assert abs(average - expected) < 0.01, f"averaged {average:.2f}, expected {expected:.2f}"
    print("PASS: fewer seed bars than the lookback averages what exists\n")


# ---------------------------------------------------------------
# elapsed_seconds: derived from the bar being handed out, never the clock.
# ---------------------------------------------------------------

def test_forming_bar_reports_true_partial_elapsed():
    session = make_session()
    session.apply_tick(57900.0, 500_000.0, 2_002_000.0, at(15, 0, 0))

    snap = session.advance(at(15, 1, 0))

    assert snap["elapsed_seconds"] == 60.0, f"got {snap['elapsed_seconds']}"
    print("PASS: a forming bar reports its true partial elapsed\n")


def test_completed_bar_reports_full_elapsed_not_clock_age():
    """THE regression. advance() nulls self.current when it closes a bar, so
    snapshot() falls back to completed[-1] — the returned candle is a FINISHED
    bar. A clock-derived elapsed would read ~1s and project its full volume by
    300x, deterministically, at every bar close."""
    session = make_session()
    session.apply_tick(57900.0, 500_000.0, 2_002_000.0, at(15, 0, 10))
    session.apply_tick(57905.0, 500_500.0, 2_002_100.0, at(15, 4, 50))

    snap = session.advance(at(15, 5, 1))  # rolls over; five_minute_start(now) just moved

    assert session.current is None, "precondition: advance() closed the bar"
    assert snap["elapsed_seconds"] == config.BAR_SECONDS, \
        f"completed bar reported {snap['elapsed_seconds']}s — a 300x projection"
    print("PASS: a completed bar reports full elapsed, not ~1s of clock age\n")


def test_feed_stall_keeps_elapsed_at_full_bar():
    """During a stall advance() keeps running with no ticks, so current_candle
    stays a finished bar for the whole stall."""
    session = make_session()
    session.apply_tick(57900.0, 500_000.0, 2_002_000.0, at(15, 0, 10))
    session.advance(at(15, 5, 1))

    for minutes in range(1, 6):
        snap = session.advance(at(15, 5 + minutes, 1))
        assert snap["elapsed_seconds"] == config.BAR_SECONDS, \
            f"after {minutes}min stall: {snap['elapsed_seconds']}"
    print("PASS: a stalled feed keeps elapsed pinned to a full bar\n")


def test_no_bars_at_all_reports_full_elapsed():
    session = LiveFiveMinuteSession([], at(15, 0), config.BIN_SIZE, config.VALUE_AREA_PCT,
                                     ema_period=config.EMA_PERIOD)

    snap = session.snapshot()

    assert snap["current_candle"] is None
    assert snap["elapsed_seconds"] == config.BAR_SECONDS
    print("PASS: an empty session reports full elapsed and no candle\n")


def test_no_false_high_at_a_bar_boundary_end_to_end():
    """Integration proof: drive a real session through a close where the bar's
    volume equals the average, then run the snapshot through the Signal and
    Decision engines. Volume must read Normal, not High."""
    from signal_engine import IndicatorSnapshot, SignalEngine, VolumeState

    session = make_session()
    session.apply_tick(57900.0, 500_000.0, 2_002_000.0, at(15, 0, 10))
    # Accumulate exactly one average bar's worth of volume.
    session.apply_tick(57905.0, 500_000.0 + TODAY_BAR_VOLUME, 2_002_100.0, at(15, 4, 50))

    snap = session.advance(at(15, 5, 1))

    state, detail = SignalEngine()._evaluate_volume(
        snap["current_candle"],
        IndicatorSnapshot(sma20_volume=snap["average_bar_volume"],
                          elapsed_seconds=snap["elapsed_seconds"]),
    )
    assert state == VolumeState.NORMAL, f"boundary tick read {state}: {detail}"
    print("PASS: no false High at a bar boundary, end to end\n")


def test_session_without_history_accepts_first_tick():
    """No seeded bars means no previously-closed bucket to clamp against."""
    session = LiveFiveMinuteSession([], at(15, 0), config.BIN_SIZE, config.VALUE_AREA_PCT,
                                     ema_period=config.EMA_PERIOD)

    session.apply_tick(57900.0, 100.0, 2_000_000.0, at(15, 0, 5))

    assert session.current is not None
    assert session.current.start == five_minute_start(at(15, 0, 5))
    print("PASS: a session with no history accepts its first tick\n")


# ---------------------------------------------------------------
# Session date anchor
#
# The seed/completed split keeps prior-session bars out of VWAP and the
# profile, but only for bars handed in at construction. A session that
# OUTLIVES its day — the service running through a night or a weekend
# without the morning restart — just keeps appending to self.completed.
# ---------------------------------------------------------------
def test_session_records_the_date_it_was_built_for():
    session = make_session(now=at(15, 0))

    assert session.session_date == date(2026, 8, 3)
    print("PASS: the session records its trading date\n")


def test_session_is_not_stale_on_the_same_day():
    """The guard must not fire during a normal session, or it would tear the
    feed down mid-market."""
    session = make_session(now=at(9, 20))

    assert session.is_from_a_previous_day(at(9, 20)) is False
    assert session.is_from_a_previous_day(at(15, 29, 59)) is False
    print("PASS: a session is not stale within its own day\n")


def test_session_is_stale_on_any_later_day():
    """Both the overnight case and the Friday-to-Monday gap."""
    overnight = make_session(now=at(15, 0))
    assert overnight.is_from_a_previous_day(datetime(2026, 8, 4, 9, 20, tzinfo=IST)) is True

    friday = LiveFiveMinuteSession([], datetime(2026, 8, 7, 15, 0, tzinfo=IST),
                                    config.BIN_SIZE, config.VALUE_AREA_PCT,
                                    ema_period=config.EMA_PERIOD)
    assert friday.is_from_a_previous_day(datetime(2026, 8, 10, 9, 20, tzinfo=IST)) is True
    print("PASS: a session is stale the next day and across a weekend\n")


def test_ticks_from_a_later_day_contaminate_the_profile():
    """Characterization — this documents the bug the anchor exists to prevent,
    so it asserts the BROKEN behaviour deliberately.

    Nothing raises. self.completed simply accumulates across the boundary, so
    the volume profile spans two days and VWAP stays cumulative from the first
    — plausible, confident, wrong numbers that alerts then fire on. The fix is
    that callers never let it get here; they rebuild instead.
    """
    session = make_session(now=at(15, 0))
    volume_on_day_one = session.snapshot()["result"].total_volume

    next_day = datetime(2026, 8, 4, 9, 20, tzinfo=IST)
    session.advance(next_day)
    session.apply_tick(58200.0, 900_000.0, 2_100_000.0, next_day)
    session.apply_tick(58250.0, 903_000.0, 2_100_500.0, next_day + timedelta(seconds=30))
    session.advance(next_day + timedelta(minutes=6))

    days_in_profile = {bar.start.date() for bar in session.completed}
    assert len(days_in_profile) == 2, "completed spans two days — that IS the defect"
    assert session.snapshot()["result"].total_volume > volume_on_day_one

    # ...and the session itself can say so, which is what the caller acts on.
    assert session.is_from_a_previous_day(next_day) is True
    print("PASS: an outlived session mixes two days (documented, guarded against)\n")


if __name__ == "__main__":
    test_bar_start_is_the_bar_handed_out_not_the_wall_clock()
    test_bar_start_is_none_before_any_bar_exists()
    test_late_tick_does_not_reopen_closed_bucket()
    test_ema_takes_one_step_per_bar()
    test_sma20_volume_not_diluted_by_late_ticks()
    test_profile_total_volume_stable_across_boundary()
    test_normal_rollover_still_closes_one_bar()
    test_tick_two_buckets_ahead_opens_correct_bucket()
    test_repeated_advance_within_bucket_is_noop()
    test_slope_is_positive_while_price_rises()
    test_slope_is_negative_while_price_falls()
    test_slope_does_not_flip_on_chop_around_the_closed_ema()
    test_slope_unknown_until_the_lookback_window_fills()
    test_lookback_window_is_time_based_not_sample_count()
    test_ema_history_does_not_grow_unbounded()
    test_snapshot_candle_is_an_immutable_copy()
    test_snapshot_volume_fields_agree()
    test_ema_is_not_todays_first_bar_close()
    test_volume_baseline_is_full_from_the_first_bar()
    test_vwap_excludes_seed_bars()
    test_volume_profile_excludes_seed_bars()
    test_oi_pattern_never_spans_the_overnight_gap()
    test_empty_seed_reproduces_previous_behaviour()
    test_fewer_seed_bars_than_lookback_is_safe()
    test_forming_bar_reports_true_partial_elapsed()
    test_completed_bar_reports_full_elapsed_not_clock_age()
    test_feed_stall_keeps_elapsed_at_full_bar()
    test_no_bars_at_all_reports_full_elapsed()
    test_no_false_high_at_a_bar_boundary_end_to_end()
    test_session_without_history_accepts_first_tick()
    test_session_records_the_date_it_was_built_for()
    test_session_is_not_stale_on_the_same_day()
    test_session_is_stale_on_any_later_day()
    test_ticks_from_a_later_day_contaminate_the_profile()
    print("All tests passed.")
