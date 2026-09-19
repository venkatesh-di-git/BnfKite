#!/usr/bin/env python3
"""
telegram_poller.py — accept the daily Kite login over Telegram.

WHY THIS EXISTS. Kite sessions expire every morning around 06:00 IST. Until
someone runs token_helper.py over SSH, the scanner exits 78 and systemd
deliberately stops retrying (RestartPreventExitStatus=78) because no amount of
restarting can invent a token. Measured cost of that gap: the scanner sat failed
from Wed 02 Sep 08:00 to Sat 05 Sep — three trading days lost to a login nobody
was at a terminal to perform.

WHAT IT DOES. Long-polls getUpdates for exactly one command, `/login <token>`,
from exactly one chat. No webhook, so nothing listens on a port.

WHAT IT DOES NOT DO. No command changes config, thresholds, or grades, and none
places an order. The single exception to "query and exchange only" is the
scanner restart inside /login — writing the token cache does not fix a scanner
that is already dead or holding a stale KiteConnect, and a /login that leaves
you with no scanner has not solved the problem it exists to solve. That restart
is bounded to `systemctl --user restart kite-scanner` and reachable no other
way.

SECURITY. The access token never travels over Telegram — only the request
token, which is single-use and already spent by the time it arrives. The API
secret stays on the VM. Every update is checked against TELEGRAM_CHAT_ID before
anything is dispatched, and non-matching senders get no reply at all: an error
message is itself a signal that something is listening.

Run as its own systemd service, independent of kite-scanner, so a crash here
can never take the scanner with it.
"""

import logging
import subprocess
import sys
import time

import requests

import config
from kite_auth import complete_login, try_cached_session

logger = logging.getLogger("telegram_poller")

API = "https://api.telegram.org/bot{token}/{method}"

# Long-poll duration. The read timeout must exceed it or every poll ends in a
# spurious ReadTimeout and the loop degenerates into a busy retry.
POLL_SECONDS = 30
HTTP_TIMEOUT = (10, POLL_SECONDS + 10)

# Backoff after a transport failure, so a network blip or a Telegram outage does
# not become a hot loop against their API.
ERROR_BACKOFF_SECONDS = 15

SERVICE = "kite-scanner"  # same unit token_helper.py restarts


def validated_chat_id(raw):
    """The allowlist value, or None if it is not a usable chat id.

    NOT cosmetic. This was found in production: the VM's .env held a
    `TELEGRAM_CHAT_ID` with a heredoc terminator concatenated onto the end
    (e.g. `123456789EOF`) when the file was written. Telegram itself is
    lenient about it (it parses the leading integer and ignores the rest, so
    outbound alerts were unaffected), which is exactly why it survived
    unnoticed for so long.

    But this allowlist compares strings, and "123456789" != "123456789EOF", so
    every /login would have been silently discarded while `systemctl is-active`
    reported the service healthy.

    Refusing to start is deliberate. The alternative — normalising the value
    until it matches — makes an allowlist fuzzy, which is the wrong direction
    for the one check standing between a stranger and a scanner restart.
    """
    cid = str(raw).strip()
    try:
        int(cid)          # negative for groups/supergroups, positive for a DM
    except ValueError:
        return None
    return cid


def _call(method: str, **params):
    """One Telegram API call. Returns the `result` payload, or None."""
    r = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                      json=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        logger.warning("telegram %s not ok: %s", method, body.get("description"))
        return None
    return body.get("result")


def _reply(text: str) -> None:
    """Best-effort. A failed reply must never abort a login that succeeded."""
    try:
        _call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        logger.warning("reply failed: %s: %s", type(e).__name__, e)


def _delete(message_id: int) -> None:
    """Clear the request token from chat history.

    Cosmetic, not a security control: the token is single-use and already spent
    by the time this runs. Permitted — the Bot API lets a bot delete incoming
    messages in a private chat — but it fails harmlessly if that ever changes.
    """
    try:
        _call("deleteMessage", chat_id=config.TELEGRAM_CHAT_ID,
              message_id=message_id)
    except Exception as e:
        logger.debug("could not delete message %s: %s", message_id, e)


