# AI Overlay Alert — Implementation Spec (v4)

Implementation-ready. Every name below was verified against the code on
05 Sep 2026; nothing here is "confirm before running".

v3 → v4: the overlay moved out of `alert_engine.py`. See "Why not
alert_engine.py" — v3's two headline constraints could not both hold.

---

## STATUS — built and deployed 05 Sep 2026, inert pending a key

Code is on the VM and `openai` 3.8.0 is installed. It produces nothing until
`GROQ_API_KEY` exists in the VM's `.env` — by design, not by fault.

`engine_version()` is **`fd8e6f05`, unchanged**, which was the point of moving
the overlay out of `alert_engine.py`. No golden re-freeze was needed. (Item 1
of the next batch — the `raise_for_status` fix — *will* move it; that is a
separate, deliberate change. See `session_state_05sep.md`.)

Tests: `test_ai_overlay.py`, 15 passing.

**Two hardenings the implementation added beyond this spec**, both now folded
into the code listing below:

- The `openai` import **and** the client construction are guarded. `openai>=3`
  raises `OpenAIError` from the *constructor* on a blank key — found on the VM
  at 3.8.0, in exactly the state a "install the package now, add the key later"
  deploy produces. Unguarded, `import session_runner` would have failed and the
  **scanner** would not have started. Regression test:
  `test_an_empty_api_key_does_not_break_the_import`.
- The call site passes `enabled=self.alert_engine.enable_telegram`, not
  `not config.DRY_RUN` — tests and harnesses build
  `AlertEngine(enable_telegram=False)` with `BN_DRY_RUN` unset, and only the
  engine's own flag is right in every path.

**Still open:** `_state` is imported from `telegram_format.py` under its
private name (as `state_label`). Rename it there or accept the underscore —
one line either way, `telegram_format.py` is unhashed so a rename is free.

---

## Summary

A second Telegram message that trails an **A / A+** alert with a one-sentence
AI read of the setup.

Pure overlay — no write access to `Decision`, `Gates`, `Grades`, `Latch`, or
CSV. If it were deleted entirely, the existing alert pipeline would be
unaffected.

**New file:** `ai_overlay.py`
**Touches:** `session_runner.py` (one call site, two state keys),
`requirements.txt` (`openai`)
**New tests:** `test_ai_overlay.py`
**Requires:** `GROQ_API_KEY` in the environment.

---

## Why not `alert_engine.py`

