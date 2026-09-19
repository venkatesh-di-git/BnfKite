# Pending Order → Black-76 → Telegram Latency Test (rev 3)

## Objective
Measure real end-to-end latency using actual Kite data, from **order
placement** to sending a Telegram alert — including detection lag, not just
compute.

**No order execution, modification, or trading decision. The script never
places or cancels an order.**

---

## STATUS — built + connectivity check added 19 Sep 2026. Awaiting Mon 21 Sep.

`black76.py`, `test_black76.py` (16 passing), `bench_latency.py` all written.
No existing file touched. `bench_latency.py` confirmed absent from
`pytest --collect-only`.

Reuse ended up one function wider than originally listed:
`get_current_month_contract(kite)` from
[instruments.py:53](../instruments.py#L53) is used to resolve the futures
contract for Mode A rather than reimplementing the `NFO-FUT` filter — same
import-only reuse as everything else in the table below.

`_parse_order_timestamp()` handles the REST `orders()` shape (already a
`datetime`, via kiteconnect's own parser) and the websocket postback shape
(a raw string, since the postback path bypasses that parser) — but the
**websocket postback's exact field names were not verified against a live
payload**, because the market was closed while this was built. If Monday's
run shows `order_id`/`order_timestamp`/`status` under different keys, that
function is the one to fix. This is the one open risk in an otherwise
verified implementation.

### What today's session-with-a-closed-market added

1. **`check-ws` — a new subcommand, no market-hours gate.** The original
   `run_mode_b()` refused to run at all on a closed day, which meant the
   "start the websocket and confirm it connects" promise below (**Running
   it: Today**) was not actually deliverable by the code as first written.
   `check_ws_connectivity()` connects, waits up to 10s for `on_connect` /
   `on_error`, reports which, and disconnects — no order listening, no
   Telegram, nothing that can place or cancel anything. Verified against a
   faked ticker (both the connect and the error path); the real handshake
   is still unverified pending a live session (next point).
2. **A fresh LOCAL Kite login is needed before ANY of this touches real
   data — independent of market hours.** `bench_latency.py` authenticates
   through `kite_auth.try_cached_session()`, which reads the same
   `.kite_session_cache.json` `token_helper.py` / `/login` write — a
   completely separate session from any other Kite connector that might be
   logged in elsewhere. Kite tokens expire daily regardless of whether the
   market opens, so **Mode A and `check-ws` both need that fresh login
   first, every day**, not just today.
3. **Kite accepts an order request on a closed-market day** — it does not
   reject it. A regular/limit order placed outside market hours is
   auto-converted to an AMO and comes back with status `AMO REQ RECEIVED`.
   That status means the request is queued, **not yet on the exchange** —
   Kite pushes AMO requests to NSE in the pre-open window (roughly
   09:00–09:08 IST) ahead of the next session's open. Confirmed against a
   live order book today (order details deliberately not reproduced here —
   see the redaction note in the repo's `.gitignore`).

   **This changes what Monday should watch for.** An AMO placed today
   converting to a live exchange order during Monday's pre-open is a
   second, independent detection event — distinct from a fresh order
   placed from the phone during the regular session — and it is worth
   checking `on_order_update` against *both* transitions, not just the
   second one.
4. **Order timestamp format is not uniform across Kite API surfaces.**
   Confirmed today: the REST `orders()` call through the `kiteconnect`
   Python SDK returns a naive, second-resolution `datetime` (via the SDK's
   own parser, matching the ±1s bracket already documented under
   **Timing**); at least one other Kite API surface returns a full
   ISO-8601 string with a timezone offset for the same field. This doesn't
   resolve the open risk above about the websocket postback's exact
   shape — if anything it reinforces it. **On Monday, have
   `_parse_order_timestamp()` print the raw value on first sight**, so a
   format mismatch is visible immediately rather than silently returning
   `None` and being mistaken for "never fired."
5. **Fixed while adding `check-ws`:** `main()` was requiring
   `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` unconditionally, even for Mode
   B, which never sends anything to Telegram. Only Mode A is gated on
   Telegram config now — a bare detection or connectivity test no longer
   needs Telegram configured at all.

Rev 3 supersedes rev 2's Mode B, which was not buildable: it assumed a
detection mechanism that exists nowhere in the repo.
`kite_ticker.py` registers `on_connect` / `on_ticks` / `on_close` / `on_error`
/ `on_reconnect` / `on_noreconnect` and **no `on_order_update`**; `postback`
appears in no file. The spec's deciding number rested on code nobody had
written.

Rev 3 also fixes the placement model: **orders are placed from the phone, by
hand. The script only listens.** No `place_order` path exists in it, not even a
disabled one. That is added if and when auto-trading is built, not before.

---

## Why this revision exists

Rev 1 started the clock at "order detected." That skips the actual open
question: how long between placing an order and the tool *noticing* it.

Order-update postbacks reliably cover orders placed through the API app.
Orders placed **externally — mobile or Kite web — may not fire the same way**,
mitigated only by a `kite.orders()` polling backstop. The externally-placed
case is the one that decides the feature, and it is what rev 3 measures.

Black-76 compute was measured in rev 1 at 0.034–0.33ms and was never the
bottleneck. Detection lag might be. Both are measured here and reported
separately so one cannot hide the other.

> Rev 1 and "the order-price-retuning feature" are not in this repo. The facts
> that matter from them are restated inline above and below, so this document
> stands on its own.

---

## Flow

```text
Order placed on the phone (T-1)
        ↓
Tool detects the order (T0)              ← THE STAGE THAT WAS MISSING
        ↓
Extract instrument
        ↓
Auto-detect Strike + CE/PE + Expiry
        ↓
Get latest actual Bank Nifty Futures price
        ↓
Get latest actual option LTP
        ↓
Risk-free rate (constant — see IV)
        ↓
Calculate IV from option LTP using Black-76 inversion
        ↓
Calculate Black-76 theoretical price + Greeks
        ↓
Generate dummy Telegram alert
        ↓
Record Telegram response
```

---

## Two test modes — run both, report separately

### Mode A — Compute-and-format latency (market open or closed)

Starts at T0 with the instrument already known. Since nothing is placed, take
the tradingsymbol as an argument or auto-pick ATM from the chain — for a
latency measurement the instrument's provenance is irrelevant.

**Valid with market closed**, using latest available Kite data. No fill
required, no order required.

A stale or zero LTP with the market shut will not bracket for IV inversion.
**A failed solve still records its timings** rather than aborting the
iteration — this measures latency, not pricing.

### Mode B, part 1 — connectivity check (works today, no market hours)

`python bench_latency.py check-ws` — connects the websocket, waits up to
10s for `on_connect` or `on_error`, reports which, disconnects. No order
listening, no Telegram, no market-hours gate. Needs only a fresh local Kite
login (see STATUS above — that login itself needs no market hours either).

This is the one piece of Mode B genuinely testable before Monday: it proves
the `api_key` / `access_token` / websocket handshake all work. It does
**not** prove `on_order_update` fires for a real order — that is exactly
what part 2 still needs market hours for.

### Mode B, part 2 — Detection latency (market open only, phone-placed)

Two listeners run together, both stamping arrival on the local monotonic
clock:

1. **WebSocket** — the script's own `KiteTicker` with `on_order_update` set.
   Its own instance in its own process; `kite_ticker.py` is not modified. Kite
   allows several connections per api_key and the scanner uses one — confirm
   headroom on the day if the scanner is running.
2. **Polling backstop** — `kite.orders()` every 15s, matching what the real
   system would do, so the measured ceiling is the real one rather than the
   theoretical one.

The script prints "place an order from your phone now" and waits. First
sighting of a new `order_id` on either path is recorded, with which path saw
it and the `order_timestamp` / `exchange_timestamp` it carried.

`on_order_update` fires several times per order (`PUT ORDER REQ RECEIVED` →
`VALIDATION PENDING` → `OPEN PENDING` → `OPEN`). **Record first arrival per
`order_id`** and note the status it carried; ignore the rest.

**The primary output is binary, not a number: did `on_order_update` fire at
all for a phone-placed order?**

If it never fires and only polling sees the order, that answers the whole
spec — polling is the only mechanism, the ceiling is ~15s, and the feature is
not viable as designed. The script must distinguish "fired" from "timed out
waiting" explicitly, and must never fall through to the polling number as
though it were the websocket's.

The script does **not** cancel the order. It is a real order on a real
account, placed by hand; cancelling it is the placer's business, and touching
it would breach "no order modification".

---

## Inputs

- **Strike / CE-PE / Expiry:** from the detected order's trading symbol
- **Futures price:** latest Kite market data
- **Option LTP:** latest Kite market data
- **Time to expiry:** calculated from actual expiry timestamp
- **Risk-free rate:** a constant — see below

Option instruments need an `NFO-OPT` filter, which does not exist:
`instruments.py` matches `NFO-FUT` only ([instruments.py:41](../instruments.py#L41)).
Fetch `kite.instruments("NFO")` **once** and filter locally; it is a ~100k-row
download and the heaviest call here. Read `lot_size` from the instrument row,
never hardcoded.

---

## IV

Solve:

```text
Black76(F, K, T, r, IV) = Actual Option LTP
```

**Bisection over `[0.01, 3.0]` to 1e-6, not Brent.** Brent means `scipy`,
which is not installed and is a heavy add on a 1GB e2-micro — to optimise the
one stage this spec already measured at 0.034–0.33ms and called "never the
bottleneck". Bisection is ~22 evaluations of a closed form: microseconds, and
no new dependency.

**Risk-free rate is a hardcoded constant.** No source was ever named and there
is no free Indian T-bill API. For a short-dated option `e^(-rT) ≈ 1`, and
6.5% vs 7.0% moves IV in the fourth decimal. This measures latency, not
pricing accuracy. A constant with a comment removes a fetch, a cache, and an
unanswered question.

---

## Timing

Record high-resolution timestamps:

```text
T-1 = order actually placed  (Mode B — from order_timestamp, ±1s; see below)
T0  = tool detects the order
T1  = instrument/data retrieval complete
T2  = IV calculation complete
T3  = Black-76 calculation complete
T4  = Telegram request sent
T5  = Telegram response received
```

| Measure | From | Precision |
|---|---|---|
| WebSocket detection | ws_arrival − order_timestamp | ±1s bracket |
| Polling detection | poll_arrival − order_timestamp | ±1s bracket |
| **Polling penalty** | **poll_arrival − ws_arrival** | **exact** |
| Data retrieval | T1 − T0 | exact |
| IV + Black-76 | T3 − T1 | exact |
| Telegram | T5 − T4 | exact |
| Total (Mode A) | T5 − T0 | exact |
| **Total (Mode B)** | **T5 − T-1** | **±1s** |

**Why ±1s, and why that is good enough.** The script places nothing, so there
is no local placement instant to read — `order_timestamp` is the only
reference for a phone-placed order, it is truncated to the second, and it
comes off Zerodha's clock rather than the VM's. True placement therefore lies
in `[order_ts, order_ts+1)`. **Report detection as that interval, not a
point.** The decision this feeds is "~1s or ~15s", which ±1s separates
cleanly.

The polling-penalty row is the clean number: both stamps are the local
monotonic clock, so it carries no quantisation and no clock skew, and it says
exactly what the backstop costs versus the websocket.

Use `perf_counter()` for all durations; keep wall-clock separately, only for
correlating against Kite's timestamps.

**Iterations.** Rev 2 said 10–20 per mode, written on the assumption the
script placed the orders. Twenty manual placements from a phone is not
realistic. Mode B loops open-ended — record each placement, print running
stats, stop on Ctrl+C — and **5 is enough** for a ~1s vs ~15s call. Mode A
stays automated at 20. Report min / median / average / P95 / max per stage.

---

## Market closed

Mode A only. **The report states explicitly that Mode B was not run and why**,
so the result is not mistaken for a full validation. A clean websocket connect
with the market shut is not evidence that `on_order_update` fires —
it cannot fire, and saying so is part of the report.

---

## Files

Three new files. **Nothing existing is modified** — no `requirements.txt`, no
`kite_ticker.py`, no `sync.sh` (it already globs `*.py`), no new dependency.

| File | Contents |
|---|---|
| `black76.py` | pure pricing, Greeks, IV inversion. No Kite, no network, no I/O |
| `bench_latency.py` | the harness: instruments, websocket, polling, Telegram, stats |
| `test_black76.py` | put-call parity and a known value |

**Black-76 is its own module on purpose.** It is the only part with a life
beyond this test — order-price-retuning would need the same pricing, and
importing it from a benchmark script is worse than the alternative, copying
it, which leaves two pricing models to drift. `volume_profile.py` is the
existing pattern: pure math, dataclass result, tested alongside.

**Not `latency_test.py`.** Verified empirically: pytest's default
`python_files` covers `test_*.py` **and** `*_test.py`, so that name is
collected into the normal `pytest -q` run and would hit the network mid-suite.
`bench_latency.py` is not collected.

Reuse, import-only:

| Need | Use |
|---|---|
| Kite session | `try_cached_session(config.KITE_API_KEY)` — [kite_auth.py:91](../kite_auth.py#L91) |
| Market-hours guard | `is_market_hours(now)` — [engine.py:431](../engine.py#L431), handles weekends |
| Telegram creds | `config.TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |

Telegram test messages carry a `🧪` prefix so they are never mistaken for
alerts.

---

## Constraints

- Python
- Kite Connect
- Telegram Bot API
- No AI
- No dashboard
- No database
- **No order placement, modification or cancellation by the script**
- No new dependency
- Standalone scripts; no existing file touched

---

## Success criteria

For each mode independently, determine whether latency is closer to
**sub-second, 1–3 seconds, 5+ seconds, or ~15 seconds**, and identify the
actual bottleneck stage.

**Decision this feeds:** if Mode B's total lands near the 15s polling ceiling
— or if `on_order_update` never fires for a phone-placed order at all —
order-price-retuning off pending-order detection is not viable as designed;
the table would consistently lag the order it is meant to inform. That would
mean either building real-time order-update handling that works for external
orders, or dropping auto-activation in favour of a manual trigger.

---

## Running it — what's actually testable, and when

Three tiers, not two. The dividing line is NOT just market hours — a fresh
local Kite login gates two of the three tiers on its own, every day,
regardless of whether the market is open.

**Tier 1 — right now, no login at all:**
```
python -m pytest test_black76.py -q
```
Pure math, 16 tests, no Kite, no network. Already passing.

**Tier 2 — today, but only AFTER a fresh local login** (`/login` via the
Telegram poller, or `python3 token_helper.py` — works any day, this is the
same daily Kite expiry every login flow deals with, nothing today-specific):
```
python bench_latency.py check-ws        # proves the handshake works
python bench_latency.py mode-a          # real last-close data, real Telegram sends
```
Both need only a valid session — neither needs market hours. If the local
`.kite_session_cache.json` is stale (check its `date` field against today),
this is the blocking step; nothing below it runs without it.

**Tier 3 — Mon 21 Sep, 09:15 IST, market open:**
```
python bench_latency.py mode-b
```
Needs the same fresh login as Tier 2, plus the market actually open. Two
things to watch for, not one — see STATUS point 3: whichever AMO is sitting
in the order book converting during pre-open (~09:00–09:08 IST), and a fresh
order placed from the phone during the regular session. Both are legitimate
`on_order_update` tests; capture whichever comes first.

## Verification

1. `python -m pytest -q` — collection unchanged; `bench_latency.py` not picked
   up, `test_black76.py` passes. Needs no login, works right now.
2. `python -c "from engine import engine_version; print(engine_version())"` —
   unchanged; this touches no hashed module. Needs no login either.
3. After a fresh local login: `python bench_latency.py check-ws` reports
   `[ws] CONNECTED`, not a timeout or an auth error.
4. After a fresh local login: Mode A completes, prints per-stage stats, and a
   failed IV solve still reports timings rather than aborting.
5. Monday: the binary result — did `on_order_update` fire at all, for either
   the AMO's pre-open conversion or a fresh phone order.
6. Cross-check one order's `order_timestamp` against the Kite order book by
   eye, to confirm the bracket is computed the right way round — and check
   what `_parse_order_timestamp()` printed for the raw value (STATUS point
   4), since the exact format was unverified going into Monday.
