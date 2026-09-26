"""
kite_auth.py

Handles the official Kite Connect login flow. Zerodha requires a fresh
browser login (with 2FA/TOTP) once per trading day — access tokens
expire daily around 6 AM IST. This is the standard, documented Kite
Connect flow (login_url -> request_token -> generate_session), not a
workaround.

On first run each day, this will:
  1. Print a login URL for you to open in your browser.
  2. You log in, Zerodha redirects to your configured redirect URL
     with a `request_token` in the query string.
  3. Paste that request_token back into the terminal when prompted.
  4. The resulting access_token is cached locally for the rest of
     the day so you only do this once per morning.

You need your own Kite Connect app (api_key + api_secret) from
https://developers.kite.trade — this is separate from Claude's Kite
MCP connector, since this script runs standalone on your machine.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

TOKEN_CACHE_FILE = Path(__file__).parent / ".kite_session_cache.json"

# Token expiry is an exchange-day rule, so "today" must mean the exchange's
# today. A naive datetime.now() ties that to whatever timezone the host happens
# to be set to — correct on the VM only because its clock was changed to IST,
# which is an invisible dependency nothing checks.
IST = ZoneInfo("Asia/Kolkata")

NO_TERMINAL_MSG = (
    "No valid Kite token for today, and no terminal to log in from. "
    "Run: python3 token_helper.py"
)


class NoTokenError(RuntimeError):
    """No usable token and no terminal to obtain one.

    Distinct from a generic failure because restarting cannot fix it: only a
    human running token_helper.py can, and that script restarts the service
    itself. main.py maps this to exit 78, which the systemd unit lists under
    RestartPreventExitStatus — so the service stops cleanly instead of retrying
    every 30s for a whole session and recovering nothing.

    Subclasses RuntimeError so existing `except RuntimeError` callers still work.
    """


def _today_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _load_cached_token(api_key: str) -> str | None:
    if not TOKEN_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    if data.get("api_key") != api_key:
        return None
    if data.get("date") != _today_str():
        return None  # token from a previous day — expired by exchange rule
    return data.get("access_token")


def _save_token_cache(api_key: str, access_token: str) -> None:
    TOKEN_CACHE_FILE.write_text(json.dumps({
        "api_key": api_key,
        "access_token": access_token,
        "date": _today_str(),
    }))
    # Keep this file out of version control / not world-readable
    try:
        os.chmod(TOKEN_CACHE_FILE, 0o600)
    except OSError:
        pass


def has_token_for_today(api_key: str) -> bool:
    """True if a cached token for TODAY exists.

    File-only, no network — unlike try_cached_session(), which also calls
    profile() to verify. Exists for probes that run on a short timer and only
    need to know whether a session COULD be established, not whether it
    currently works: healthcheck.py uses it to stay silent about services
    that legitimately cannot run before the day's login has landed.
    """
    return _load_cached_token(api_key) is not None


def try_cached_session(api_key: str) -> KiteConnect | None:
    """Returns a ready-to-use KiteConnect if a valid cached token exists
    for today, else None. Never blocks on input — safe to call from a
    GUI event loop."""
    cached_token = _load_cached_token(api_key)
    if not cached_token:
        return None

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(cached_token)
    try:
        kite.profile()
        return kite
    except Exception:
        return None


def get_login_url(api_key: str) -> str:
    """Just the URL to send the user to — no blocking input()."""
    return KiteConnect(api_key=api_key).login_url()


def complete_login(api_key: str, api_secret: str, request_token: str) -> KiteConnect:
    """Exchanges a request_token (pasted by the user after browser login)
    for an access_token, caches it, and returns a ready KiteConnect.
    Raises on failure — caller (GUI or CLI) decides how to show the error."""
    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]
    kite.set_access_token(access_token)
    _save_token_cache(api_key, access_token)
    return kite


def get_authenticated_kite(api_key: str, api_secret: str) -> KiteConnect:
    """
    Returns a KiteConnect instance with a valid access_token set.
    Reuses today's cached token if available; otherwise runs the
    interactive (terminal) login flow. For GUI use, prefer
    try_cached_session() + get_login_url() + complete_login() instead,
    which never block on input().
    """
    cached = try_cached_session(api_key)
    if cached:
        print("Reusing cached Kite session for today.")
        return cached

    # Under systemd (or any pipe) stdin is /dev/null, so the input() below is an
    # EOFError traceback rather than a prompt — a crash whose real cause is
    # "no token today", stated nowhere. Fail with that sentence instead.
    #
    # Checked twice on purpose. isatty() catches it early, before printing a
    # login URL nobody is there to open. But it is not reliable everywhere:
    # under Git Bash on Windows, `< /dev/null` still reports isatty() == True
    # (MSYS maps it to a console handle), so the early check would pass and
    # input() would raise anyway. The EOFError catch is what actually
    # guarantees the message, on every platform.
    if not sys.stdin.isatty():
        raise NoTokenError(NO_TERMINAL_MSG)

    print("\n--- Kite Connect login required (once per trading day) ---")
    print(f"1. Open this URL in your browser and log in:\n\n{get_login_url(api_key)}\n")
    print("2. After login, you'll be redirected to your app's redirect URL.")
    print("   Copy the 'request_token' value from that URL's query string.\n")

    try:
        request_token = input("Paste request_token here: ").strip()
    except EOFError:
        raise NoTokenError(NO_TERMINAL_MSG) from None

    kite = complete_login(api_key, api_secret, request_token)
    print("Login successful — session cached for the rest of today.\n")
    return kite
