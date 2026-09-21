"""
order_premium.py — price a live order's own strike against futures VWAP.

EXTRACTED from bench_latency.py so production code (order_watch.py) never
imports a benchmark harness. bench_latency.py's `order-premium` subcommand
now imports from here too — this is the one implementation, not two.

Kite's own `average_price` is deliberately NOT used as a VWAP source: it
returns 0 outside market hours, and the whole point is to reuse the number
this project already calculates (engine.write_output(), engine.py:439)
rather than introduce a second, differently-derived one.

Never places, modifies, or cancels an order. Reads the book, prices what it
finds, sends a message. That's the entire surface.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import black76
import config
from instruments import get_current_month_contract

IST = ZoneInfo("Asia/Kolkata")

# Statuses that mean "this order is still live in the book". Everything else
# (CANCELLED / COMPLETE / REJECTED) is history and must not be priced.
LIVE_ORDER_STATUSES = ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")


def send_telegram(text: str) -> bool:
    """Own copy, deliberately not shared with bench_latency.py's Mode A sender
    or ai_overlay.py's — same shape as both (alert_engine.py:459's pattern),
    but each caller owns its own so none of them can accidentally end up
    routing through another's delivery path or dedupe state."""
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


def parse_order_timestamp(value):
    """Normalise a Kite order_timestamp to a naive datetime, regardless of
    which API surface it came from.

    The REST orders() response comes back already parsed into a datetime by
    kiteconnect's own _format_response (only when the string is exactly 19
    chars, i.e. second resolution). The websocket postback does NOT go
    through that parser (ticker.py's _parse_text_message hands the raw JSON
    dict straight to on_order_update), so there it arrives as a string.
    Shared by bench_latency.py's Mode B and order_watch.py's startup-backlog
    filter — both need the identical normalisation.

    NOT independently verified against a live postback payload as of 19 Sep
    2026 — the market was closed while this was written. If the field name
    or format differs on the day, this is the function to fix.
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


def resolve_vwap(explicit, futures_symbol: str):
    """(vwap, source_label) on success, (None, reason) on refusal.

    Two sources only, by design:
      explicit          supplied by hand (closed-market testing, or a caller
                        that already has the number)
      the project's own VWAP, which the scanner already computes and writes to
      config.OUTPUT_FILE via engine.write_output() (engine.py:439)

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


def price_order(kite, o: dict, by_token: dict, contract, vwap: float, fut_ltp: float,
                r: float = black76.DEFAULT_RISK_FREE_RATE):
    """Price ONE order against the given futures VWAP. Returns a dict of every
    field the CLI and the daemon both need, or None with a printed reason for
    every refusal path (not-an-instrument, wrong expiry, no IV root) — so
    both callers get identical behaviour for identical inputs.

    `fut_ltp` is the caller's ONE snapshot, taken once before pricing any
    order in a run — not refetched here, so every order in the same run (and
    every split in the same group) is priced against the identical futures
    tick rather than a slightly different one per order.

    This is the per-order body of the CLI loop, factored out so order_watch.py
    can call it on one order (or one aggregated split-group) without
    reimplementing the expiry guard or the IV inversion.
    """
    symbol = o.get("tradingsymbol")
    inst = by_token.get(o.get("instrument_token"))
    if inst is None:
        print(f"{symbol}: not found in the NFO dump — skipped.\n")
        return None
    if inst.get("instrument_type") not in ("CE", "PE"):
        print(f"{symbol}: not an option ({inst.get('instrument_type')}) — "
             f"Black-76 does not apply, skipped.\n")
        return None

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
             f"skipped rather than priced off the wrong forward.\n")
        return None

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
        return None

    # ...then the same strike re-priced as if the future sat at its VWAP.
    premium = black76.price(vwap, strike, T, r, iv, is_call)
    g = black76.greeks(vwap, strike, T, r, iv, is_call)
    diff = (premium - limit_price) if limit_price else None
    pct = (diff / limit_price * 100) if (diff is not None and limit_price) else None

    print(f"  IV {iv:.4f}  ->  equivalent premium at VWAP: {premium:.2f}")
    if diff is not None:
        verdict = "limit is CHEAP vs VWAP" if diff > 0 else "limit is RICH vs VWAP"
        print(f"  vs limit {limit_price}: {diff:+.2f} ({pct:+.2f}%)  — {verdict}")
    print(f"  delta {g.delta:.3f}  gamma {g.gamma:.6f}  vega {g.vega:.2f}  "
         f"theta {g.theta / 365:.2f}/day")

    if diff is None:
        return None

    return {
        "symbol": symbol, "side": o.get("transaction_type"),
        "qty": o.get("quantity"), "status": o.get("status"),
        "limit_price": limit_price, "premium": premium, "diff": diff,
        "pct": pct, "days": days,
    }


def format_order_premium(symbol, side, qty, status, limit_price, premium,
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


def run_order_premium(kite, explicit_vwap, send_telegram_flag: bool = True) -> None:
    """The CLI entry point — bench_latency.py's `order-premium` subcommand
    calls this unchanged. Loops every live order, prints, optionally sends."""
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
        priced = price_order(kite, o, by_token, contract, vwap, fut_ltp, r)
        if priced is None:
            continue

        if send_telegram_flag:
            text = format_order_premium(
                priced["symbol"], priced["side"], priced["qty"], priced["status"],
                priced["limit_price"], priced["premium"], priced["diff"],
                priced["pct"], fut_ltp, vwap, priced["days"])
            print("  telegram:", "sent" if send_telegram(text) else "FAILED")
        print()
