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


def _load_state(today: str, path: str = STATE_FILE) -> str:
    """'down' only if we already alerted TODAY; anything else reads as healthy.

    `path` defaults to the engine's own STATE_FILE so every existing call
    site is unchanged; order_watch's probe passes ORDER_WATCH_STATE_FILE so
    the two edge-triggers never share — and can never clobber — one flag.
    """
    try:
        with open(path) as f:
            state, _, when = f.read().strip().partition(" ")
    except OSError:
        return "ok"
    return "down" if state == "down" and when == today else "ok"


def _save_state(state: str, today: str, path: str = STATE_FILE) -> None:
    try:
        with open(path, "w") as f:
            f.write(f"{state} {today}")
    except OSError:
        pass  # see notify(): an unwritable state file must never suppress an alert


def _check_order_watch(now: datetime) -> int:
    """Probes `systemctl --user is-active kite-order-watch`.

    Deliberately called from main() ABOVE the is_market_hours() early return
    below — order_watch.py's whole point is detecting orders placed OUTSIDE
    market hours too (an AMO queues fine on a closed market, per STATUS in
    Markdowns/pending_order_black76_telegram_latency_test_v2.md), so a check
    that only ran during market hours would miss exactly the case that
    matters most: the service dying overnight before a morning AMO.

    No systemctl on this machine (e.g. a dev workstation, not the VM) is not
    a failure to alert on — it means this probe cannot answer the question
    here, so it says nothing rather than firing a false "down".
    """
    today = now.strftime("%Y-%m-%d")
    already_alerted = _load_state(today, ORDER_WATCH_STATE_FILE) == "down"

    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", ORDER_WATCH_SERVICE],
            capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return 0  # no systemctl here — nothing this probe can determine
    except subprocess.TimeoutExpired:
        # A hung systemctl call is itself worth surfacing, same edge-trigger
        # shape as a genuine "not active" — but never crash the rest of the
        # script over it.
        if not already_alerted:
            notify(f"{ORDER_WATCH_SERVICE}: systemctl did not respond within 10s")
            _save_state("down", today, ORDER_WATCH_STATE_FILE)
        return 1

    active = result.stdout.strip() == "active"

    if not active:
        if not already_alerted:
            notify(f"{ORDER_WATCH_SERVICE} is not running "
                  f"(systemctl reports: {result.stdout.strip() or 'unknown'})")
            _save_state("down", today, ORDER_WATCH_STATE_FILE)
        return 1

    if already_alerted:
        notify(f"recovered — {ORDER_WATCH_SERVICE} is active again")
        _save_state("ok", today, ORDER_WATCH_STATE_FILE)
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
