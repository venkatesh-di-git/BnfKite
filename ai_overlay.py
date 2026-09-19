"""
ai_overlay.py — the trailing AI sentence after an A/A+ alert.

DELIBERATELY OUTSIDE THE HASHED SET, like telegram_format.py. engine_version()
hashes signal_engine.py, decision_engine.py and alert_engine.py; this module can
change freely without moving the fingerprint or forcing a golden re-freeze.

That placement is the whole design. The obvious home was alert_engine.py, where
the record is built — but that file IS hashed (engine.py:85, deliberately), and
golden replay baselines are keyed on (day, engine_version, tick_mode), so an
overlay touching no rule would still invalidate every frozen baseline.
session_runner.py already sets this precedent for dry-run wiring, for the same
reason (session_runner.py:68).

A PURE OVERLAY. Reads a DELIVERED AlertRecord and sends a second Telegram
message. Writes nothing, gates nothing, returns nothing, and holds no reference
the engine can see. Delete this file and the alert pipeline is unchanged.

Never blocks the real alert: maybe_send() starts a daemon thread and returns.
A dead or slow Groq endpoint costs nothing but its own thread.
"""

import logging
import os
import threading
from datetime import datetime

import requests

import config
# The SAME words the phone alert shows, not a second derivation. VP in
# particular must come from volume_profile_status(): SignalSnapshot.vah/.val are
# already LevelState, and that enum includes REJECTED, which a naive
# `price > vah` can never produce — see telegram_format.py:21-25.
from telegram_format import (MISSING, SETUP_LABELS, volume_profile_status,
                             _state as state_label)

logger = logging.getLogger(__name__)

# THE openai IMPORT IS GUARDED, and that is not defensive habit — it is the
# module contract. This file promises that deleting it leaves the alert pipeline
# unaffected; a bare `from openai import OpenAI` breaks that promise in the
# worst way, because session_runner imports this at module level. A dev box or
# test runner without the package would then fail to start the SCANNER, turning
# an optional comment feature into a hard dependency of the trading path.
#
# Absent package -> _client is None -> maybe_send() returns immediately and
# nothing else in the system notices.
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - exercised by not installing openai
    OpenAI = None

# os.environ, not config: GROQ_API_KEY arrives via the unit's EnvironmentFile=,
# so it is in the environment before Python starts. Reading it through config
# would work too, but only because `import config` runs load_dotenv() first —
# correct by accident of import order rather than by construction.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")


def _build_client():
    """None whenever a client cannot be built, for ANY reason.

    THE EMPTY-KEY CASE IS NOT HYPOTHETICAL. openai>=3 raises OpenAIError from
    the CONSTRUCTOR when api_key is blank — not at call time. Since
    session_runner imports this module at import time, `openai` installed with
    GROQ_API_KEY unset would raise here, fail that import, and stop the SCANNER
    from starting at all. Measured on the VM at openai 3.8.0, in exactly the
    state a "install the package now, add the key later" deploy produces.

    So the key is checked before construction AND the construction is wrapped:
    the first covers today's behaviour, the second covers a future version that
    finds some other reason to raise. Neither is worth a scanner outage.
    """
    if OpenAI is None or not GROQ_API_KEY:
        return None
    try:
        return OpenAI(base_url="https://api.groq.com/openai/v1",
                      api_key=GROQ_API_KEY)
    except Exception as e:
        logger.warning("ai overlay disabled — client init failed: %s: %s",
                       type(e).__name__, e)
        return None


_client = _build_client()

# Two in flight is the cap. A third alert inside the same window is dropped
# rather than queued: this is a comment on a live setup, and one that arrives
# after the move has played out is worse than none.
_MAX_CONCURRENT = threading.Semaphore(2)

# Module-level, so the cooldown is per-process rather than per-alert. Reset to
# None on any success, so a genuine later failure still gets one message.
_LAST_FAILURE = None
_FAILURE_COOLDOWN_MIN = 30

# Grades that earn an overlay. Intentionally not read from alert_engine's
# TRADING_GRADES — that constant is hashed and this file must never be a reason
# to touch it. B and B+ produce no AI message by design.
_OVERLAY_GRADES = ("A", "A+")

