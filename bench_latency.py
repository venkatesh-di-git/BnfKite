#!/usr/bin/env python3
"""
bench_latency.py — Pending-order -> Black-76 -> Telegram latency benchmark.

See Markdowns/pending_order_black76_telegram_latency_test_v2.md (rev 3) for
the full spec and why each design choice was made. Summary:

Mode A (compute-and-format latency): starts the clock once an instrument is
already known. Valid with the market closed. Measures data retrieval, IV
inversion, Black-76 pricing, and Telegram round-trip.

Mode B (detection latency): market-open only, and THE SCRIPT NEVER PLACES OR
CANCELS AN ORDER. You place an order by hand from your phone; this only
listens, on two channels at once — its own KiteTicker with on_order_update,
and a 15s kite.orders() poll matching what the real system would do — and
reports which one saw it first, and whether the websocket fired at all.

NOT collected by pytest: `python_files` defaults to `test_*.py` and
`*_test.py` (verified empirically), and this name matches neither, so it
cannot land in a normal `pytest -q` run and hit the network or Telegram.

Standalone. No existing file is modified to build this.
"""

import argparse
import json
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from kiteconnect import KiteTicker

import black76
import config
from engine import is_market_hours
from instruments import get_current_month_contract
from kite_auth import try_cached_session

IST = ZoneInfo("Asia/Kolkata")

NFO_OPT_SEGMENT = "NFO-OPT"
BANKNIFTY_NAME = "BANKNIFTY"

# Matches the real system's assumed backstop cadence (rev 2 Mode B, and the
# original spec's polling-backstop mitigation).
POLL_INTERVAL_SECONDS = 15

# Rev 2 said 10-20 iterations per mode, written for a script that placed its
# own orders. Mode B orders now come from a human's phone, so 20 manual
# placements is not realistic; 5 is enough to separate "~1s" from "~15s".
DEFAULT_MODE_B_MAX_ORDERS = 5
DEFAULT_MODE_A_ITERATIONS = 20

TELEGRAM_PREFIX = "\U0001F9EA"  # test-tube emoji — never mistaken for a real alert

# Set by run_mode_a() before run_mode_a_once() is called. Module-level rather
# than threaded through every call because Mode A is single-threaded and
# sequential; there is exactly one futures contract in play per run.
_futures_tradingsymbol: Optional[str] = None


# ===========================================================================
# Setup shared by both modes
# ===========================================================================

def _require_kite_session():
    kite = try_cached_session(config.KITE_API_KEY)
    if kite is None:
        print("No valid Kite session. Log in first: python3 token_helper.py "
             "(or send /login <request_token> to the bot).")
        sys.exit(1)
    return kite


def _require_telegram():
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env.")
        sys.exit(1)


def _fetch_option_chain(kite) -> List[dict]:
    """All live BANKNIFTY NFO-OPT instrument rows.

    instruments.py only filters NFO-FUT (instruments.py:41) — options need
    their own filter, which does not exist yet. One ~100k-row download,
    filtered locally; this is the heaviest call in the whole script.
    """
    all_nfo = kite.instruments("NFO")
    return [i for i in all_nfo
            if i.get("name") == BANKNIFTY_NAME and i.get("segment") == NFO_OPT_SEGMENT]


def _pick_atm_option(chain: List[dict], futures_price: float,
                     option_type: str) -> dict:
    """Nearest expiry, then the strike closest to the futures price."""
    live = [i for i in chain if i.get("instrument_type") == option_type]
    if not live:
        raise RuntimeError(f"no {option_type} rows in the option chain")
    nearest_expiry = min(i["expiry"] for i in live)
    same_expiry = [i for i in live if i["expiry"] == nearest_expiry]
    return min(same_expiry, key=lambda i: abs(i["strike"] - futures_price))


