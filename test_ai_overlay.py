"""
Tests for ai_overlay.py.

The tests that matter here are the ones pinning what the overlay must NOT do.
It is a comment on an alert that already went out, so every failure mode is
"it interfered with the trading path" rather than "the sentence was poor":

  - it must not reach the network when the engine is not delivering (replay,
    dry-run, tests) — the bug the first draft of the spec would have shipped
  - it must not fire on B grades
  - it must not raise into the caller, ever
  - a missing `openai` package must not break the scanner

No network in any test: the Groq client and the Telegram sender are both
replaced, and `_worker` is called directly so nothing depends on thread timing.
"""

from datetime import datetime, timedelta

import pytest

import ai_overlay
from alert_engine import AlertRecord
from signal_engine import (LevelState, OpenInterestState, PullbackState,
                           RejectionState, SignalSnapshot, TrendState,
                           VolumeState)


def snapshot(volume=VolumeState.HIGH, oi=OpenInterestState.RISING,
             vah=LevelState.ABOVE, val=LevelState.ABOVE):
    return SignalSnapshot(
        trend=TrendState.BULLISH, pullback=PullbackState.BULLISH,
        rejection=RejectionState.BULLISH, volume=volume, open_interest=oi,
        poc=LevelState.ABOVE, vah=vah, val=val,
        details={"trend": "Close 57799.00 above VWAP 57734.39, slope +0.73"},
    )


def record(**kw):
    base = dict(id="x", timestamp=datetime(2026, 8, 14, 9, 48, 3), type="Trading",
                direction="Long", grade="A", confidence=80, current_price=57799.0,
                setup="ema", vwap=57734.39, ema10=57778.66, ema_slope=0.73,
                signals=snapshot())
    base.update(kw)
    return AlertRecord(**base)


class FakeClient:
    """Stands in for the Groq client. Records calls; never touches a socket."""

    def __init__(self, content="Volume high but OI falling — watch for a fade.",
                 raises=None):
        self.content = content
        self.raises = raises
        self.calls = []
        self.chat = self  # so .chat.completions.create resolves back here
        self.completions = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        message = type("M", (), {"content": self.content})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice]})()


