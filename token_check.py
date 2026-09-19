#!/usr/bin/env python3
"""
token_check.py — pre-open reminder. Is there a usable Kite token for today?

Runs at 08:30 and 09:00 on weekdays from kite-tokencheck.timer. Silent when the
token is good; Telegrams you when it is not, while there is still time to fix it.

This is the only check in the set that recovers trading time rather than
explaining its loss. Everything else tells you afterwards that the session was
lost; this one prevents it.

Why try_cached_session and not a file check: it calls kite.profile(), so it is a
LIVE probe. A cache file dated today can still hold a token Kite has already
expired — exactly what happens to a token minted overnight, since Kite retires
them each morning well before the open. A mtime or date test would call that
healthy and you would find out at 09:15.

Exit codes: 0 usable token (or weekend/outside the window), 1 no usable token.
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from healthcheck import notify  # one Telegram implementation, not two
from kite_auth import try_cached_session

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    if not config.KITE_API_KEY:
        notify("KITE_API_KEY missing from .env — the scanner cannot start")
        return 1

    if try_cached_session(config.KITE_API_KEY) is not None:
        return 0

    now = datetime.now(IST)
    open_at = now.replace(hour=config.MARKET_OPEN_HOUR,
                          minute=config.MARKET_OPEN_MINUTE, second=0, microsecond=0)
    minutes_left = int((open_at - now).total_seconds() // 60)
    when = f"{minutes_left} min to the open" if minutes_left > 0 else "market is already open"

    notify(f"NO usable Kite token for today ({when}). "
           f"Run: ssh in, cd ~/kite-scanner, python3 token_helper.py")
    return 1


if __name__ == "__main__":
    # Same reasoning as healthcheck.py: an unexpected exception here would be a
    # silent non-zero exit, and a reminder that fails quietly is not a reminder.
    try:
        sys.exit(main())
    except Exception as e:
        notify(f"token_check itself failed: {type(e).__name__}: {e}")
        sys.exit(1)