def _restart_scanner() -> bool:
    """Restart, then confirm it actually came up.

    The confirmation is the point. Restarting is easy; the failure this feature
    exists to end is a scanner that is DOWN, so replying "logged in" while the
    unit sits in failed would recreate the same silence with extra steps. A
    short settle first, because systemd reports the unit active the instant exec
    succeeds, and main.py can still exit 78 a second later.
    """
    try:
        subprocess.run(["systemctl", "--user", "restart", SERVICE],
                       check=True, timeout=60)
    except Exception as e:
        logger.warning("restart failed: %s: %s", type(e).__name__, e)
        return False
    time.sleep(3)
    probe = subprocess.run(["systemctl", "--user", "is-active", SERVICE],
                           capture_output=True, text=True)
    return probe.stdout.strip() == "active"


def handle_login(request_token: str) -> str:
    """Exchange, verify, restart. Returns the line to send back."""
    if not request_token:
        return "❌ Usage: /login <request_token>"

    try:
        # complete_login writes the session cache itself, so it stays the single
        # writer of a format the scanner has to agree with (kite_auth.py:113).
        complete_login(config.KITE_API_KEY, config.KITE_API_SECRET, request_token)
    except Exception as e:
        # A request token is single-use and expires in minutes, so re-sending
        # the same one always fails — say so rather than leaving you retrying.
        return (f"❌ Kite rejected the request token: {e}\n"
                "They are single-use and expire within minutes — log in again "
                "for a fresh one.")

    # Kite accepting the token is NOT the same as the scanner being able to read
    # it back. Restarting before checking is how a format or api_key mismatch
    # becomes a silently unauthenticated scanner at 09:16 — the check
    # token_helper.py:65 exists for.
    if try_cached_session(config.KITE_API_KEY) is None:
        return "❌ Token saved but the scanner cannot read it back. Not restarting."

    if not _restart_scanner():
        return ("⚠️ Logged in, but the scanner did not come up. "
                "Check: systemctl --user status kite-scanner")
    return "✅ Logged in — scanner restarted and running."


def _handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    # THE ALLOWLIST, before any dispatch. Silence, not an error reply: a reply
    # tells an unknown sender that something is here and listening.
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id != validated_chat_id(config.TELEGRAM_CHAT_ID):
        logger.warning("ignored message from chat %s", chat_id)
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/login"):
        return  # the only command there is

    # Delete FIRST. If the exchange raises, the token is still spent and should
    # not be left sitting in the chat.
    _delete(message.get("message_id"))
    _reply(handle_login(text[len("/login"):].strip()))


def _initial_offset() -> int:
    """Skip whatever is already queued at startup.

    Without this, a restart replays the backlog: an hour-old /login would be
    re-processed, spending a dead token and triggering a pointless scanner
    restart. offset=-1 asks for the most recent update only, so we learn where
    'now' is and begin after it.
    """
    try:
        result = _call("getUpdates", offset=-1, timeout=0) or []
        if result:
            return result[-1]["update_id"] + 1
    except Exception as e:
        logger.warning("could not read initial offset: %s", e)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — set them in .env.")
        return 1
    if not config.KITE_API_KEY or not config.KITE_API_SECRET:
        print("KITE_API_KEY / KITE_API_SECRET missing — set them in .env.")
        return 1

    # Exit rather than run: a poller whose allowlist can never match is worse
    # than one that is stopped, because systemctl reports it active and /login
    # simply goes unanswered with nothing in the log to explain why.
    if validated_chat_id(config.TELEGRAM_CHAT_ID) is None:
        print(f"TELEGRAM_CHAT_ID is not a valid chat id: "
              f"{config.TELEGRAM_CHAT_ID!r}. Expected digits only (negative for "
              f"groups). Check .env for stray characters.")
        return 1

    offset = _initial_offset()
    logger.info("polling from offset %s; only /login from chat %s is accepted",
                offset, config.TELEGRAM_CHAT_ID)

    while True:
        try:
            updates = _call("getUpdates", offset=offset,
                            timeout=POLL_SECONDS) or []
            for update in updates:
                offset = update["update_id"] + 1
                # One bad update must not stop the loop, or a malformed message
                # silences the login path until someone notices the service.
                try:
                    _handle_update(update)
                except Exception as e:
                    logger.exception("update %s failed: %s", update.get("update_id"), e)
        except Exception as e:
            logger.warning("poll failed (%s: %s) — retrying in %ss",
                           type(e).__name__, e, ERROR_BACKOFF_SECONDS)
            time.sleep(ERROR_BACKOFF_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
