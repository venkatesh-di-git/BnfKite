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
    if not is_market_hours(now):
        return 0

    # Silent during the grace window: no bar has closed yet, so there is nothing
    # to probe. Deliberately returns 0 rather than skipping the state handling —
    # a "down" flag set yesterday should not be cleared by a window in which we
    # learned nothing, and a real failure here is caught minutes later anyway.
    open_at = now.replace(hour=config.MARKET_OPEN_HOUR,
                          minute=config.MARKET_OPEN_MINUTE, second=0, microsecond=0)
    if (now - open_at).total_seconds() < OPEN_GRACE_SECONDS:
        return 0

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
        return 1

    if already_alerted:
        # Worth its own message: it confirms token_helper.py worked without you
        # having to go and look.
        notify(f"recovered — {name} is being written again")
        _save_state("ok", today)
    return 0


if __name__ == "__main__":
    # Belt and braces: an unexpected exception here would otherwise be a silent
    # non-zero exit, which is the one outcome a detector must never have.
    try:
        sys.exit(main())
    except Exception as e:
        notify(f"healthcheck itself failed: {type(e).__name__}: {e}")
        sys.exit(1)
