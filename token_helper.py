#!/usr/bin/env python3
"""
token_helper.py — run each morning on the VM, before 09:15.

Kite sessions expire daily. This prints the login URL, takes the request_token
from the redirect, exchanges it for an access_token, writes it where the scanner
actually reads it, and restarts the scanner — but only once the token is proven
loadable.

It asks for the request_token, not the access_token, because the request_token
is the only one any screen ever shows you. The exchange used to happen on the
laptop, which meant copying an access_token across by hand; both strings are 32
characters, so pasting the wrong one failed with a misleading "Incorrect api_key
or access_token".

Usage:
    python3 token_helper.py
"""

import subprocess
import sys

import config
# complete_login exchanges the request_token AND writes the cache, so it stays
# the single writer of a format the scanner has to agree with. The scanner reads
# a JSON cache keyed by api_key and today's date, not a bare token file.
from kite_auth import complete_login, get_login_url, try_cached_session

SERVICE = "kite-scanner"


def main() -> int:
    # From config, not os.environ: config runs load_dotenv(), so a .env-only
    # machine has no KITE_API_KEY in the environment. Prompting for it instead
    # invites a key that differs from the scanner's, which fails
    # _load_cached_token's api_key check silently.
    api_key = config.KITE_API_KEY
    api_secret = config.KITE_API_SECRET
    if not api_key or not api_secret:
        print("KITE_API_KEY / KITE_API_SECRET missing — set them in .env. Aborting.")
        return 1

    print(f"\n1. Open this and log in:\n\n{get_login_url(api_key)}\n")
    print("2. You'll be redirected. Copy the request_token from that URL's query string.\n")

    request_token = input("Paste request_token: ").strip()
    if not request_token:
        print("Nothing entered. Aborting.")
        return 1

    try:
        # Exchanges the request_token and writes the cache in one step.
        kite = complete_login(api_key, api_secret, request_token)
        profile = kite.profile()
    except Exception as e:
        print(f"FAILED — Kite rejected the request_token: {e}")
        print("A request_token is single-use and expires within minutes, so re-running")
        print("with the same one always fails. Log in again for a fresh one.")
        return 1

    # Kite accepting the token is not the same as the scanner being able to
    # LOAD it. Restarting before checking is how a format or api_key mismatch
    # ends up as a silently unauthenticated scanner at 09:16 — it falls through
    # to get_authenticated_kite and blocks on an input() no VM can answer.
    if try_cached_session(api_key) is None:
        print("FAILED — token saved but the scanner cannot read it back. Not restarting.")
        return 1

    print(f"OK — connected as {profile.get('user_name', 'unknown')}")

    try:
        subprocess.run(["systemctl", "--user", "restart", SERVICE], check=True)
        print("Scanner restarted.")
    except subprocess.CalledProcessError as e:
        print(f"Token is valid, but the scanner restart failed: {e}")
        return 1
    except FileNotFoundError:
        print("Token is valid. (systemctl not found — start the scanner manually.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