def _send_telegram(text: str) -> bool:
    """Same shape as ai_overlay._send / alert_engine._deliver_telegram —
    deliberately not reusing either, since this must never be able to touch
    the real delivery queue or its dedupe state."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
        return r.ok
    except Exception as e:
        print(f"  Telegram send failed: {type(e).__name__}: {e}")
        return False


# ===========================================================================
# Mode A — compute-and-format latency
# ===========================================================================

@dataclass
class ModeAResult:
    ok: bool
    iv_solved: bool
    t0: float
    t1: float
    t2: float
    t3: float
    t4: float
    t5: float
    error: Optional[str] = None

    @property
    def data_retrieval(self) -> float:
        return self.t1 - self.t0

    @property
    def iv_and_pricing(self) -> float:
        return self.t3 - self.t1

    @property
    def telegram(self) -> float:
        return self.t5 - self.t4

    @property
    def total(self) -> float:
        return self.t5 - self.t0


def run_mode_a_once(kite, instrument: dict, r: float) -> ModeAResult:
    t0 = time.perf_counter()

    # T0 -> T1: data retrieval. Two separate ltp() calls, matching the flow's
    # two separate stages ("Get latest actual Bank Nifty Futures price" then
    # "Get latest actual option LTP") rather than one combined quote() call.
    try:
        futures_key = f"NFO:{_futures_tradingsymbol}"
        option_key = f"NFO:{instrument['tradingsymbol']}"
        futures_price = kite.ltp([futures_key])[futures_key]["last_price"]
        option_price = kite.ltp([option_key])[option_key]["last_price"]
    except Exception as e:
        t1 = time.perf_counter()
        return ModeAResult(False, False, t0, t1, t1, t1, t1, t1,
                           error=f"data retrieval failed: {e}")
    t1 = time.perf_counter()

    # T1 -> T2 -> T3: IV inversion, then price + Greeks at that IV.
    expiry = instrument["expiry"]
    now_ist = datetime.now(IST).replace(tzinfo=None)
    days_to_expiry = (datetime.combine(expiry, datetime.min.time()) - now_ist).total_seconds() / 86400
    T = max(days_to_expiry, 0.0001) / 365.0
    is_call = instrument["instrument_type"] == "CE"

    iv = black76.implied_vol(futures_price, instrument["strike"], T, r,
                             option_price, is_call)
    t2 = time.perf_counter()

    iv_solved = iv is not None
    if iv_solved:
        theo_price = black76.price(futures_price, instrument["strike"], T, r, iv, is_call)
        g = black76.greeks(futures_price, instrument["strike"], T, r, iv, is_call)
        summary = (f"IV={iv:.4f} theo={theo_price:.2f} delta={g.delta:.3f} "
                  f"vega={g.vega:.2f}")
    else:
        # A stale/zero LTP with the market closed cannot bracket for IV — the
        # iteration still records its timings rather than aborting.
        summary = "IV solve failed (no root in bracket — stale/closed-market quote)"
    t3 = time.perf_counter()

    # T4 -> T5: Telegram round-trip.
    t4 = time.perf_counter()
    text = (f"{TELEGRAM_PREFIX} bench_latency Mode A\n"
           f"{instrument['tradingsymbol']}  F={futures_price} opt={option_price}\n"
           f"{summary}")
    ok = _send_telegram(text)
    t5 = time.perf_counter()

    return ModeAResult(ok, iv_solved, t0, t1, t2, t3, t4, t5)


def run_mode_a(kite, iterations: int, tradingsymbol: Optional[str],
              option_type: str) -> None:
    global _futures_tradingsymbol

    print(f"Mode A — {iterations} iterations, market "
         f"{'OPEN' if is_market_hours(datetime.now(IST)) else 'CLOSED'}")

    contract = get_current_month_contract(kite)
    if contract is None:
        print("Could not resolve a BANKNIFTY futures contract. Aborting.")
        return
    _futures_tradingsymbol = contract.tradingsymbol

    chain = _fetch_option_chain(kite)
    if tradingsymbol:
        matches = [i for i in chain if i["tradingsymbol"] == tradingsymbol]
        if not matches:
            print(f"'{tradingsymbol}' not found in the live BANKNIFTY option chain.")
            return
        instrument = matches[0]
    else:
        fut_key = f"NFO:{contract.tradingsymbol}"
        futures_ltp = kite.ltp([fut_key])[fut_key]["last_price"]
        instrument = _pick_atm_option(chain, futures_ltp, option_type)

    print(f"Instrument: {instrument['tradingsymbol']} "
         f"(strike {instrument['strike']}, expiry {instrument['expiry']}, "
         f"lot_size {instrument['lot_size']})")
    print(f"Risk-free rate: {black76.DEFAULT_RISK_FREE_RATE:.3%} (hardcoded — see the spec)\n")

    results: List[ModeAResult] = []
    for i in range(1, iterations + 1):
        res = run_mode_a_once(kite, instrument, black76.DEFAULT_RISK_FREE_RATE)
        results.append(res)
        status = "ok" if res.ok else f"FAILED ({res.error})"
        iv_note = "" if res.iv_solved else "  [no IV root]"
        print(f"  [{i:2d}/{iterations}] total={res.total*1000:7.2f}ms  "
             f"data={res.data_retrieval*1000:6.2f}ms  "
             f"iv+pricing={res.iv_and_pricing*1000:6.2f}ms  "
             f"telegram={res.telegram*1000:7.2f}ms  {status}{iv_note}")

    _print_stage_stats("Mode A", {
        "Data retrieval": [r.data_retrieval for r in results],
        "IV + Black-76":  [r.iv_and_pricing for r in results],
        "Telegram":       [r.telegram for r in results],
        "TOTAL":          [r.total for r in results],
    })
    solved = sum(1 for r in results if r.iv_solved)
    print(f"\nIV solved in {solved}/{iterations} iterations.")
    if solved < iterations:
        print("Some iterations had no IV root — expected with stale/closed-"
             "market quotes; their timings are still counted above.")


def _print_stage_stats(label: str, stages: Dict[str, List[float]]) -> None:
    n = len(next(iter(stages.values())))
    print(f"\n{label} — per-stage stats (ms), n={n}")
    print(f"{'stage':<18}{'min':>9}{'median':>9}{'avg':>9}{'p95':>9}{'max':>9}")
    for name, values in stages.items():
        values_ms = sorted(v * 1000 for v in values)
        if not values_ms:
            continue
        p95_idx = min(len(values_ms) - 1, int(round(0.95 * (len(values_ms) - 1))))
        print(f"{name:<18}{values_ms[0]:9.2f}{statistics.median(values_ms):9.2f}"
             f"{statistics.fmean(values_ms):9.2f}{values_ms[p95_idx]:9.2f}{values_ms[-1]:9.2f}")


# ===========================================================================
# Mode B — detection latency. LISTEN ONLY. Never places or cancels an order.
# ===========================================================================

@dataclass
class _OrderSighting:
    order_timestamp: Optional[datetime] = None  # naive IST, ±1s — see the spec
    ws_arrival: Optional[datetime] = None
    ws_perf: Optional[float] = None
    ws_status: Optional[str] = None
    poll_arrival: Optional[datetime] = None
    poll_perf: Optional[float] = None


class _Recorder:
    """Shared, lock-protected state between the websocket thread and the
    polling thread. Both channels write; only the main thread reads to
    decide when to stop and what to print."""

    def __init__(self, session_start: datetime, max_orders: int):
        self.session_start = session_start
        self.max_orders = max_orders
        self.lock = threading.Lock()
        self.orders: Dict[str, _OrderSighting] = {}
        self.ws_ever_fired = False

    def _is_new(self, order_timestamp: Optional[datetime]) -> bool:
        """Ignore anything already sitting in the order book before the
        harness started — a stale order from an earlier test run must not be
        mistaken for one just placed. Caller holds self.lock."""
        if order_timestamp is None:
            return True  # can't tell; err toward recording it
        return order_timestamp >= self.session_start - timedelta(seconds=2)

    def record_ws(self, order_id: str, order_timestamp: Optional[datetime],
                  status: str) -> None:
        with self.lock:
            self.ws_ever_fired = True
            if not self._is_new(order_timestamp):
                return
            s = self.orders.setdefault(order_id, _OrderSighting())
            if s.ws_arrival is None:  # first sighting only
                s.ws_arrival = datetime.now(IST).replace(tzinfo=None)
                s.ws_perf = time.perf_counter()
                s.ws_status = status
                if s.order_timestamp is None:
                    s.order_timestamp = order_timestamp

    def record_poll(self, order_id: str, order_timestamp: Optional[datetime]) -> None:
        with self.lock:
            if not self._is_new(order_timestamp):
                return
            s = self.orders.setdefault(order_id, _OrderSighting())
            if s.poll_arrival is None:
                s.poll_arrival = datetime.now(IST).replace(tzinfo=None)
                s.poll_perf = time.perf_counter()
                if s.order_timestamp is None:
                    s.order_timestamp = order_timestamp

    def done(self) -> bool:
        with self.lock:
            return len(self.orders) >= self.max_orders

    def count(self) -> int:
        with self.lock:
            return len(self.orders)


def _parse_order_timestamp(value) -> Optional[datetime]:
    """The REST orders() response comes back already parsed into a datetime
    by kiteconnect's _format_response (only when the string is exactly 19
    chars, i.e. second resolution — confirming the ±1s bracket). The
    websocket postback does NOT go through that parser (ticker.py's
    _parse_text_message hands the raw JSON dict straight to on_order_update),
    so there it arrives as a string. Handle both.

    NOT independently verified against a live postback payload — the market
    was closed while this was written. If the field name or format differs
    on the day, this is the function to fix; see the spec's "Running it".
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def _make_ws_handler(recorder: _Recorder):
    def on_order_update(ws, data):
        order_id = data.get("order_id")
        if not order_id:
            return
        ts = _parse_order_timestamp(data.get("order_timestamp")
                                    or data.get("exchange_timestamp"))
        recorder.record_ws(order_id, ts, data.get("status", "?"))
    return on_order_update