v3 said "file touched: `alert_engine.py` only" and, in the same document,
"confirm `engine_version()` is unchanged — this must not touch a hashed
module." Both cannot be true. `engine_version()` hashes `alert_engine.py`
deliberately ([engine.py:85](../engine.py#L85), reasoned at
[engine.py:82](../engine.py#L82)), and golden replay baselines are keyed on
`(day, engine_version, tick_mode)` ([replay/golden.py:83](../replay/golden.py#L83)) —
so an overlay that touches no rule would still invalidate every frozen
baseline and force a re-freeze.

`session_runner.py` is outside the hashed set, and
[session_runner.py:68-70](../session_runner.py#L68-L70) already establishes
this exact precedent: dry-run wiring lives there rather than in
`alert_engine.py` for the same reason. The call site there also has three
things the v3 site did not:

- the returned `alert` record, already in hand
- `self.state`, which is where `india_vix` and `premarket_summary` live —
  `AlertEngine` has no `state` attribute at all
  ([alert_engine.py:150](../alert_engine.py#L150))
- `config.DRY_RUN`, which is what stops replay firing live AI messages

---

## Settings

| Setting | Value |
|---|---|
| Model | `openai/gpt-oss-120b` (Groq, free tier) |
| `reasoning_effort` | `"medium"` — provisional, see below |
| `max_tokens` | 150 — provisional, see below |
| `temperature` | 0 |
| Timeout | 8s |
| Concurrency | `Semaphore(2)` |
| Grades | A, A+ only |
| Failure cooldown | 30 min |
| Logging | no AI content persisted; exception type at debug |

**150 is provisional, and the content guard is what makes that safe.** On
`gpt-oss-120b`, reasoning tokens are charged against the completion budget, so
at medium effort a 150-token ceiling can in principle be consumed entirely by
reasoning — returning empty or `None` content rather than a short sentence.
Ship medium/150 and adjust after ten real responses; deciding this in the
abstract is guessing.

**The symptom to watch for** is `🤖 AI unavailable` arriving after an A/A+
alert while the key is known good and the network is fine. That is the budget
running out, not an outage. The fix is `max_tokens=500`, or
`reasoning_effort="low"` — the token budget has enormous headroom either way
(see rate limits below), so raising it costs nothing.

Without the content guard below, that symptom is indistinguishable from a dead
API key and locks the overlay out for 30 minutes. The guard is not optional.

**Rate-limit context** (Groq free tier, `gpt-oss-120b`): 30 RPM, 1K RPD,
8K TPM, 200K TPD. At ~310 tokens per call and a worst case of 2 alerts per
minute, usage sits under 8% of TPM. Cached tokens do not count toward rate
limits, and the static system prompt is cached automatically after the first
call — provided it stays byte-identical and comes first in `messages`.

---

## `ai_overlay.py`

```python
"""
ai_overlay.py — the trailing AI sentence after an A/A+ alert.

OUTSIDE THE HASHED SET, like telegram_format.py. engine_version() hashes
signal_engine.py, decision_engine.py and alert_engine.py; this module can
change freely without moving the fingerprint or re-freezing goldens.

Reads a delivered AlertRecord and sends. Writes nothing, gates nothing,
returns nothing. Deleting this file leaves the alert pipeline intact.
"""

import logging
import os
import threading
from datetime import datetime

import requests

import config
# The SAME words the phone alert shows. Re-deriving VP from a price
# comparison is what telegram_format.py:21-25 warns against — its LevelState
# includes REJECTED, which `price > vah` can never produce.
from telegram_format import (MISSING, SETUP_LABELS, volume_profile_status,
                             _state as state_label)

logger = logging.getLogger(__name__)

# GUARDED, and this is the module contract rather than defensive habit. This
# file promises that deleting it leaves the alert pipeline unaffected — and
# session_runner imports it at module level, so a bare `from openai import
# OpenAI` would mean an absent package stops the SCANNER. An optional comment
# feature must never become a hard dependency of the trading path.
# No package -> _client is None -> maybe_send() returns and nothing notices.
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# os.environ, not config: GROQ_API_KEY is delivered by the unit's
# EnvironmentFile=, so it is in the environment before Python starts. That is
# what makes this correct rather than accidentally correct — reading it here
# used to depend on `import config` having run load_dotenv() first.
_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ.get("GROQ_API_KEY", ""),
) if OpenAI is not None else None

_MAX_CONCURRENT = threading.Semaphore(2)
_LAST_FAILURE = None
_FAILURE_COOLDOWN_MIN = 30

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


def maybe_send(record, state, enabled=True):
    """Fire-and-forget. Called from session_runner after a DELIVERED alert."""
    if _client is None:
        return  # openai not installed — overlay is simply absent
    if not enabled or record is None or record.grade not in _OVERLAY_GRADES:
        return
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    threading.Thread(target=_worker, args=(record, state), daemon=True).start()


def _send(text):
    """Deliberately NOT the alert delivery queue. The whole point is that this
    cannot sit in front of a real alert."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception:
        pass


def _payload(record, state):
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
        f"India VIX: {state.get('india_vix') or 'n/a'}\n"
        f"Premarket: {state.get('premarket_summary') or 'n/a'}\n"
    )


def _worker(record, state):
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

        # MANDATORY. content is None when the completion budget went entirely
        # to reasoning tokens — .strip() on None raises, and the except below
        # would report that as "AI unavailable" for 30 minutes.
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("empty completion")

        _send(f"🤖 {content[:200]}")
        _LAST_FAILURE = None  # reset so a later genuine failure still alerts

    except Exception as exc:
        # "No logging" means no AI CONTENT is persisted — this stores none.
        # Without it, a dead API key, an 8s timeout and a completion budget
        # eaten by reasoning tokens are three different faults producing one
        # identical message, on a 30-minute cooldown, with nothing to read.
        logger.debug("ai overlay failed: %s: %s", type(exc).__name__, exc)

        now = datetime.now()
        if (_LAST_FAILURE is None
                or (now - _LAST_FAILURE).total_seconds()
                > _FAILURE_COOLDOWN_MIN * 60):
            _send("🤖 AI unavailable")
            _LAST_FAILURE = now
        # otherwise silent

    finally:
        _MAX_CONCURRENT.release()
```

---

## Call site

`session_runner.py`, at the end of the alert block
([session_runner.py:287](../session_runner.py#L287)) — after
`process_decision` has returned, never inside it:

```python
        if alert:
            s["current_alert"] = alert
            ai_overlay.maybe_send(alert, s,
                                  enabled=self.alert_engine.enable_telegram)
```

**`enabled=` is load-bearing, not decoration.** Replay drives the real
`SessionRunner` ([replay/driver.py:133](../replay/driver.py#L133)). Without this
argument, replaying a month would fire hundreds of Groq calls and real Telegram
messages for sessions that ended weeks ago — precisely what `replay_day`'s own
guard ([replay/driver.py:97-101](../replay/driver.py#L97-L101)) exists to
prevent.

**It reads `self.alert_engine.enable_telegram`, not `config.DRY_RUN`** — the
implementation tightened this. They agree in the live and replay paths, since
the engine is built with `enable_telegram=not config.DRY_RUN`
([session_runner.py:71](../session_runner.py#L71)). But tests and harnesses
construct `AlertEngine(enable_telegram=False)` directly, with `BN_DRY_RUN`
unset; `config.DRY_RUN` alone would let the overlay reach the network in
exactly those runs. Deferring to the flag the AlertEngine itself honours means
there is one answer to "is this engine delivering", not two that can disagree.

Also add to `initial_state()` ([session_runner.py:43](../session_runner.py#L43)):

```python
        # Premarket context for the AI overlay. Read-only here; loaded once at
        # startup from premarket_state.json. Nothing in the engine reads these.
        "india_vix": None, "premarket_summary": None,
```

---

## Record fields — verified, not assumed

Present on `AlertRecord` ([alert_engine.py:81-125](../alert_engine.py#L81-L125)):

```
record.grade   record.direction   record.current_price   record.setup
record.vwap    record.ema10       record.ema_slope       record.signals
```

**Not present** — v3 named these and they do not exist:

| v3 name | Reality |
|---|---|
| `record.volume_state` | `record.signals.volume` (a `str, Enum`) |
| `record.oi_state` | `record.signals.open_interest` |
| `record.vp_position` | derived from `signals.vah` / `signals.val` via `volume_profile_status()` |

`ema_slope` is EMA10's change over `EMA_SLOPE_LOOKBACK_SECONDS` (60s), which is
what makes `/min` correct — see [telegram_format.py:116-121](../telegram_format.py#L116-L121).

`india_vix` and `premarket_summary` come from `SessionRunner.state`, cached at
startup from `premarket_state.json`, never fetched inside this worker. Absent →
`n/a` and the model works with less.

---

## Behaviour

**Trailing, non-blocking.** Runs on its own daemon thread. The main alert path
never waits on it. A slow or dead API call cannot delay or block the real
alert.

**Ordering is expected, not guaranteed.** The real alert goes through the
delivery queue ([alert_engine.py:426](../alert_engine.py#L426)) while this
thread starts immediately. In practice the API round-trip is seconds and the
queue is empty, so the alert lands first — but a backed-up queue could invert
them. Acceptable: the AI line is prefixed 🤖 and reads as a comment either way.

**Failure.** Any exception — timeout, rate limit, bad key, empty completion,
malformed response, network — sends `🤖 AI unavailable` once, then stays silent
for 30 minutes. Resets on the next success. Never retries.

**Diagnosis is one `journalctl` away.** The exception type is logged at debug;
the AI's output never is. `journalctl --user -u kite-scanner | grep "ai overlay
failed"` separates the four faults that share the one Telegram message. This is
the only change v4 makes to v3's no-logging stance, and it holds the line the
stance was actually drawn on: no model content is stored, in Telegram, in CSV,
or in the journal.

---

## Verification

1. `GROQ_API_KEY` set in the environment on the VM; `openai` installed
2. Trigger an A/A+ alert — main alert arrives, AI line follows within a few
   seconds
3. Unset the key, trigger again — `🤖 AI unavailable` arrives once; a second
   alert within 30 min produces no failure message
4. **`engine_version()` is unchanged.** The baseline is **`fd8e6f05`**,
   measured 05 Sep 2026 and identical on the dev box and `bnvm`. Re-run
   `python -c "from engine import engine_version; print(engine_version())"`
   after the change; a different value means the overlay landed in a hashed
   module and the v4 restructure was for nothing.

   (The comment at [session_runner.py:70](../session_runner.py#L70) still cites
   `b3620733`. That is stale — a pre-existing doc drift, not something this
   change caused, but it is the number someone would check against.)
5. `pytest test_replay_golden.py -q` passes with no golden re-freeze
6. B and B+ alerts produce no AI message
7. `BN_DRY_RUN=1 python -m replay.driver ...` over an archived day sends **no**
   Telegram messages and makes **no** Groq calls

---

## Out of scope

- No calculation, scoring, or invention of price levels
- No gating, delaying, or suppressing of the real alert
- No changes to `Decision`, `Gates`, `Grades`, `Latch`, or CSV
- No logging, no retry, no evaluation trail
- PCR — considered and dropped (contested indicator, extra API call, cannot be
  evaluated without logging). Included in the *premarket* brief, where there is
  no latency constraint and it is explicitly context

---

## Resolved

- **`reasoning_effort` / `max_tokens`** — ship `medium`/150, adjust after ten
  real responses. Symptom and fix documented in Settings.
- **Failure diagnosability** — `logger.debug` of the exception type. No AI
  content stored, so the no-logging constraint holds as written.
- **`GROQ_API_KEY` delivery** — `EnvironmentFile=` in the unit, not `.env` plus
  import ordering.

## Open points

1. **`_state` is a private name in `telegram_format.py`.** Imported here as
   `state_label`. Either accept the underscore import or rename it in
   `telegram_format.py` — that file is unhashed, so a rename is free. Decide at
   implementation time; it changes one line either way.