# Must stay byte-identical between calls or Groq's automatic prompt caching will
# not hit, and cached tokens are the reason this fits inside the free tier's TPM
# ceiling with room to spare.
SYSTEM_PROMPT = """You are reviewing a Bank Nifty futures setup already \
graded by a deterministic rule engine. The grade is final — never question, \
re-grade, or contradict it.

Your only job: one short sentence noting a tension between the fields, or what \
to watch next. If everything aligns cleanly, say so briefly.

Hard rules:
- Never state a price target, stop loss, or entry level.
- Never predict direction beyond what the grade already says.
- Never invent numbers not given to you.
- Premarket context is a starting bias only; live price action overrides it. \
Never let premarket contradict the grade.
- Maximum 25 words. One sentence. No preamble."""


def maybe_send(record, state, enabled: bool = True) -> None:
    """Fire-and-forget. Called from session_runner after a DELIVERED alert.

    `enabled` is load-bearing, not decoration. Replay drives the real
    SessionRunner (replay/driver.py:133), so without it a month-long replay
    would fire hundreds of Groq calls and real Telegram messages for sessions
    that ended weeks ago — exactly what replay_day's own BN_DRY_RUN guard
    exists to prevent.
    """
    if _client is None:
        return  # no package, or no key — the overlay is simply absent
    if not enabled or record is None or record.grade not in _OVERLAY_GRADES:
        return
    # Checked here rather than in the worker so an unconfigured machine never
    # spawns a thread or spends a Groq call on a message it cannot deliver.
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    threading.Thread(target=_worker, args=(record, state),
                     name="ai-overlay", daemon=True).start()


def _send(text: str) -> None:
    """Deliberately NOT alert_engine's delivery queue.

    The queue exists to keep a blocking requests.post off the caller's thread,
    and its ordering guarantee is for real alerts. Putting this in it would let
    a slow AI call sit in front of the next genuine alert — the one thing this
    feature must never do.
    """
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        logger.debug("ai overlay send failed: %s: %s", type(e).__name__, e)


def _payload(record, state) -> str:
    """The record, rendered as the same states the phone alert shows."""
    snapshot = getattr(record, "signals", None)
    setup = SETUP_LABELS.get(record.setup, record.setup or MISSING)
    volume = state_label(snapshot.volume) if snapshot else MISSING
    oi = state_label(snapshot.open_interest) if snapshot else MISSING
    vp = volume_profile_status(snapshot.vah, snapshot.val) if snapshot else MISSING
    return (
        f"Grade: {record.grade} {record.direction}\n"
        f"Price: {record.current_price}\n"
        f"Setup: {setup}\n"
        f"VWAP: {record.vwap}\n"
        f"EMA10: {record.ema10}\n"
        f"EMA10 slope: {record.ema_slope}/min\n"
        f"Volume: {volume}\n"
        f"OI: {oi}\n"
        f"VP: {vp}\n"
        # Cached at startup from premarket_state.json; never fetched here. `or`
        # rather than a default, so a null in that file reads as n/a too.
        f"India VIX: {state.get('india_vix') or 'n/a'}\n"
        f"Premarket: {state.get('premarket_summary') or 'n/a'}\n"
    )


def _worker(record, state) -> None:
    global _LAST_FAILURE

    if not _MAX_CONCURRENT.acquire(blocking=False):
        return  # two already in flight; skip this one silently

    try:
        resp = _client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _payload(record, state)},
            ],
            max_tokens=150,
            temperature=0,
            reasoning_effort="medium",
            timeout=8,
        )

        # MANDATORY, not defensive. On gpt-oss-120b reasoning tokens are charged
        # against the completion budget, so at medium effort a 150-token ceiling
        # can be consumed entirely by reasoning and return content=None. Calling
        # .strip() on that raises, the except below swallows it, and the symptom
        # is "AI unavailable" for 30 minutes — indistinguishable from a dead key.
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("empty completion — likely max_tokens exhausted by "
                             "reasoning; raise it or lower reasoning_effort")

        _send(f"🤖 {content[:200]}")
        _LAST_FAILURE = None  # so a later genuine failure still alerts

    except Exception as e:
        # "No logging" was always about not persisting AI CONTENT. This stores
        # none. Without it, a dead key, an 8s timeout, a rate limit and an
        # exhausted token budget are four faults producing one identical
        # message on a 30-minute cooldown, with nothing to read afterwards.
        logger.debug("ai overlay failed: %s: %s", type(e).__name__, e)

        now = datetime.now()
        if (_LAST_FAILURE is None
                or (now - _LAST_FAILURE).total_seconds()
                > _FAILURE_COOLDOWN_MIN * 60):
            _send("🤖 AI unavailable")
            _LAST_FAILURE = now
        # otherwise silent — one message per 30 min, never a retry

    finally:
        _MAX_CONCURRENT.release()
