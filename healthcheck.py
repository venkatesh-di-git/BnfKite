#!/usr/bin/env python3
"""
healthcheck.py — proves the ENGINE is running, not just that a process is.

Run from a systemd timer, never from inside the scanner: a probe living in the
process it checks cannot report that process wedged.

Background. On 10 Aug the scanner ran 14 hours and produced nothing — app.py
drives its engine from ui.timer, which is client-bound, so with no browser
attached every timer was cancelled at startup. Each check we had said healthy:
systemctl active, bound to 127.0.0.1:8080, HTTP 200, clean journal. All true,
all describing a *serving* process rather than a *running* one.

What it probes, and why this file. latest_volume_profile.json is rewritten by
write_output() every LOG_WRITE_INTERVAL_SECONDS whenever a result exists — an
unconditional heartbeat. signal_log.csv is the obvious candidate and the wrong
one: it is change-keyed, so a row appears only when one of the 18 STATE_FIELDS
moves. A genuinely quiet market writes nothing for minutes, the probe cries
wolf, and you learn to ignore it.

Exit codes: 0 healthy or outside market hours, 1 anything else.

Known blind spot, stated because it is real: this cannot see an UNDER-SEEDED
session. write_output still fires on schedule, so mtime stays fresh while the
volume gate measures against a one-bar average — fresh output, wrong content.
main.py now waits for market hours before seeding, which makes that structurally
impossible rather than merely detectable; the seed-count line in the journal is
what confirms it.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config
from engine import is_market_hours
from kite_auth import has_token_for_today

IST = ZoneInfo("Asia/Kolkata")

# Six missed writes at the 30s cadence. Long enough to ride out a slow REST
# bootstrap or a brief tick lull, short enough that a dead engine is caught
# within one timer interval of going quiet.
STALE_SECONDS = 180

# Edge-triggered alerting. Firing every 5 minutes for a whole session is ~75
# identical messages, which trains you to mute the bot — and then the next real
# failure reaches nobody. One message when it breaks, one when it recovers.
#
# The date key is load-bearing. A bare "down" flag that only clears on success
# never clears at all when a session is lost outright: the next day's failure
# sees "down" already set and says nothing, so the detector permanently mutes
# itself after its first unrecovered incident. Keying by session date guarantees
# at least one alert per trading day, and unlike clearing in the outside-hours
# branch it survives the VM being off overnight or the timer missing runs.
STATE_FILE = os.path.join(config.CSV_DIR, ".healthcheck_state")

# order_watch.py's own state file — deliberately SEPARATE from STATE_FILE
# above. That file holds one flag for the engine check; a second
# edge-triggered condition sharing it would clobber the engine's flag and
# break the once-per-day guarantee the date key exists to provide.
ORDER_WATCH_STATE_FILE = os.path.join(config.CSV_DIR, ".healthcheck_order_watch_state")
ORDER_WATCH_SERVICE = "kite-order-watch"

# Grace after the open, before the heartbeat can exist at all.
#
# write_output() only runs once a bar CLOSES, so the first write of the day
# lands at 09:20 — the close of the 09:15 bar. Between 09:15 and then the file
# legitimately does not exist, and probing it reports a healthy engine as dead.
# Measured on 11 Aug: alert at 09:15:28, "recovered" at 09:20:27. Two false
# Telegrams every morning is exactly the fatigue the edge-triggering exists to
# prevent.
#
# One bar plus a margin for the REST bootstrap. Long enough to clear the first
# close, short enough that a genuinely dead engine is still caught within the
# first ten minutes.
OPEN_GRACE_SECONDS = 600


def _load_state(today: str) -> str:
    """'down' only if we already alerted TODAY; anything else reads as healthy."""
    try:
        with open(STATE_FILE) as f:
            state, _, when = f.read().strip().partition(" ")
    except OSError:
        return "ok"
    return "down" if state == "down" and when == today else "ok"


def _save_state(state: str, today: str) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            f.write(f"{state} {today}")
    except OSError:
        pass  # see notify(): an unwritable state file must never suppress an alert


def _load_watch_state(today: str):
    """Returns (state, nrestarts) for order-watch, or ('ok', None) if the file
    is absent or from another day.

    Separate from _load_state above rather than a shared helper with a path
    argument: this one carries a second field the engine check has no use for,
    and the engine check is the load-bearing one — it must not acquire new
    parsing paths to serve this probe.

    nrestarts is kept as the raw string; it is only ever compared for equality
    against the next probe's value, never arithmetic.
    """
    try:
        with open(ORDER_WATCH_STATE_FILE) as f:
            parts = f.read().strip().split()
    except OSError:
        return "ok", None
    if len(parts) < 2 or parts[1] != today:
        return "ok", None
    return parts[0], (parts[2] if len(parts) > 2 else None)


def _save_watch_state(state: str, today: str, nrestarts) -> None:
    try:
        with open(ORDER_WATCH_STATE_FILE, "w") as f:
            line = f"{state} {today}"
            if nrestarts is not None:
                line += f" {nrestarts}"
            f.write(line)
    except OSError:
        pass  # see notify(): an unwritable state file must never suppress an alert


def _check_order_watch(now: datetime) -> int:
    """Probes kite-order-watch, edge-triggered, ONE message per transition.

    Deliberately called from main() ABOVE the is_market_hours() early return
    below — order_watch.py's whole point is detecting orders placed OUTSIDE
    market hours too (an AMO queues fine on a closed market), so a check that
    only ran during market hours would miss exactly the case that matters
    most: the service dying overnight before a morning AMO.

    Two things this must NOT do, both learned from Sat 26 Sep, when it sent 24
    Telegrams on a closed market.

    It must not alert when order-watch CANNOT run. Without today's token
    order_watch.py exits 1 at startup by design, so on any non-trading day it
    crash-loops forever — expected, not a fault. The gate is the token cache
    rather than a weekday/market-hours test on purpose: is_market_hours() only
    knows weekends, and this codebase deliberately carries no NSE holiday
    calendar (see config.py's SEED_LOOKBACK_DAYS note), so a weekday gate
    would still fire all through Diwali. kite-tokencheck.timer already owns
    the "you have no token" alert, so nothing is lost by staying quiet here.

    And it must not read a crash loop as health. `systemctl is-active` returns
    "activating" for ~30 of every 31 seconds of a RestartSec=30 loop and
    briefly "active" in between, so a single binary is-active check sampled
    every 5 minutes reported down -> recovered -> down -> recovered
    indefinitely. Hence SubState, and hence comparing NRestarts against the
    previous probe: a unit that restarted since we last looked is not healthy,
    whatever it happens to report this instant.

    No systemctl on this machine (e.g. a dev workstation, not the VM) is not a
    failure to alert on — it means this probe cannot answer the question here,
    so it says nothing rather than firing a false "down".
    """
    today = now.strftime("%Y-%m-%d")

    if not has_token_for_today(config.KITE_API_KEY):
        return 0  # no session today: order-watch cannot run, and that is not a fault

    prev_state, prev_restarts = _load_watch_state(today)
    already_alerted = prev_state == "down"

    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", ORDER_WATCH_SERVICE,
             "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return 0  # no systemctl here — nothing this probe can determine
    except subprocess.TimeoutExpired:
        # A hung systemctl call is itself worth surfacing, same edge-trigger
        # shape as a genuine "not active" — but never crash the rest of the
        # script over it.
        if not already_alerted:
            notify(f"{ORDER_WATCH_SERVICE}: systemctl did not respond within 10s")
            _save_watch_state("down", today, prev_restarts)
        return 1

    props = dict(line.split("=", 1)
                 for line in result.stdout.strip().splitlines() if "=" in line)
    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "unknown")
    restarts = props.get("NRestarts")

    # A restart between two probes means it is looping, even if this sample
    # caught the brief running window. Unknown on the first probe of a day,
    # which is harmless: a real loop reports auto-restart almost every time.
    restarted_since_last_probe = (prev_restarts is not None
                                  and restarts is not None
                                  and restarts != prev_restarts)
    stable = active_state == "active" and sub_state == "running"

    if not stable or restarted_since_last_probe:
        if not already_alerted:
            if restarted_since_last_probe or sub_state == "auto-restart":
                notify(f"{ORDER_WATCH_SERVICE} is crash-looping "
                      f"({restarts} restarts) — {active_state}/{sub_state}")
            else:
                notify(f"{ORDER_WATCH_SERVICE} is not running "
                      f"— {active_state}/{sub_state}")
            _save_watch_state("down", today, restarts)
        else:
            # Still down, no new message — but keep the counter current so the
            # next probe compares against this sample, not a stale one.
            _save_watch_state("down", today, restarts)
        return 1

    if already_alerted:
        notify(f"recovered — {ORDER_WATCH_SERVICE} is running again")
    _save_watch_state("ok", today, restarts)
    return 0


def notify(text: str) -> None:
    """Best-effort Telegram. Never raises — a delivery failure must not stop the
    caller printing and exiting non-zero, or the failure disappears twice over."""
    print(text)
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("  (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset — no message sent)")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": f"SCANNER: {text}"},
            timeout=5,
        )
    except Exception as e:
        print(f"  (Telegram delivery failed: {e})")


def main() -> int:
    now = datetime.now(IST)

    # ABOVE the market-hours gate below, on purpose — see _check_order_watch's
    # docstring. This probe must run every invocation, not just during market
    # hours, or it would miss the service dying exactly when an AMO needs it.
    order_watch_status = _check_order_watch(now)

    if not is_market_hours(now):
        return order_watch_status

    # Silent during the grace window: no bar has closed yet, so there is nothing
    # to probe. Deliberately returns 0 rather than skipping the state handling —
    # a "down" flag set yesterday should not be cleared by a window in which we
    # learned nothing, and a real failure here is caught minutes later anyway.
    open_at = now.replace(hour=config.MARKET_OPEN_HOUR,
                          minute=config.MARKET_OPEN_MINUTE, second=0, microsecond=0)
    if (now - open_at).total_seconds() < OPEN_GRACE_SECONDS:
        return order_watch_status

    today = now.strftime("%Y-%m-%d")
    already_alerted = _load_state(today) == "down"
    # LIVE_OUTPUT_FILE, never OUTPUT_FILE: under BN_DRY_RUN the latter moves into
    # csv-dryrun/, so a probe reading it would be satisfied by dry-run output.
    # This one is immune to the flag by construction, which means the guard does
    # not depend on this script running as a separate systemd unit that happens
    # not to inherit the scanner's environment.
    path = config.LIVE_OUTPUT_FILE
    name = os.path.basename(path)

    # Absence is its own alerting case, not a crash. A bare os.stat() raises
    # FileNotFoundError; unhandled that exits non-zero with NO Telegram, so the
    # detector goes silent in exactly the 10 Aug scenario — where the file had
    # never been written at all.
    try:
        age = time.time() - os.path.getmtime(path)
        problem = f"engine stalled — {name} last written {age:.0f}s ago" if age > STALE_SECONDS else None
    except FileNotFoundError:
        problem = f"engine has never written {name} — not running"

    if problem:
        if not already_alerted:
            notify(problem)
            _save_state("down", today)
        return 1  # engine problem always exits 1 regardless of order_watch_status

    if already_alerted:
        # Worth its own message: it confirms token_helper.py worked without you
        # having to go and look.
        notify(f"recovered — {name} is being written again")
        _save_state("ok", today)
    # Engine is healthy — the exit code still reflects order_watch_status, so a
    # dead order-watch service doesn't disappear just because the engine is fine.
    return order_watch_status


if __name__ == "__main__":
    # Belt and braces: an unexpected exception here would otherwise be a silent
    # non-zero exit, which is the one outcome a detector must never have.
    try:
        sys.exit(main())
    except Exception as e:
        notify(f"healthcheck itself failed: {type(e).__name__}: {e}")
        sys.exit(1)
