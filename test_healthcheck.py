"""
Tests for healthcheck.py's order-watch probe.

Written after Sat 26 Sep, when this probe sent 24 Telegrams on a closed
market. Both defects behind that are regression-tested here:

  - it alerted at all on a day when order-watch CANNOT run (no token yet), and
  - it read a RestartSec=30 crash loop as down -> recovered -> down -> ...,
    because `systemctl is-active` reports "activating" for most of each cycle
    and briefly "active" in between.

The engine's own staleness check is not covered here — it predates this file
and is unchanged; these tests are scoped to the probe that misfired.

No subprocess, no network, no Telegram: systemctl is faked per-test and the
state file is redirected into tmp_path.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import healthcheck as hc


IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=IST)
TODAY = "2026-09-26"


def _show_output(active_state, sub_state, nrestarts):
    """What `systemctl show -p ActiveState -p SubState -p NRestarts` prints."""
    return (f"ActiveState={active_state}\n"
            f"SubState={sub_state}\n"
            f"NRestarts={nrestarts}\n")


class _Result:
    def __init__(self, stdout):
        self.stdout = stdout


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Token present by default, state file in tmp_path, notify() captured.

    Returns (sent, set_systemctl) — sent is the list of Telegram texts, and
    set_systemctl swaps in whatever systemctl should report next, so a test can
    drive two consecutive probes with different states.
    """
    sent = []
    monkeypatch.setattr(hc, "notify", lambda text: sent.append(text))
    monkeypatch.setattr(hc, "ORDER_WATCH_STATE_FILE",
                        str(tmp_path / ".healthcheck_order_watch_state"))
    monkeypatch.setattr(hc, "has_token_for_today", lambda api_key: True)

    def set_systemctl(active_state="active", sub_state="running", nrestarts="7"):
        monkeypatch.setattr(
            hc.subprocess, "run",
            lambda *a, **k: _Result(_show_output(active_state, sub_state, nrestarts)))

    set_systemctl()
    return sent, set_systemctl


# ------------------------------------------------- the non-trading-day case

def test_no_token_for_today_says_nothing(wired, monkeypatch):
    """The 26 Sep bug. Without today's login order_watch.py exits 1 by design,
    so it crash-loops all weekend — expected, not a fault, and not worth a
    single Telegram let alone 24."""
    sent, set_systemctl = wired
    monkeypatch.setattr(hc, "has_token_for_today", lambda api_key: False)
    set_systemctl(active_state="activating", sub_state="auto-restart", nrestarts="3047")

    assert hc._check_order_watch(NOW) == 0
    assert sent == []


def test_no_token_check_happens_before_systemctl(wired, monkeypatch):
    """The token gate must short-circuit, not merely suppress the message —
    a probe on a 5-minute timer should not shell out on every non-trading day."""
    sent, _ = wired
    monkeypatch.setattr(hc, "has_token_for_today", lambda api_key: False)

    def _boom(*a, **k):
        raise AssertionError("systemctl must not be called without a token")
    monkeypatch.setattr(hc.subprocess, "run", _boom)

    assert hc._check_order_watch(NOW) == 0


# ------------------------------------------------------------- healthy path

def test_stable_and_running_is_silent(wired):
    sent, _ = wired
    assert hc._check_order_watch(NOW) == 0
    assert sent == []


def test_two_stable_probes_stay_silent(wired):
    """Unchanged NRestarts across probes is the definition of stable."""
    sent, _ = wired
    hc._check_order_watch(NOW)
    assert hc._check_order_watch(NOW) == 0
    assert sent == []


# -------------------------------------------------------------- crash loop

def test_a_crash_loop_alerts_once_not_repeatedly(wired):
    sent, set_systemctl = wired
    set_systemctl(active_state="activating", sub_state="auto-restart", nrestarts="3047")

    assert hc._check_order_watch(NOW) == 1
    assert len(sent) == 1
    assert "crash-looping" in sent[0]

    # Second probe, restart counter has moved on as the loop continues.
    set_systemctl(active_state="activating", sub_state="auto-restart", nrestarts="3050")
    assert hc._check_order_watch(NOW) == 1
    assert len(sent) == 1, "a continuing crash loop must not re-alert"


