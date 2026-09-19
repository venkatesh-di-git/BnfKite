"""
Dry-run mode (BN_DRY_RUN=1) — see Markdowns/dry_run_section.md.

The invariant these tests exist to hold:

    A dry run writes nothing outside csv-dryrun/, and sends nothing.

It matters because the VM is the live engine and vm-csv/ is the corpus every
tuning decision is measured against. A local run that appends to it does not
fail; it quietly makes the evidence base wrong, and nobody finds out until an
analysis weeks later cannot be explained.

Subprocesses rather than importlib.reload throughout: BN_DRY_RUN is read once at
import (deliberately — a mode that can change mid-session cannot be reasoned
about afterwards), so the real startup path is the thing under test.
"""

import json
import os
import subprocess
import sys

import pytest

import config
import session_runner
from session_runner import SessionRunner

HERE = os.path.dirname(os.path.abspath(__file__))

_PATH_PROBE = """
import json, config
print(json.dumps({k: getattr(config, k) for k in (
    "DRY_RUN", "CSV_DIR", "OUTPUT_FILE", "LIVE_OUTPUT_FILE",
    "LOG_FILE", "SIGNAL_LOG_FILE", "ALERT_LOG_FILE")}))
"""


def _probe(env_value=None):
    """Import config in a clean process and hand back its resolved paths."""
    env = dict(os.environ)
    env.pop("BN_DRY_RUN", None)
    if env_value is not None:
        env["BN_DRY_RUN"] = env_value
    out = subprocess.run([sys.executable, "-c", _PATH_PROBE], cwd=HERE, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


# ---------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------
def test_live_paths_are_unchanged_without_the_flag():
    p = _probe(None)
    assert p["DRY_RUN"] is False
    assert os.path.basename(p["CSV_DIR"]) == "csv"
    for key in ("LOG_FILE", "SIGNAL_LOG_FILE", "ALERT_LOG_FILE"):
        assert os.path.dirname(p[key]) == p["CSV_DIR"], key
    # The heartbeat sits at the project root when live, NOT under csv/.
    assert p["OUTPUT_FILE"] == p["LIVE_OUTPUT_FILE"]
    assert os.path.dirname(p["OUTPUT_FILE"]) == HERE
    print("PASS: live paths unchanged\n")


def test_dry_run_redirects_every_output_path():
    """The invariant, at the level of path resolution.

    OUTPUT_FILE is the one that makes this non-trivial: it is the only output
    that does not live under CSV_DIR, so redirecting CSV_DIR alone would leave a
    dry run overwriting the live heartbeat."""
    p = _probe("1")
    assert p["DRY_RUN"] is True
    assert os.path.basename(p["CSV_DIR"]) == "csv-dryrun"
    for key in ("LOG_FILE", "SIGNAL_LOG_FILE", "ALERT_LOG_FILE", "OUTPUT_FILE"):
        assert os.path.dirname(p[key]) == p["CSV_DIR"], f"{key} escaped csv-dryrun/"
    print("PASS: every output path redirected\n")


def test_live_output_file_ignores_the_flag():
    """healthcheck.py reads LIVE_OUTPUT_FILE so its guard cannot be defeated by
    the environment. If this ever follows DRY_RUN, a stray BN_DRY_RUN on the VM
    stops all alerting while the probe reports healthy — every check green,
    nothing delivered, which is the 10 Aug failure shape exactly."""
    assert _probe("1")["LIVE_OUTPUT_FILE"] == _probe(None)["LIVE_OUTPUT_FILE"]
    print("PASS: LIVE_OUTPUT_FILE is immune to BN_DRY_RUN\n")


def test_csv_dir_is_created_at_import():
    """os.makedirs must sit BELOW the redirect. write_output() and
    SignalStateLog both open by path and neither creates its parent, so getting
    the order wrong kills the first dry run at 09:15 for a boring reason."""
    assert os.path.isdir(_probe("1")["CSV_DIR"])
    print("PASS: csv-dryrun/ exists after import\n")


@pytest.mark.parametrize("value,expected", [
    ("", False), ("0", False), ("false", False), ("False", False), ("no", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_flag_truthiness(value, expected):
    """Worth its own test because the expensive way to learn that BN_DRY_RUN=0
    means "on" is at 09:15 with no alerts arriving."""
    assert _probe(value)["DRY_RUN"] is expected


# ---------------------------------------------------------------
# Telegram wiring
# ---------------------------------------------------------------
def test_dry_run_disables_telegram(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    assert SessionRunner().alert_engine.enable_telegram is False
    print("PASS: dry run disables Telegram\n")


def test_live_run_enables_telegram(monkeypatch):
    """The converse matters more. A bug that leaves Telegram off in live mode is
    silent: the engine runs, the CSVs fill, and no alert ever arrives."""
    monkeypatch.setattr(config, "DRY_RUN", False)
    assert SessionRunner().alert_engine.enable_telegram is True
    print("PASS: live run enables Telegram\n")


def test_injected_engine_still_wins(monkeypatch):
    """SessionRunner(alert_engine=...) must keep overriding the default — replay
    passes its own engine and must not have it silently replaced."""
    from alert_engine import AlertEngine
    monkeypatch.setattr(config, "DRY_RUN", True)
    mine = AlertEngine(enable_telegram=False)
    assert SessionRunner(alert_engine=mine).alert_engine is mine
    print("PASS: injected engine is not overridden\n")


# ---------------------------------------------------------------
# The invariant, end to end
# ---------------------------------------------------------------
_RUN_ONE_TICK = """
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, {here!r})
import config, session_runner
from engine import FiveMinuteCandle
from session_runner import SessionRunner

assert config.DRY_RUN, "subprocess did not see BN_DRY_RUN"
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 3, 11, 0, tzinfo=IST)

def bars(count, start, price=57900.0):
    out, t = [], start
    for i in range(count):
        out.append(FiveMinuteCandle(start=t, open=price + i, high=price + i + 5,
                                    low=price + i - 5, close=price + i, volume=2000.0,
                                    oi_open=2_000_000.0, oi_close=2_000_000.0 + i * 100))
        t += timedelta(minutes=5)
    return out

class Feed:
    connected, last_error = True, None
    def start(self): pass
    def stop(self): pass
    def seconds_since_last_tick(self): return 1.0

seed = bars(config.SEED_BARS, NOW - timedelta(days=1, hours=3))
today = bars(20, NOW.replace(hour=9, minute=15))
session_runner.fetch_session_five_minute_candles = lambda *a, **k: (seed, today)
session_runner.LiveTickFeed = lambda *a, **k: Feed()

r = SessionRunner()
r.state["kite"] = type("K", (), {{"access_token": "x"}})()
r.state["contract"] = type("C", (), {{"instrument_token": 1, "tradingsymbol": "BANKNIFTYFUT"}})()
r.ensure_started(NOW)
r.tick(NOW)
assert r.state["result"] is not None, "tick produced nothing — test proves nothing"
"""


def _snapshot(*paths):
    out = {}
    for p in paths:
        if os.path.isdir(p):
            for name in os.listdir(p):
                f = os.path.join(p, name)
                if os.path.isfile(f):
                    out[f] = (os.path.getmtime(f), os.path.getsize(f))
        elif os.path.isfile(p):
            out[p] = (os.path.getmtime(p), os.path.getsize(p))
    return out


def test_a_dry_run_writes_nothing_outside_csv_dryrun(tmp_path):
    """The whole point, end to end: run a real tick under the flag and prove the
    live corpus did not move.

    This is the test that catches redirecting CSV_DIR while leaving OUTPUT_FILE
    at the project root — every path check above can pass while the heartbeat is
    still being overwritten."""
    live_csv = os.path.join(HERE, "csv")
    live_json = os.path.join(HERE, "latest_volume_profile.json")
    before = _snapshot(live_csv, live_json)

    env = dict(os.environ, BN_DRY_RUN="1")
    script = _RUN_ONE_TICK.format(here=HERE)
    proc = subprocess.run([sys.executable, "-c", script], cwd=HERE, env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"dry run failed:\n{proc.stdout}\n{proc.stderr}"

    assert _snapshot(live_csv, live_json) == before, "a dry run touched the live corpus"

    dryrun_dir = os.path.join(HERE, "csv-dryrun")
    written = os.listdir(dryrun_dir) if os.path.isdir(dryrun_dir) else []
    assert written, "dry run wrote nothing at all — the test would pass vacuously"
    print(f"PASS: live corpus untouched; csv-dryrun/ got {sorted(written)}\n")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
