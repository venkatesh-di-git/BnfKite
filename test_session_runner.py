"""
Startup smoke test for SessionRunner (session_runner.py).

Both of this month's outages would have failed one of these, and neither raised
anything at the time:

  10 Aug — engine work lived inside ui.timer, which NiceGUI cancels when no
           browser attaches. The scanner ran 14 hours and produced nothing while
           systemctl said active, port 8080 was bound and HTTP returned 200.
           Caught here by: one tick() must produce a payload.

  11 Aug — main.py seeded above its loop, so a process started outside market
           hours built an empty session and ran the whole day with EMA10 equal
           to the first bar's close. latest_volume_profile.json still updated on
           schedule, so the freshness probe reported healthy throughout.
           Caught here by: seed bars must be non-zero.

Only writable at all because the engine is now importable without starting a web
server — which is the point of the extraction.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import session_runner
from engine import FiveMinuteCandle
from session_runner import SessionRunner

IST = ZoneInfo("Asia/Kolkata")

# Inside market hours on a Monday, so is_market_hours() passes without stubbing.
NOW = datetime(2026, 8, 3, 11, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """Redirect every log path into tmp_path.

    tick() reaches write_output() and signal_log.record(), which resolve
    config.OUTPUT_FILE / LOG_FILE / SIGNAL_LOG_FILE. Without this the smoke test
    appends rows stamped with NOW into the REAL csv/ logs — and vm_handover.md
    §6 tells you to run pytest on the VM, which would put fabricated rows in the
    live corpus the alert tuning reads.

    autouse so it lands before SessionRunner() is constructed: SignalStateLog
    captures config.SIGNAL_LOG_FILE in __init__, not at write time. The other
    three are read per call, so patching the config attribute is enough.
    """
    monkeypatch.setattr(config, "OUTPUT_FILE", str(tmp_path / "latest_volume_profile.json"))
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "volume_profile_log.csv"))
    monkeypatch.setattr(config, "SIGNAL_LOG_FILE", str(tmp_path / "signal_log.csv"))
    monkeypatch.setattr(config, "ALERT_LOG_FILE", str(tmp_path / "alert_log.csv"))


class FakeContract:
    instrument_token = 1234
    tradingsymbol = "BANKNIFTYFUT"


class FakeKite:
    access_token = "fake"


def _bars(count, start, price=57900.0):
    out, t = [], start
    for i in range(count):
        out.append(FiveMinuteCandle(start=t, open=price + i, high=price + i + 5,
                                    low=price + i - 5, close=price + i, volume=2000.0,
                                    oi_open=2_000_000.0, oi_close=2_000_000.0 + i * 100))
        t += timedelta(minutes=5)
    return out


def _runner(monkeypatch):
    """A runner whose REST and websocket are stubbed, so nothing touches Kite.

    Via the monkeypatch fixture rather than plain assignment, so the stubs are
    undone afterwards instead of outliving the test in an imported module.
    """
    seed = _bars(config.SEED_BARS, NOW - timedelta(days=1, hours=3))
    today = _bars(20, NOW.replace(hour=9, minute=15))
    monkeypatch.setattr(session_runner, "fetch_session_five_minute_candles",
                        lambda *a, **k: (seed, today))
    monkeypatch.setattr(session_runner, "LiveTickFeed", lambda *a, **k: _FakeFeed())

    r = SessionRunner()
    r.state["kite"] = FakeKite()
    r.state["contract"] = FakeContract()
    # No Telegram, no alert-log writes from a test run.
    r.alert_engine.enable_telegram = False
    return r


class _FakeFeed:
    connected = True
    last_error = None

    def start(self): pass
    def stop(self): pass
    def seconds_since_last_tick(self): return 1.0


def test_seeds_with_prior_session_bars(monkeypatch):
    """The 11 Aug failure: a session built with zero warm-up bars runs all day
    with EMA10 equal to the first bar's close and relative volume near 1.00."""
    r = _runner(monkeypatch)
    r.ensure_started(NOW)

    session = r.state["live_session"]
    assert session is not None, "ensure_started produced no session"
    assert len(session.seed) == config.SEED_BARS, \
        f"expected {config.SEED_BARS} prior-session bars, got {len(session.seed)}"
    print("PASS: session seeds with prior-session bars\n")


def test_one_tick_produces_a_payload(monkeypatch):
    """The 10 Aug failure: timers cancelled, so nothing ever ran. A tick that
    does nothing looks identical to a healthy idle process from the outside."""
    r = _runner(monkeypatch)
    r.ensure_started(NOW)
    r.tick(NOW)

    assert r.state["result"] is not None, "tick produced no volume profile"
    assert r.state["last_updated"] == NOW
    assert r.state["candle_count"] > 0
    print("PASS: one tick produces a payload\n")


def test_state_alias_survives_a_tick(monkeypatch):
    """app.py does `state = runner.state` and ~120 readers rely on that alias.
    A reassignment inside the runner detaches the dashboard silently — nothing
    raises, the page just stops updating. Same shape as the ui.timer freeze."""
    r = _runner(monkeypatch)
    before = r.state

    r.ensure_started(NOW)
    r.tick(NOW)

    assert r.state is before, "runner rebound self.state — app.py's alias is now stale"
    print("PASS: state alias survives ensure_started + tick\n")


def test_engine_module_never_imports_nicegui():
    """The structural half of the fix. Engine work inside a ui.timer is what
    caused 10 Aug; a module with no access to the UI toolkit cannot hold a stray
    ui.notify, enforced by the import graph rather than by review."""
    import ast

    # Resolved from __file__, not the cwd: pytest can legitimately be invoked
    # from anywhere, and a relative path turns this guard into a FileNotFoundError
    # that reads like a broken test rather than the check it is.
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_runner.py")
    tree = ast.parse(open(source, encoding="utf-8").read())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.append((getattr(node, "module", "") or "") +
                            " " + " ".join(a.name for a in node.names))
    offenders = [i for i in imported if any(w in i for w in ("nicegui", "fastapi", "plotly"))]
    assert not offenders, f"session_runner must stay UI-free, found: {offenders}"
    print("PASS: session_runner imports no web stack\n")


if __name__ == "__main__":
    # Through pytest, not direct calls: the tests take fixtures now (tmp_path and
    # monkeypatch), and the log isolation those provide is the whole point.
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