def _polling_loop(kite, recorder: _Recorder, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            for o in kite.orders():
                order_id = o.get("order_id")
                if not order_id:
                    continue
                ts = _parse_order_timestamp(o.get("order_timestamp"))
                recorder.record_poll(order_id, ts)
        except Exception as e:
            print(f"  [poll] error: {type(e).__name__}: {e}")
        stop_event.wait(POLL_INTERVAL_SECONDS)


# ===========================================================================
# order-premium — price the pending order's own strike against futures VWAP
# ===========================================================================

# Statuses that mean "this order is still live in the book". Everything else
# (CANCELLED / COMPLETE / REJECTED) is history and must not be priced.
LIVE_ORDER_STATUSES = ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")


def resolve_vwap(explicit, futures_symbol: str):
    """(vwap, source_label) on success, (None, reason) on refusal.

    Two sources only, by design:
      --vwap            supplied by hand (closed-market testing)
      the project's own VWAP, which the scanner already computes and writes to
      config.OUTPUT_FILE via engine.write_output() (engine.py:439)

    Kite's own `average_price` is deliberately NOT used: it returns 0 outside
    market hours, and the whole point is to reuse the number this project
    already calculates rather than introduce a second, differently-derived one.

    THE STALENESS GUARDS ARE THE POINT OF THIS FUNCTION. That JSON file
    survives the scanner stopping, so yesterday's VWAP reads as a perfectly
    ordinary float. Pricing a September strike against an August VWAP would
    produce a confident, wrong number with nothing to hint at it — so this
    refuses instead, on either a contract mismatch or a date that is not
    today's session.
    """
    if explicit is not None:
        return float(explicit), "supplied via --vwap"

    path = config.OUTPUT_FILE
    if not os.path.exists(path):
        return None, (f"{path} not found — the scanner has never written it on "
                     f"this machine")
    try:
        with open(path) as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"could not read {path}: {type(e).__name__}: {e}"

    vwap = payload.get("vwap")
    if vwap is None:
        return None, f"{os.path.basename(path)} carries no vwap value"

    instrument = payload.get("instrument")
    if instrument != futures_symbol:
        return None, (f"contract mismatch — file has {instrument}, current "
                     f"month is {futures_symbol}")

    stamp = payload.get("timestamp")
    try:
        written = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None, f"unparseable timestamp in {os.path.basename(path)}: {stamp!r}"
    if written.tzinfo is None:
        written = written.replace(tzinfo=IST)

    now = datetime.now(IST)
    if written.astimezone(IST).date() != now.date():
        return None, (f"stale — written {written.astimezone(IST):%Y-%m-%d %H:%M:%S}, "
                     f"not today's session")

    age = (now - written).total_seconds()
    return float(vwap), f"{os.path.basename(path)} (age {age:.0f}s)"


def _format_order_premium(symbol, side, qty, status, limit_price, premium,
                          diff, pct, fut_ltp, vwap, days) -> str:
    """Curated to ONE thing the Kite app cannot show.

    No IV, no Greeks, no option VWAP — the app already displays all three on
    the same screen he places the order from, so repeating them here is just
    re-sending what he is already looking at.

    What the app cannot do is reprice this strike off a DIFFERENT underlying
    level: what it is worth if the future mean-reverts to its VWAP. That
    cross-instrument number is the only reason this message exists.
    """
    verdict = "CHEAP" if diff > 0 else "RICH"
    gap = fut_ltp - vwap
    side_word = "above" if gap >= 0 else "below"
    lines = [
        "📊 " + str(symbol),
        f"{side} {qty} @ {limit_price} · {status}",
        "",
        f"If BNF @ VWAP {vwap:g} → {premium:.2f}",
        f"Your limit {limit_price} → {diff:+.2f} ({pct:+.2f}%) {verdict}",
        "",
        f"BNF {fut_ltp:g} spot · {abs(gap):.0f} pts {side_word} VWAP · {days:.1f}d",
    ]
    return chr(10).join(lines)


def run_order_premium(kite, explicit_vwap, send_telegram: bool = True) -> None:
    orders = kite.orders()
    live = [o for o in orders if o.get("status") in LIVE_ORDER_STATUSES]
    if not live:
        print("No live orders in the book — nothing to price.")
        print(f"(Looked for: {', '.join(LIVE_ORDER_STATUSES)})")
        return

    contract = get_current_month_contract(kite)
    if contract is None:
        print("Could not resolve a BANKNIFTY futures contract. Aborting.")
        return

    vwap, source = resolve_vwap(explicit_vwap, contract.tradingsymbol)
    if vwap is None:
        print(f"Cannot resolve VWAP: {source}")
        print("Pass --vwap <number> to supply it explicitly.")
        return

    fut_key = f"NFO:{contract.tradingsymbol}"
    fut_ltp = kite.ltp([fut_key])[fut_key]["last_price"]

    # One instruments download, indexed by token — the order carries
    # instrument_token, which is exact, unlike parsing the tradingsymbol.
    by_token = {i["instrument_token"]: i for i in kite.instruments("NFO")}

    r = black76.DEFAULT_RISK_FREE_RATE
    print(f"Futures : {contract.tradingsymbol}  LTP {fut_ltp}  VWAP {vwap}")
    print(f"VWAP src: {source}")
    print(f"Rate    : {r:.3%} (hardcoded)\n")

    for o in live:
        symbol = o.get("tradingsymbol")
        inst = by_token.get(o.get("instrument_token"))
        if inst is None:
            print(f"{symbol}: not found in the NFO dump — skipped.\n")
            continue
        if inst.get("instrument_type") not in ("CE", "PE"):
            print(f"{symbol}: not an option ({inst.get('instrument_type')}) — "
                 f"Black-76 does not apply, skipped.\n")
            continue

        strike = inst["strike"]
        is_call = inst["instrument_type"] == "CE"
        expiry = inst["expiry"]
        limit_price = o.get("price")

        # THE FORWARD MUST MATCH THE OPTION'S EXPIRY. Black-76 prices an option
        # off the futures of its OWN expiry; pricing a December option against
        # September's VWAP is simply the wrong forward, and it would look
        # perfectly ordinary in the output. Refuse rather than mislead — the
        # VWAP in hand belongs to one contract only.
        if (expiry.year, expiry.month) != (contract.expiry.year, contract.expiry.month):
            print(f"{symbol}: expiry {expiry} is not the contract the VWAP "
                 f"belongs to ({contract.tradingsymbol}, {contract.expiry}) — "
                 f"skipped rather than priced off the wrong forward." + chr(10))
            continue

        opt_key = f"NFO:{symbol}"
        opt_ltp = kite.ltp([opt_key])[opt_key]["last_price"]

        now_naive = datetime.now(IST).replace(tzinfo=None)
        days = (datetime.combine(expiry, datetime.min.time()) - now_naive).total_seconds() / 86400
        T = max(days, 0.0001) / 365.0

        # IV from the option's OWN market price at the CURRENT futures level...
        iv = black76.implied_vol(fut_ltp, strike, T, r, opt_ltp, is_call)

        print(f"{symbol}   {o.get('transaction_type')} {o.get('quantity')} "
             f"@ {limit_price}   [{o.get('status')}]")
        print(f"  strike {strike:.0f} {inst['instrument_type']}  "
             f"expiry {expiry}  ({days:.2f}d)  LTP {opt_ltp}")

        if iv is None:
            print("  IV: no root in bracket (stale/closed-market quote) — "
                 "cannot price.\n")
            continue

        # ...then the same strike re-priced as if the future sat at its VWAP.
        premium = black76.price(vwap, strike, T, r, iv, is_call)
        g = black76.greeks(vwap, strike, T, r, iv, is_call)
        diff = premium - limit_price if limit_price else None

        print(f"  IV {iv:.4f}  ->  equivalent premium at VWAP: {premium:.2f}")
        if diff is not None:
            verdict = "limit is CHEAP vs VWAP" if diff > 0 else "limit is RICH vs VWAP"
            pct = (diff / limit_price * 100) if limit_price else 0.0
            print(f"  vs limit {limit_price}: {diff:+.2f} ({pct:+.2f}%)  — {verdict}")
        print(f"  delta {g.delta:.3f}  gamma {g.gamma:.6f}  vega {g.vega:.2f}  "
             f"theta {g.theta / 365:.2f}/day")

        if send_telegram and diff is not None:
            text = _format_order_premium(
                symbol, o.get("transaction_type"), o.get("quantity"),
                o.get("status"), limit_price, premium, diff, pct,
                fut_ltp, vwap, days)
            print("  telegram:", "sent" if _send_telegram(text) else "FAILED")
        print()


def check_ws_connectivity(kite, timeout_seconds: float = 10.0) -> bool:
    """Connect-only smoke test — no market-hours gate, no order listening, no
    Telegram. Proves the api_key/access_token/websocket handshake works at
    all, independent of whether the market is open, so it's the one piece of
    Mode B that IS testable on a closed-market day.

    Rev 3's "Running it: Today" section promised this ("start the websocket
    and confirm it connects"); the original run_mode_b() couldn't actually
    deliver it because its market-hours gate returns before ever calling
    kws.connect(). This closes that gap rather than leaving the doc
    promising something the code didn't do.

    Touches no order state — nothing here can place, modify, or cancel.
    """
    result = {"connected": False, "error": None}
    done = threading.Event()

    def on_connect(ws, response):
        result["connected"] = True
        done.set()

    def on_error(ws, code, reason):
        result["error"] = reason or f"error (code {code})"
        done.set()

    def on_close(ws, code, reason):
        if not done.is_set():  # only meaningful if we never connected
            result["error"] = reason or f"closed before connecting (code {code})"
            done.set()

    kws = KiteTicker(config.KITE_API_KEY, kite.access_token)
    kws.on_connect = on_connect
    kws.on_error = on_error
    kws.on_close = on_close

    print(f"Connecting (up to {timeout_seconds:.0f}s)...")
    kws.connect(threaded=True)
    done.wait(timeout_seconds)
    kws.close()

    if result["connected"]:
        print("  [ws] CONNECTED — api_key/access_token and the websocket "
             "handshake are good.")
        print("  This proves the connection works, NOT that on_order_update")
        print("  fires for a real order — that still needs Monday, market open.")
    else:
        print(f"  [ws] FAILED to connect within {timeout_seconds:.0f}s: "
             f"{result['error'] or 'no callback fired (timed out)'}")
    return result["connected"]


def run_mode_b(kite, max_orders: int) -> None:
    now = datetime.now(IST)
    if not is_market_hours(now):
        print("Mode B's order-listening loop requires the market to be open "
             "(orders can only be placed and detected live).")
        print("Run `python bench_latency.py check-ws` instead to at least "
             "confirm the websocket connects — that part needs no market "
             "hours. Or come back during market hours for the full test.")
        return

    print(f"Mode B — listening for up to {max_orders} phone-placed orders.")
    print("This script NEVER places or cancels an order. Place one from your")
    print("phone (any instrument, any side) whenever you're ready.\n")

    session_start = now.replace(tzinfo=None)
    recorder = _Recorder(session_start, max_orders)

    kws = KiteTicker(config.KITE_API_KEY, kite.access_token)
    kws.on_order_update = _make_ws_handler(recorder)
    kws.on_connect = lambda ws, resp: print("  [ws] connected.")
    kws.on_close = lambda ws, code, reason: print(f"  [ws] closed: {reason}")
    kws.on_error = lambda ws, code, reason: print(f"  [ws] error: {reason}")
    kws.connect(threaded=True)

    stop_event = threading.Event()
    poll_thread = threading.Thread(target=_polling_loop, args=(kite, recorder, stop_event),
                                   daemon=True)
    poll_thread.start()

    seen_before = 0
    try:
        while not recorder.done():
            time.sleep(1)
            n = recorder.count()
            if n != seen_before:
                print(f"  ... {n}/{max_orders} order(s) captured")
                seen_before = n
    except KeyboardInterrupt:
        print("\nStopped by Ctrl+C.")
    finally:
        stop_event.set()
        kws.close()

    _report_mode_b(recorder)


def _report_mode_b(recorder: _Recorder) -> None:
    print(f"\nMode B — {len(recorder.orders)} order(s) captured.\n")

    # THE PRIMARY FINDING. Binary, and must never be conflated with the
    # polling number.
    if not recorder.ws_ever_fired:
        print("*** on_order_update NEVER FIRED for a phone-placed order. ***")
        print("Only the polling backstop saw anything — detection ceiling is")
        print("~15s (POLL_INTERVAL_SECONDS), and order-price-retuning off")
        print("real-time detection is not viable as designed for externally-")
        print("placed orders. This is the spec's own decision criterion.\n")
    else:
        print("on_order_update DID fire for at least one phone-placed order.\n")

    ws_brackets, poll_brackets, penalties = [], [], []
    for order_id, s in recorder.orders.items():
        ws_str = "never fired"
        if s.ws_arrival is not None and s.order_timestamp is not None:
            lo = (s.ws_arrival - s.order_timestamp - timedelta(seconds=1)).total_seconds()
            hi = (s.ws_arrival - s.order_timestamp).total_seconds()
            ws_str = f"[{lo:.1f}s, {hi:.1f}s]  (status={s.ws_status})"
            ws_brackets.append(hi)

        poll_str = "never seen"
        if s.poll_arrival is not None and s.order_timestamp is not None:
            lo = (s.poll_arrival - s.order_timestamp - timedelta(seconds=1)).total_seconds()
            hi = (s.poll_arrival - s.order_timestamp).total_seconds()
            poll_str = f"[{lo:.1f}s, {hi:.1f}s]"
            poll_brackets.append(hi)

        print(f"  order {order_id}  order_timestamp={s.order_timestamp}")
        print(f"    websocket: {ws_str}")
        print(f"    polling:   {poll_str}")

        if s.ws_perf is not None and s.poll_perf is not None:
            penalty = s.poll_perf - s.ws_perf  # exact — both are local perf_counter()
            penalties.append(penalty)
            print(f"    polling penalty (exact, local clock): {penalty:.2f}s")
        print()

    if ws_brackets:
        print(f"Websocket detection bracket (upper bound), n={len(ws_brackets)}: "
             f"min={min(ws_brackets):.1f}s median={statistics.median(ws_brackets):.1f}s "
             f"max={max(ws_brackets):.1f}s")
    if poll_brackets:
        print(f"Polling detection bracket (upper bound), n={len(poll_brackets)}: "
             f"min={min(poll_brackets):.1f}s median={statistics.median(poll_brackets):.1f}s "
             f"max={max(poll_brackets):.1f}s")
    if penalties:
        print(f"Exact polling penalty, n={len(penalties)}: "
             f"min={min(penalties):.2f}s median={statistics.median(penalties):.2f}s "
             f"max={max(penalties):.2f}s")


# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    a = sub.add_parser("mode-a", help="compute-and-format latency (market open or closed)")
    a.add_argument("--iterations", type=int, default=DEFAULT_MODE_A_ITERATIONS)
    a.add_argument("--tradingsymbol", default=None,
                   help="use this exact option instead of auto-picking ATM")
    a.add_argument("--option-type", choices=["CE", "PE"], default="CE")

    b = sub.add_parser("mode-b", help="detection latency (market open, phone-placed orders only)")
    b.add_argument("--max-orders", type=int, default=DEFAULT_MODE_B_MAX_ORDERS)

    c = sub.add_parser("check-ws", help="websocket connect/disconnect smoke test "
                       "— no market hours needed, no orders, no Telegram")
    c.add_argument("--timeout", type=float, default=10.0)

    p = sub.add_parser("order-premium", help="price the live order's own strike "
                       "against current-month futures VWAP")
    p.add_argument("--vwap", type=float, default=None,
                   help="supply VWAP explicitly; omit to read the project's own "
                        "VWAP from config.OUTPUT_FILE")
    p.add_argument("--no-telegram", action="store_true",
                   help="print only; do not send the curated message")

    args = parser.parse_args()

    kite = _require_kite_session()
    # Only Mode A sends anything to Telegram. Mode B and check-ws never did —
    # gating them on it too was an unconditional check left over from before
    # check-ws existed, and it meant a bare detection/connectivity test could
    # not even start on a machine with no TELEGRAM_* configured.
    if args.mode == "mode-a" or (args.mode == "order-premium"
                                and not args.no_telegram):
        _require_telegram()

    if args.mode == "mode-a":
        run_mode_a(kite, args.iterations, args.tradingsymbol, args.option_type)
    elif args.mode == "mode-b":
        run_mode_b(kite, args.max_orders)
    elif args.mode == "order-premium":
        run_order_premium(kite, args.vwap, send_telegram=not args.no_telegram)
    else:
        check_ws_connectivity(kite, args.timeout)


if __name__ == "__main__":
    main()
