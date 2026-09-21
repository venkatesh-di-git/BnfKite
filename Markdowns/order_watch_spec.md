# order-watch — automatic order detection and VWAP-equivalent pricing

## Objective

`bench_latency.py order-premium` works but is manual: run it by hand, it
reads the book, prices whatever's live, sends a message. This is the
automatic version — place an order from the app, a message arrives, no
command run.

**No order execution, modification, or trading decision. The daemon never
places or cancels an order.** Same constraint as
`pending_order_black76_telegram_latency_test_v2.md`, carried over exactly.

---

## STATUS — deployed and verified end-to-end on the VM, 21 Sep 2026.

`order_premium.py`, `order_watch.py`, `test_order_watch.py` (15 passing),
`deploy/kite-order-watch.service`, and a healthcheck probe are all written.
`engine_version()` unchanged (`fd8e6f05`) — touches no hashed module.
Neither `order_premium.py` nor `order_watch.py` is collected by
`pytest --collect-only`; `test_order_watch.py` is.

**21 Sep, live, market open:** ran `order_watch.py` locally against the real
session with a temporary diagnostic tag on each detection channel (removed
after the test, not part of the shipped code). A live (non-AMO) order fired
`on_order_update` at 11:23:00.193 — before the next poll tick (11:23:02) even
came due — and a second, pre-existing live order fired it again on every
status transition it made (`OPEN` → `OPEN` → `CANCELLED`), all three times via
the websocket, never via poll. **The websocket works fine for live orders; the
19 Sep AMO finding does not generalize to them.** That closes the one open
question in this doc — see the finding table below.

**21 Sep, deploy: committed (`3ddcaed`), pushed, synced to the VM, unit
enabled and active.** `kite-scanner` was not restarted — nothing in its
core loop imports any of these files. A real order placed afterward on the
VM produced a correct Telegram message: detection, pricing against the
VM's live VWAP, and delivery all confirmed working end-to-end.

---

## Why this exists, and what's measured

Built directly on `bench_latency.py`'s `order-premium` subcommand, which was
verified against real orders on 19 Sep — real strike resolution, real IV
inversion, real VWAP-equivalent repricing, a real Telegram send with the
curated message format. See
[pending_order_black76_telegram_latency_test_v2.md](pending_order_black76_telegram_latency_test_v2.md)
for that history; this doc does not repeat it.

**Measured 19 Sep, live: `on_order_update` does NOT fire for an AMO.**
Listener up, order placed 5s later, 180s window, fired 0 times — while the
order genuinely appeared in the book. That order carries
`exchange_order_id: None`: it never reached the exchange, and the websocket
order stream carries exchange events. Nothing to broadcast.

**Consequence: polling is required, not a fallback.** Anything placed
outside market hours is AMO-only and structurally invisible to the
websocket. This daemon runs both channels always, not websocket-with-a-
polling-fallback.

**Measured 21 Sep, live, market open: `on_order_update` DOES fire for a live
order.** A newly placed order (non-AMO, `exchange_order_id` assigned
immediately) was delivered via the websocket at 11:23:00.193 — strictly
before the next poll tick was even due. A second order already in the book
had every one of its status transitions (`OPEN` → `OPEN` → `CANCELLED`)
delivered the same way, three separate times, never once via poll. Verified
with a temporary per-channel diagnostic tag (`[DIAG ws]` / `[DIAG poll]`) so
there was no ambiguity about which channel actually caught it; removed after
the test.

**So: detection is instant for anything placed during market hours, and
bounded by the 15s poll only for an AMO queued while the market is closed.**
Both channels stay on regardless — the poll is still the only path for AMOs
and remains the safety net if the websocket connection ever drops.

---

## Design

**Caching — one, not a table.** `kite.instruments("NFO")` at startup,
refreshed every 24h. That's the one genuinely expensive call
(~100k rows, seconds — see `bench_latency.py`'s `_fetch_option_chain`
comment). Everything else — LTP, VWAP file read, IV, Black-76 — is on
demand: IV + pricing costs 0.06ms, dwarfed by the ~1200ms median Telegram
send. A precomputed premium table would be wrong the moment it's written,
since IV moves continuously.

**Two detection channels, always both:**
- websocket (`on_order_update`) — instant, when it fires
- `kite.orders()` poll, every 15s — the only path for AMOs, and the safety
  net for everything else

**Split aggregation.** The app splits large orders above the NSE freeze
quantity into several child orders — same symbol, side, price, seconds
apart. Grouped on `(tradingsymbol, transaction_type, price)`, **not**
`parent_order_id` — that field links bracket/cover legs, not freeze-quantity
splits, which arrive as independent orders. This grouping needs no
knowledge of the freeze quantity itself, so it survives NSE changing it.

A 3-second debounce window collects splits before pricing the sum and
sending **one** message. A split landing after the window closed gets its
own message rather than being silently dropped — documented behaviour, not
a bug: below ~2s a slow split risks landing after the message fires; above
~5s the message stops feeling immediate.

**Dedupe.** `on_order_update` fires repeatedly per order
(`PUT ORDER REQ RECEIVED` → `VALIDATION PENDING` → `OPEN PENDING` → `OPEN`).
Each `order_id` is grouped exactly once, the first time it's seen in a live
status — status transitions on an already-grouped order never re-fire.