@pytest.fixture
def wired(monkeypatch):
    """Fake client + captured sends + configured Telegram, cooldown reset."""
    client = FakeClient()
    sent = []
    monkeypatch.setattr(ai_overlay, "_client", client)
    monkeypatch.setattr(ai_overlay, "_send", lambda text: sent.append(text))
    monkeypatch.setattr(ai_overlay.config, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(ai_overlay.config, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(ai_overlay, "_LAST_FAILURE", None)
    return client, sent


# ------------------------------------------------------- what it must NOT do

def test_a_disabled_engine_never_reaches_the_network(wired):
    """THE replay guard. replay/driver.py drives the real SessionRunner, so
    without this a month-long replay fires hundreds of Groq calls and real
    Telegram messages for sessions that ended weeks ago."""
    client, sent = wired
    started = []
    # Prove it never even spawns the thread, not merely that nothing was sent.
    ai_overlay.maybe_send(record(grade="A+"), {}, enabled=False)
    assert client.calls == [] and sent == [] and started == []


def test_b_grades_get_no_overlay(wired):
    client, sent = wired
    for grade in ("B", "B+", "Ignore", None):
        ai_overlay.maybe_send(record(grade=grade), {}, enabled=True)
    assert client.calls == [] and sent == []


def test_unconfigured_telegram_spends_no_groq_call(wired, monkeypatch):
    """No point paying for a sentence that cannot be delivered."""
    client, sent = wired
    monkeypatch.setattr(ai_overlay.config, "TELEGRAM_BOT_TOKEN", "")
    ai_overlay.maybe_send(record(), {}, enabled=True)
    assert client.calls == [] and sent == []


def test_an_empty_api_key_does_not_break_the_import(monkeypatch):
    """REGRESSION, caught on the VM at openai 3.8.0. The constructor — not the
    call — raises OpenAIError when api_key is blank. session_runner imports this
    module at import time, so `openai` installed with GROQ_API_KEY unset stopped
    the SCANNER from starting: precisely the state a "package now, key later"
    deploy leaves behind."""
    class Boom:
        def __init__(self, **kw):
            raise RuntimeError("Missing credentials")

    monkeypatch.setattr(ai_overlay, "OpenAI", Boom)
    monkeypatch.setattr(ai_overlay, "GROQ_API_KEY", "")
    assert ai_overlay._build_client() is None   # short-circuits before Boom

    # And if a future version raises for some other reason, still None.
    monkeypatch.setattr(ai_overlay, "GROQ_API_KEY", "present")
    assert ai_overlay._build_client() is None


def test_a_missing_openai_package_is_survivable(monkeypatch):
    """session_runner imports this at module level. If a missing optional
    dependency could raise here, an absent `openai` would stop the SCANNER —
    turning a comment feature into a hard dependency of the trading path."""
    monkeypatch.setattr(ai_overlay, "_client", None)
    ai_overlay.maybe_send(record(), {}, enabled=True)  # must not raise


# ---------------------------------------------------------- the happy path

def test_an_a_grade_sends_one_prefixed_line(wired):
    client, sent = wired
    ai_overlay._worker(record(grade="A+"), {})
    assert len(client.calls) == 1
    assert sent == ["🤖 Volume high but OI falling — watch for a fade."]


def test_the_payload_shows_the_same_states_as_the_phone_alert(wired):
    """Not a second derivation of VP. volume_profile_status() collapses the
    LevelState pair the engine decided — including REJECTED, which a naive
    price comparison can never produce."""
    _client, _sent = wired
    text = ai_overlay._payload(
        record(signals=snapshot(vah=LevelState.REJECTED, val=LevelState.ABOVE)),
        {"india_vix": 11.3, "premarket_summary": "Range-bound, neutral."})
    assert "VP: Inside Value" in text
    assert "Volume: High" in text and "OI: Rising" in text
    assert "Setup: EMA Pullback + Rejection" in text  # label, not the raw "ema"
    assert "India VIX: 11.3" in text
    assert "Premarket: Range-bound, neutral." in text


def test_missing_premarket_context_reads_na(wired):
    text = ai_overlay._payload(record(), {})
    assert "India VIX: n/a" in text and "Premarket: n/a" in text


def test_the_system_prompt_is_stable(wired):
    """Groq caches it only while it stays byte-identical, and cached tokens are
    what keeps this inside the free tier's TPM ceiling."""
    client, _sent = wired
    ai_overlay._worker(record(), {})
    messages = client.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == ai_overlay.SYSTEM_PROMPT


# --------------------------------------------------------------- failure

def test_an_empty_completion_is_treated_as_a_failure(wired):
    """content=None is what a budget exhausted by reasoning tokens looks like.
    Unguarded, .strip() raises and the symptom is indistinguishable from a dead
    key — for thirty minutes."""
    client, sent = wired
    client.content = None
    ai_overlay._worker(record(), {})
    assert sent == ["🤖 AI unavailable"]


def test_a_failure_alerts_once_then_stays_quiet(wired):
    client, sent = wired
    client.raises = RuntimeError("boom")
    ai_overlay._worker(record(), {})
    ai_overlay._worker(record(), {})
    ai_overlay._worker(record(), {})
    assert sent == ["🤖 AI unavailable"]  # not three


def test_the_cooldown_expires(wired, monkeypatch):
    client, sent = wired
    client.raises = RuntimeError("boom")
    ai_overlay._worker(record(), {})
    monkeypatch.setattr(
        ai_overlay, "_LAST_FAILURE",
        datetime.now() - timedelta(minutes=ai_overlay._FAILURE_COOLDOWN_MIN + 1))
    ai_overlay._worker(record(), {})
    assert sent == ["🤖 AI unavailable", "🤖 AI unavailable"]


def test_a_success_rearms_the_failure_message(wired):
    """Without the reset, one early failure silences the next genuine one for
    thirty minutes even though the service recovered in between."""
    client, sent = wired
    client.raises = RuntimeError("boom")
    ai_overlay._worker(record(), {})
    client.raises = None
    ai_overlay._worker(record(), {})
    client.raises = RuntimeError("boom again")
    ai_overlay._worker(record(), {})
    assert sent[0] == "🤖 AI unavailable"
    assert sent[1].startswith("🤖 Volume high")
    assert sent[2] == "🤖 AI unavailable"


def test_the_semaphore_is_released_even_on_a_baseexception(wired):
    """KeyboardInterrupt is not an Exception, so it passes straight through the
    handler — by design. What must NOT leak is the semaphore slot: two of those
    and the overlay is silently dead for the rest of the session."""
    client, sent = wired
    client.raises = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ai_overlay._worker(record(), {})
    # The semaphore must still have been released, or the overlay is dead for
    # the rest of the session after one unusual failure.
    assert ai_overlay._MAX_CONCURRENT.acquire(blocking=False)
    ai_overlay._MAX_CONCURRENT.release()


def test_concurrency_is_capped_at_two(wired):
    client, sent = wired
    assert ai_overlay._MAX_CONCURRENT.acquire(blocking=False)
    assert ai_overlay._MAX_CONCURRENT.acquire(blocking=False)
    try:
        ai_overlay._worker(record(), {})  # third — dropped, not queued
        assert client.calls == [] and sent == []
    finally:
        ai_overlay._MAX_CONCURRENT.release()
        ai_overlay._MAX_CONCURRENT.release()
