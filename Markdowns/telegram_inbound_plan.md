# Telegram Inbound — Implementation Plan (trimmed)

Covers token exchange via Telegram, and the premarket brief triggering
automatically off whichever login succeeds first each day — manual SSH or
`/login`. Status commands (`/status`, `/health`, `/last`) dropped — seeded
bar count at 09:15 already answers "is it alive."

Query/exchange only, **with one named exception: `/login` restarts the
scanner.** See below. Nothing here changes thresholds, config, or places
orders.

---

## 1. Token exchange via `/login`

**Problem:** daily SSH just to run `token_helper.py`.

**Fix:** send the request token to the bot; the VM does the exchange locally.
The access token never travels through Telegram.

**Flow**

1. You send `/login <request_token>` (copied from the Zerodha redirect URL
   after logging in via the standing Kite login link).
2. Poller reads it, checks sender against `config.TELEGRAM_CHAT_ID`, discards
   anyone else's message.
3. Calls `complete_login(api_key, api_secret, request_token)`
   ([kite_auth.py:113](../kite_auth.py#L113)) — API secret stays on the VM,
   never in the message.
4. That call writes the session cache itself, so step 3 and step 4 are one
   step — same as `token_helper.py` does today
   ([token_helper.py:53](../token_helper.py#L53)).
5. Verifies with `try_cached_session(api_key)`
   ([kite_auth.py:91](../kite_auth.py#L91)) before replying. Kite accepting the
   token is not the same as the scanner being able to read it back — this is
   the check `token_helper.py:65` exists for, and skipping it here would
   reintroduce the failure it was written to catch.
6. **Restarts the scanner** — `systemctl --user restart kite-scanner`. See
   below; this is the step that makes the feature worth having.
7. Replies `✅ Logged in` or `❌ <reason>`.
8. Deletes your original `/login` message via `deleteMessage` — the request
   token is single-use and already spent by this point, this just clears it
   from chat history. Permitted: the Bot API allows a bot to delete incoming
   messages in a private chat.
9. Triggers the premarket brief, once per day — §2.

**Step 6 is the scanner restart, and it is the point of the feature.**

Writing the cache does not fix a running scanner. It holds a dead
`KiteConnect` in memory and will keep holding it; or it exited 78 at startup
and systemd has stopped retrying by design
([deploy/kite-scanner.service](../deploy/kite-scanner.service),
`RestartPreventExitStatus=78`). Either way `/login` without a restart fixes the
file and leaves you with no scanner — the exact failure this replaces an SSH
session to avoid.

**This is the first thing in the design that changes engine state, so it is
stated as an exception rather than smuggled into a flow list.** Bounded
deliberately: `systemctl --user restart kite-scanner`, nothing else. No config
command, no threshold command, no stop command, ever. The blast radius is a
process restart the scanner already performs on its own schedule, triggered by
a message from one allowlisted chat ID that must also carry a valid Kite
request token.

**`on_auth_success()` owns the restart; `token_helper.py` drops its own.**
That script restarts today at [token_helper.py:72](../token_helper.py#L72). If
`on_auth_success()` also restarts, the manual SSH path restarts twice — once
before the brief, once after. One writer, the same discipline that makes
`complete_login` the single writer of the cache format
([token_helper.py:24-26](../token_helper.py#L24-L26)). The
verify-before-restart check at [token_helper.py:65](../token_helper.py#L65)
moves in with it, because that check is what stops a restart into a session the
scanner cannot read back.

```python
def on_auth_success(api_key, restart=True):
    if try_cached_session(api_key) is None:
        return False                      # token saved but unreadable — do not restart
    if restart:
        subprocess.run(["systemctl", "--user", "restart", "kite-scanner"], check=True)
    if not premarket_already_sent_today():
        run_premarket_brief()
        mark_premarket_sent_today()
    return True
```

Order is fixed in both callers: **verify → restart → reply → premarket.** The
reply goes out before the brief because the brief takes up to two minutes.

**Security**
- Chat-ID allowlist — hard requirement, not optional.
- Request token only, never the access token.
- No new secret, no custom header — the chat-ID check plus "never send the
  real credential" already closes the meaningful risk.

**`ALLOWED_CHAT_ID` is not a new setting.** `config.TELEGRAM_CHAT_ID`
([config.py:192](../config.py#L192)) is already the only chat this system
talks to, outbound. A second env var holding the same number is one more thing
to get wrong on the VM, and a mismatch between them fails silently in the
direction that matters — the poller ignoring you while alerts keep arriving.

---

## STATUS — §1 shipped 05 Sep 2026, §2 deferred

§1 (`/login` + restart) is **built, tested and deployed**: `telegram_poller.py`,
`deploy/kite-telegram-poller.service` installed and enabled, unit **active** on
the VM. `test_telegram_poller.py`, 14 passing.

**Never exercised end to end** — no `/login` has been sent yet, because the
workstation lost network before that could happen. That is verification step 2
below and the first thing to try next session.

**One change is local-only, not yet synced:** `validated_chat_id()` and its two
tests. The deployed poller lacks it. Harmless now that `.env` is corrected;
push with `./deploy/sync.sh bnvm` when the network returns.

§2 (premarket auto-trigger) is **deferred** until `premarket.py` exists.

The split was deliberate. §1 has no premarket dependency — it needs only
`complete_login()` and `try_cached_session()`, both of which already existed —
and it addresses a live cost: the scanner sat `failed` from Wed 02 Sep 08:00 to
Sat 05 Sep, three trading days lost to a login nobody was at a terminal to
perform.

**Consequence of the split:** `on_auth_success()` does not exist yet.
`telegram_poller.handle_login()` performs exchange → verify → restart → reply
inline, and `token_helper.py` is **untouched** — it keeps its own restart. When
`premarket.py` lands, extract `on_auth_success()` into it and have both callers
use it, per §2. Until then there is no double-restart, because there is only one
implementation.

**Found while deploying, and it is why §1 is not yet proven — the VM's `.env` held
a `TELEGRAM_CHAT_ID` with a heredoc terminator concatenated onto the end** (e.g.
`123456789EOF`), when the file was written. Telegram is lenient about it: it parses the
leading integer and ignores the rest, so every outbound alert had been
delivering correctly and nothing ever logged an error. But this plan's allowlist
compares strings, so `/login` would have been silently discarded while
`systemctl is-active` reported the poller healthy.

Fixed on the VM (backup at `.env.bak-20260905`), and `telegram_poller.py` now
**refuses to start** on a chat id that is not a clean integer. Refusing rather
than normalising is the point: making an allowlist fuzzy is the wrong direction
for the one check standing between a stranger and a scanner restart.

Note for later: `alert_engine._deliver_telegram` never calls
`raise_for_status()`, so a genuinely bad chat id would fail with nothing in the
journal. Not a live problem — but it is why this one survived unseen.

---

## 2. Premarket brief — auto-trigger on login, once per day

**Problem:** running premarket on a fixed 08:45 clock can fire before you've
actually logged in, producing gaps it didn't need to have.

**Fix:** trigger off "session became valid today," not off which tool
produced that session, and not off a fixed time.

`on_auth_success()` — the full version is in §1, since it also owns the
restart. The premarket half of it is the guard plus the call:

```python
    if not premarket_already_sent_today():
        run_premarket_brief()
        mark_premarket_sent_today()
```

**It lives in `premarket.py`, and both callers import it from there.** Not in
`kite_auth.py`: that would make the auth module import the premarket module,
which imports Kite — a cycle — and would drag `openai` into every process that
authenticates, including the scanner.

Called from **both** places a valid session can originate:
- `token_helper.py`, after a manual SSH login writes the cache
- the Telegram poller, after a successful `/login` exchange

**Guard:** `premarket_already_sent_today()` checks today's date against
`premarket_state.json`'s own `date` field, which the brief already writes. No
second flag file. Prevents a double-send if both paths trigger the same
morning — `/login` at 08:20, then an unrelated manual SSH session later.

**The date must be IST**, via the `_today_str()` pattern at
[kite_auth.py:59](../kite_auth.py#L59), never `date.today()`. A day boundary
tied to host timezone is the difference between this guard holding and it
sending twice.

**08:45 timer stays, but demoted to fallback.** If a valid session already
exists from the previous evening's cache and nothing re-authenticates,
`kite-premarket.timer` still fires and the same guard applies — sends once,
skips if already sent.

Net: premarket sends exactly once per trading day, the moment a working
session exists, regardless of path.

---

## Build

**New file:** `telegram_poller.py`
- Long-polls Telegram's `getUpdates` — no webhook, no exposed port
- Runs as its own systemd service, separate from the scanner
- Only command handled: `/login <request_token>`
- Chat-ID check first, before any dispatch

`getUpdates` is exclusive: Telegram allows one consumer per bot token, and it
conflicts with any webhook. Nothing else consumes updates today — the rest of
the system only calls `sendMessage` — so this is safe now. Worth stating
because a second poller, or a webhook added later, would break both silently
rather than erroring.

**New unit:** `deploy/kite-telegram-poller.service` — `Restart=on-failure`,
independent of `kite-scanner`. `RestartSec=30`, clear of systemd's default
start limit (5 starts / 10s), for the same reason the scanner unit sets it.
Credentials via `EnvironmentFile=`, not `.env` — this process may never
`import config`, and a poller that silently has no bot token is a poller that
looks alive and answers nothing.

**Watching the poller — extend `healthcheck.py`, no new unit.** The timer
already fires every 5 minutes. Two things about that file make this not a
drop-in append:

- **The check must sit ABOVE the market-hours return.** `main()` returns 0
  immediately outside market hours ([healthcheck.py:113](../healthcheck.py#L113))
  and again through the 10-minute open grace
  ([healthcheck.py:122](../healthcheck.py#L122)). The poller matters between
  ~07:30 and 09:15 — entirely inside the window the probe currently skips. A
  check appended to the existing flow would never run when it counts. Give it
  its own window guard before that return.
- **`STATE_FILE` holds one flag.** It is a single `"<state> <date>"` line
  ([healthcheck.py:76-91](../healthcheck.py#L76-L91)). A second edge-triggered
  condition sharing it would clobber the engine flag and break the
  once-per-day guarantee that the date key exists to provide. Use a second
  file, `.poller_state`, and give `_load_state`/`_save_state` a path argument —
  otherwise unchanged.

Probe: `systemctl --user is-active kite-telegram-poller`, non-zero →
`notify("telegram poller is not running")`. Edge-triggered like the engine
check, so a dead poller is one message, not eighteen before the open.

**Touches:**
- ~~`kite_auth.py`~~ — **no change needed.** `complete_login()` is already a
  plain callable taking `(api_key, api_secret, request_token)`
  ([kite_auth.py:113](../kite_auth.py#L113)) and `token_helper.py` already
  calls it as one. The CLI-only version this plan described does not exist.
- `token_helper.py` — delegate to `on_auth_success()`; its own
  `systemctl restart` and `try_cached_session` check move into that function
- `healthcheck.py` — poller liveness check, own window, own state file
- new: `premarket.py` gains `premarket_already_sent_today()` /
  `mark_premarket_sent_today()`, reading/writing the `date` field already
  planned for `premarket_state.json` — using the IST `_today_str()` pattern
  from [kite_auth.py:59](../kite_auth.py#L59), not `date.today()`

**Ordering in `token_helper.py` is load-bearing.** The brief runs at
`reasoning_effort="high"` with a 120s timeout. Calling `on_auth_success()`
before `systemctl --user restart kite-scanner`
([token_helper.py:72](../token_helper.py#L72)) would stall the restart by up to
two minutes on the one morning you are already short of time. Call it after the
restart returns, or spawn it and let the script exit.

No changes to `Decision`, `Gates`, `Grades`, `Latch`, `CSV`, or
`engine_version`.

---

## Explicitly out of scope

- No commands that change config or thresholds. The scanner restart is the one
  named exception, and only as a step inside `/login` — never as a command of
  its own
- No order placement, ever
- No custom auth header / pre-shared secret — chat-ID allowlist is sufficient
  given the access token never transits Telegram
- Dashboard/chart access — not replicable in Telegram, use Tailscale or SSH
  when that's actually needed

---

## Verification

1. Message from a non-allowed chat ID — silently ignored, no reply
2. Valid `/login <token>` — cache written, **scanner restarted and back up
   with a live session**, confirmation sent, original message deleted,
   premarket brief follows
3. Invalid/expired request token — clear failure reply, no crash, no
   premarket trigger
4. Manual SSH login via `token_helper.py` — also triggers premarket, once
5. `/login` at 08:20, then unrelated manual SSH session at 08:50 same day —
   premarket sends only once (guard holds)
6. No login all morning, 08:45 timer fires with a still-valid prior-evening
   cache — premarket sends once, guard prevents a second send if login
   happens afterward
7. Poller crash — scanner keeps running unaffected (separate service)
8. Two `/login` messages in quick succession — second one fails cleanly (the
   request token is single-use), no second premarket send, no second restart
9. Manual `token_helper.py` run — scanner restarts exactly **once**, not twice
10. Scanner stopped at exit 78, then `/login` — comes back up and seeds
    normally; this is the case the restart exists for
11. Kill the poller, wait for the next healthcheck inside 07:30–09:15 —
    one Telegram, and the engine's own down-flag is untouched

---

## Resolved

- **`/login` restarts the scanner** — named exception to query-only, §1. Built
  and deployed; the reply confirms the unit came back `active` rather than
  merely that a restart was issued.
- **`on_auth_success()` owns the restart**; `token_helper.py` drops its own, and
  the verify check moves with it.
- **It lives in `premarket.py`** — putting it in `kite_auth.py` is an import
  cycle.
- **Reply before the brief** — the 120s brief blocks the poller's update loop,
  so `✅ Logged in` must be sent first.
- **`RestartSec=30`** on the poller unit; credentials via `EnvironmentFile=`.
- **Poller liveness** — folded into `healthcheck.py` with its own time window
  and its own state file.

## Open points

None. The egress questions this plan depended on were measured on the VM on
05 Sep 2026 and both sources work — see `premarket_brief_spec_v2.md`, VM facts.