**Backlog guard.** Orders sitting in the book before the daemon started must
not trigger a message the moment it starts — same problem
`telegram_poller.py`'s `_initial_offset()` and `bench_latency.py`'s Mode B
`_Recorder` both solve: a reference instant at startup, anything timestamped
before it is history. A missing/unparseable timestamp is treated as backlog
too — erring toward silence over a false positive.

**Standalone service, not folded into the scanner** — same precedent as
`telegram_poller.py`. The alert path must not be breakable by a bug here,
and this needs to run whether or not the scanner is up.

---

## Files

| File | Role |
|---|---|
| `order_premium.py` | shared: `LIVE_ORDER_STATUSES`, `resolve_vwap`, `parse_order_timestamp`, `price_order`, `format_order_premium`, `send_telegram`. Used by both `bench_latency.py` (CLI) and `order_watch.py` (daemon) — one implementation, not two |
| `order_watch.py` | the daemon: websocket + poll, split aggregation, dedupe, backlog guard, send |
| `test_order_watch.py` | synthetic, no network — dedupe, backlog, splits, refusals |
| `deploy/kite-order-watch.service` | own unit, `Restart=on-failure`, `RestartSec=30`, `EnvironmentFile=` — mirrors `kite-telegram-poller.service` |
| `healthcheck.py` | gained `_check_order_watch()` — its own state file, called *above* the market-hours gate |

`bench_latency.py` now imports `run_order_premium`, `parse_order_timestamp`
from `order_premium.py` rather than defining its own copies.

---

## VWAP source, unchanged from `order-premium`

`config.OUTPUT_FILE` (`latest_volume_profile.json`), written by the scanner's
own `write_output()` ([engine.py:439](../engine.py#L439)). Two guards, both
required:

1. **Contract match** — the file's `instrument` must equal the current-month
   futures symbol. Refuses on a mismatch (e.g. `BANKNIFTY26AUGFUT` sitting in
   the file after rollover to `SEP`) rather than pricing off the wrong month.
2. **Same-session freshness** — the file's `timestamp` date must be today's.
   It survives the scanner stopping, so a stale VWAP would otherwise read as
   a perfectly ordinary float.

**Known limitation, not yet resolved:** outside market hours the scanner
isn't running, so the file is stale and the guards refuse — a weekend AMO
gets *detected* (via polling) but produces *no message*, because there is no
meaningful VWAP to price it against. Accepted for v1: "premium at VWAP" for
a session that hasn't opened isn't a meaningful number, and substituting the
previous session's would be a different statistic wearing the same label.

---

## healthcheck integration

`_check_order_watch()` probes `systemctl --user is-active kite-order-watch`,
edge-triggered (one alert on down, one on recovery), using its **own** state
file (`ORDER_WATCH_STATE_FILE`) — never the engine check's `STATE_FILE`,
which a shared flag would clobber.

**Called above `main()`'s `is_market_hours()` early return.** The engine
check only matters during market hours; order-watch matters whenever an AMO
might be queued, which is any time. A probe gated on market hours would miss
the service dying overnight before a morning AMO — exactly the failure shape
`telegram_inbound_plan.md` already flagged for the poller probe (never
built) and is now built here first.

No `systemctl` on the host (a dev workstation, not the VM) is not a failure
to alert on — the probe returns 0 silently rather than firing a false "down".

---

## Out of scope for v1

Order **modifications** and **cancellations** don't message — only a newly
detected order group. A status-transition rule to add later, not a
redesign, if wanted.

---

## Deploy checklist — done 21 Sep 2026

- [x] `engine_version()` still `fd8e6f05`
- [x] `python -m pytest -q` green (`test_order_watch.py` included, 398 passed
      / 1 known pre-existing unrelated failure)
- [x] no new dependency — `order_premium.py` and `order_watch.py` use only
      `kiteconnect` and `requests`, both already present
- [x] `git commit` + `push` (`3ddcaed`)
- [x] `./deploy/sync.sh bnvm` — pushes the `*.py` files
- [x] **copy the unit by hand** — `sync.sh` does not carry `deploy/`
      (`scp deploy/kite-order-watch.service bnvm:~/.config/systemd/user/`)
- [x] `systemctl --user daemon-reload && enable --now kite-order-watch` —
      active, connected, `healthcheck.py` on the VM sees it and exits 0

No restart of `kite-scanner` itself needed — nothing in its core loop
imports `order_watch.py`, `order_premium.py`, `bench_latency.py`, or
`healthcheck.py` (confirmed by grep before deploy).

---

## Verification

1. ~~Place a small order → exactly one message, correct quantity.~~ **Done
   21 Sep, live on the VM, market open — message arrived correctly.**
2. Place a size that splits → still one message, quantity = sum of splits.
3. Let an order sit through its status transitions → no repeat messages.
4. Restart the daemon with orders already in the book → no message on
   startup for pre-existing orders.
5. Stop the scanner so the VWAP file goes stale → detects via polling,
   sends nothing, logs the refusal reason.
6. Stop the websocket (kill the process, or block the port) → poll path
   alone still detects and messages.
7. `systemctl --user stop kite-order-watch` → next healthcheck run alerts
   once, not repeatedly; `start` it again → one recovery message.
8. ~~Monday pre-open (~09:00–09:08): the AMO already in the book converts and
   gains an `exchange_order_id`. Record whether `on_order_update` fires on
   that transition.~~ **Done 21 Sep, market open (not pre-open specifically —
   any live order during market hours exercises the same mechanism).
   Confirmed: fires, instantly, on every status transition. See STATUS.**
