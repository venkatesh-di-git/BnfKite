"""
main.py — headless CLI runner. This is what runs on the VM.

Owns no engine logic of its own: the session, feed, signal/decision/alert
engines and logging all live in session_runner.SessionRunner, shared with
app.py. That module never imports NiceGUI, which is what stops engine work
drifting back inside a browser-dependent timer — the 10 Aug failure, where the
scanner ran 14 hours and produced nothing while every check reported healthy.

This file is now just: authenticate, resolve the contract, wait for the open,
then loop calling the runner and printing what it did.

Run with:  python main.py
Stop with: Ctrl+C
"""

import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from kite_auth import NoTokenError, get_authenticated_kite
from instruments import get_current_month_contract
from engine import is_market_hours
from session_runner import SessionRunner

IST = ZoneInfo("Asia/Kolkata")


def main():
    if config.DRY_RUN:
        print("=" * 70)
        print("  DRY RUN — no Telegram, output redirected to csv-dryrun/")
        print("=" * 70)

    if not config.KITE_API_KEY or not config.KITE_API_SECRET:
        print("ERROR: Set KITE_API_KEY and KITE_API_SECRET environment variables first.")
        print("Get these from your app at https://developers.kite.trade")
        sys.exit(1)

    kite = get_authenticated_kite(config.KITE_API_KEY, config.KITE_API_SECRET)

    contract = get_current_month_contract(kite)
    if contract is None:
        print("ERROR: Could not resolve a live Bank Nifty futures contract.")
        sys.exit(1)

    print(f"Tracking: {contract.tradingsymbol} (expires {contract.expiry})")
    print(f"Bin size: {config.BIN_SIZE} pts | Value area: {config.VALUE_AREA_PCT:.0%} | "
          f"EMA period: {config.EMA_PERIOD}\n")

    runner = SessionRunner()
    runner.state["kite"] = kite
    runner.state["contract"] = contract
    state = runner.state

    # Wait for the open BEFORE seeding. This used to seed immediately and fall
    # back to ([], []) outside market hours — so a process started at, say,
    # 00:15 built an empty session, slept until 09:15, and ran the whole day
    # unseeded: EMA10 equal to the first bar's close and relative volume pinned
    # near 1.00 until the 20th bar completed around 10:55. Nothing errored, and
    # latest_volume_profile.json still updated on schedule, so the freshness
    # probe reported healthy throughout.
    #
    # Sleeping TO the bell rather than a flat 300s. The flat interval put seeding
    # wherever the service's start offset happened to land: on 12 Aug the journal
    # reads "09:12:39 waiting to seed" then "09:17:39 Seeded" — 2m39s of the open
    # missed. That is the expensive window. Across 11 and 12 Aug, alerts in the
    # first 15 minutes went 5/5 for +938.6 points; the rest of both sessions went
    # 9/25 for -901.4.
    #
    # remaining <= 0 covers after-close and weekends, where is_market_hours() is
    # false for reasons the time-to-open figure does not describe. The 1s floor
    # stops a spin if the sleep lands a few milliseconds early. Waking exactly at
    # 09:15:00 is safe: ensure_started() is retried every loop, so a bootstrap
    # that throws because history is not published yet simply succeeds 2s later.
    while not is_market_hours(now := datetime.now(IST)):
        opens_at = now.replace(hour=config.MARKET_OPEN_HOUR, minute=config.MARKET_OPEN_MINUTE,
                               second=0, microsecond=0)
        remaining = (opens_at - now).total_seconds()
        delay = 300.0 if remaining <= 0 else max(1.0, min(300.0, remaining))
        print(f"[{now.strftime('%H:%M:%S')}] Outside market hours — waiting to seed.")
        time.sleep(delay)

    # state["current_alert"] holds the latest alert indefinitely; this tracks
    # which one has already been printed so the console doesn't repeat it every
    # two seconds. Telegram de-dupes on its own via the alert latch.
    last_alert_seen = None

    # On the RECURRING line, not just the startup banner. This is what the VM
    # runs, and under `journalctl -f` the head of the log scrolls away in
    # seconds — the line you read at 09:20 is the tail. Empty when live, so the
    # normal output format is unchanged.
    tag = " DRY" if config.DRY_RUN else ""

    try:
        while True:
            now = datetime.now(IST)

            if not is_market_hours(now):
                session = state["live_session"]
                # A session must never outlive the day it was seeded for.
                # Exiting rather than rebuilding in place: systemd restarts and
                # the wait-then-seed above reseeds cleanly. Fired past 08:00 and
                # NOT at 09:15, because exiting at the bell would restart during
                # the session and lose the open — those ticks are unrecoverable
                # from 5-minute history until the 09:20 bar closes.
                #
                # sys.exit, not return: __main__ discards main()'s return value,
                # so a return would exit 0 and Restart=on-failure would never
                # fire. SystemExit still unwinds through the finally below.
                if session and session.is_from_a_previous_day(now) and now.hour >= 8:
                    print("Session was built for a previous day — exiting for a clean restart.")
                    sys.exit(1)
                print(f"[{now.strftime('%H:%M:%S')}] Outside market hours — sleeping 5 min.")
                time.sleep(300)
                continue

            # Everything the engine does — seed, advance, write, staleness, feed
            # rebuild, signals, alerts, logging — is these two calls.
            runner.ensure_started(now)
            runner.tick(now)

            for message, _kind in runner.drain_notices():
                print(f"    {message}")

            if state["result"] is not None:
                vwap_s = f"{state['vwap']:.2f}" if state["vwap"] is not None else "—"
                ema_s = f"{state['ema_10']:.2f}" if state["ema_10"] is not None else "—"
                oi_s = f"{state['oi']:.0f}" if state["oi"] is not None else "—"
                print(f"[{now.strftime('%H:%M:%S')}]{tag} {contract.tradingsymbol}  "
                      f"LTP={state['current_price']}  VWAP={vwap_s}  EMA10={ema_s}  "
                      f"POC={state['result'].poc}  VAH={state['result'].vah}  VAL={state['result'].val}  "
                      f"OI={oi_s} ({state['oi_pattern']})  bars={state['candle_count']}")

                snapshot, decision = state["signal_snapshot"], state["decision"]
                if snapshot and decision:
                    print(f"    {decision.status.value} — {decision.direction.value} ({decision.grade.value})  "
                          f"Trend={snapshot.trend.value} Pullback={snapshot.pullback.value} "
                          f"Rejection={snapshot.rejection.value} Volume={snapshot.volume.value} "
                          f"OI={snapshot.open_interest.value} "
                          f"POC={snapshot.poc.value} VAH={snapshot.vah.value} VAL={snapshot.val.value}")

                alert = state["current_alert"]
                if alert and alert is not last_alert_seen:
                    print(f"    *** ALERT [{alert.grade} {alert.direction}] @ {alert.current_price} "
                          f"(confidence {alert.confidence}%) ***")
                    last_alert_seen = alert
            elif state["error"]:
                print(f"[{now.strftime('%H:%M:%S')}]{tag} {state['error']}")
            else:
                print(f"[{now.strftime('%H:%M:%S')}]{tag} Waiting for first completed bar...")

            time.sleep(config.SNAPSHOT_INTERVAL_SECONDS)

    finally:
        # Drains queued Telegram deliveries before exiting.
        runner.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except NoTokenError as e:
        # Exit 78, listed under RestartPreventExitStatus in the unit, so systemd
        # stops instead of retrying. Retrying achieves nothing: only
        # token_helper.py can fix it, and that script restarts the service
        # itself. Without this the scanner burns ~750 restarts across a session,
        # recovering nothing. The healthcheck still alerts, so a stopped service
        # is noticed rather than silently accepted.
        print(e)
        sys.exit(78)