def test_briefly_active_during_a_crash_loop_is_not_a_recovery(wired):
    """THE flap regression. A RestartSec=30 loop reports "active" for a second
    or so per cycle; sampling that must not produce "recovered", or the probe
    alternates down/recovered forever — which is exactly what sent 24 messages."""
    sent, set_systemctl = wired
    set_systemctl(active_state="activating", sub_state="auto-restart", nrestarts="3047")
    hc._check_order_watch(NOW)
    assert len(sent) == 1

    # Next probe catches the brief running window — but NRestarts has moved,
    # which proves it restarted in between.
    set_systemctl(active_state="active", sub_state="running", nrestarts="3052")
    assert hc._check_order_watch(NOW) == 1
    assert len(sent) == 1, "a restart since the last probe is not a recovery"


def test_a_stopped_service_is_reported_as_not_running(wired):
    """A deliberate `systemctl stop` is a different message from a crash loop."""
    sent, set_systemctl = wired
    set_systemctl(active_state="failed", sub_state="failed", nrestarts="7")

    assert hc._check_order_watch(NOW) == 1
    assert len(sent) == 1
    assert "not running" in sent[0]
    assert "crash-looping" not in sent[0]


# ---------------------------------------------------------------- recovery

def test_a_genuine_recovery_sends_exactly_one_message(wired):
    sent, set_systemctl = wired
    set_systemctl(active_state="failed", sub_state="failed", nrestarts="7")
    hc._check_order_watch(NOW)
    assert len(sent) == 1

    # Restarted once to recover, then stays put across two probes.
    set_systemctl(active_state="active", sub_state="running", nrestarts="8")
    hc._check_order_watch(NOW)          # NRestarts moved — not yet trusted
    set_systemctl(active_state="active", sub_state="running", nrestarts="8")
    assert hc._check_order_watch(NOW) == 0

    recoveries = [t for t in sent if "recovered" in t]
    assert len(recoveries) == 1
    assert "running again" in recoveries[0]


# ----------------------------------------------------------- can't tell

def test_no_systemctl_on_this_host_is_silent(wired, monkeypatch):
    """A dev workstation is not a failing VM — the probe cannot answer the
    question here, so it must not invent a "down"."""
    sent, _ = wired

    def _missing(*a, **k):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(hc.subprocess, "run", _missing)

    assert hc._check_order_watch(NOW) == 0
    assert sent == []


def test_a_hung_systemctl_alerts_once(wired, monkeypatch):
    sent, _ = wired

    def _timeout(*a, **k):
        raise hc.subprocess.TimeoutExpired(cmd="systemctl", timeout=10)
    monkeypatch.setattr(hc.subprocess, "run", _timeout)

    assert hc._check_order_watch(NOW) == 1
    assert len(sent) == 1
    assert "did not respond" in sent[0]


# ------------------------------------------------------------ state file

def test_state_does_not_carry_across_days(wired):
    """The date key guarantees at least one alert per day: yesterday's "down"
    must not mute today's failure."""
    sent, set_systemctl = wired
    set_systemctl(active_state="failed", sub_state="failed", nrestarts="7")
    hc._check_order_watch(NOW)
    assert len(sent) == 1

    tomorrow = NOW.replace(day=27)
    assert hc._check_order_watch(tomorrow) == 1
    assert len(sent) == 2, "a new day must alert again"


def test_watch_state_round_trips(wired):
    hc._save_watch_state("down", TODAY, "3047")
    assert hc._load_watch_state(TODAY) == ("down", "3047")


def test_watch_state_without_a_restart_count_reads_as_unknown(wired):
    """Backward compatibility with a file written before NRestarts was stored."""
    hc._save_watch_state("down", TODAY, None)
    assert hc._load_watch_state(TODAY) == ("down", None)


def test_watch_state_from_another_day_reads_clean(wired):
    hc._save_watch_state("down", "2026-09-25", "3047")
    assert hc._load_watch_state(TODAY) == ("ok", None)
