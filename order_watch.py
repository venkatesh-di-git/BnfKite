#!/usr/bin/env python3
"""
order_watch.py — detect a placed order automatically, price it, message once.

WHY THIS EXISTS. bench_latency.py's `order-premium` works but is manual: run
it by hand, it reads the book, prices whatever's live, sends a message. This
is the automatic version — order placed from the app, message arrives, no
command run.

WHAT'S MEASURED, NOT ASSUMED (19 Sep 2026):
  - on_order_update does NOT fire for an AMO. Verified live: listener up,
    order placed 5s later, 180s window, fired 0 times, while the order
    genuinely appeared in the book. That order carries exchange_order_id:
    None — it never reached the exchange, and the websocket stream carries
    exchange events, so there is nothing for it to broadcast.
  - Therefore POLLING IS REQUIRED, not a fallback. Anything placed outside
    market hours is AMO-only and structurally invisible to the websocket.
  - Whether the websocket fires for a LIVE order (one that gets an
    exchange_order_id immediately) is still open — that is Monday's test,
    at pre-open when the AMO already in the book converts.

TWO DETECTION CHANNELS, ALWAYS BOTH:
  - websocket (on_order_update) — instant, when it fires
  - kite.orders() poll, every POLL_INTERVAL_SECONDS — the only path for AMOs,
    and the safety net for everything else

SPLIT AGGREGATION. The app splits large orders above the NSE freeze quantity
into several child orders — same symbol, side, price, seconds apart. Grouped
on (tradingsymbol, transaction_type, price), NOT parent_order_id (that links
bracket/cover legs, not freeze-quantity splits, which arrive as independent
orders). A DEBOUNCE_SECONDS window collects splits before pricing the sum
and sending ONE message.

DEDUPE. on_order_update fires several times per order as it moves through
status (PUT ORDER REQ RECEIVED -> VALIDATION PENDING -> OPEN PENDING -> OPEN
[-> AMO REQ RECEIVED for a queued one]). Each order_id is only ever placed
into a group once, the first time it's seen live.

WHAT IT NEVER DOES. Never places, modifies, or cancels an order — reads the
book, prices what it finds, sends a message. That is the entire surface.

Standalone service, not folded into the scanner — same precedent as
telegram_poller.py. The alert path must not be breakable by a bug here, and
this needs to run whether or not the scanner is up.
"""

import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from kiteconnect import KiteTicker

import config
from instruments import get_current_month_contract
from kite_auth import try_cached_session
from order_premium import (LIVE_ORDER_STATUSES, format_order_premium,
                           parse_order_timestamp, price_order, resolve_vwap,
                           send_telegram)

logger = logging.getLogger("order_watch")

IST = ZoneInfo("Asia/Kolkata")

# Matches bench_latency.py's Mode B backstop cadence — the same 15s figure
# that was measured, not guessed, as an acceptable ceiling for the poll path.
POLL_INTERVAL_SECONDS = 15

# How long a split group waits for further splits before pricing and sending.
# Same tradeoff bench_latency.py's plan settled on: too short and a slow
# split lands after the message (a second message); too long and it stops
# feeling immediate.
DEBOUNCE_SECONDS = 3.0

# How often the debounce flusher checks for due groups. Well under
# DEBOUNCE_SECONDS so the 3s window is honoured precisely, not rounded up to
# the next tick.
FLUSH_CHECK_SECONDS = 0.5

# Refresh the instruments dump this often. It's a ~100k-row download (the
# heaviest call anywhere in this project — see bench_latency.py's
# _fetch_option_chain), so it is cached, not fetched per order; refreshed
# daily in case contracts roll or new strikes list intraday.
INSTRUMENTS_REFRESH_SECONDS = 24 * 3600

GroupKey = Tuple[str, str, float]  # (tradingsymbol, transaction_type, price)


@dataclass
class _PendingGroup:
    key: GroupKey
    orders: List[dict] = field(default_factory=list)
    deadline: float = 0.0  # time.monotonic()


class OrderWatch:
    """Owns all state. observe() is called by both the websocket callback and
    the poll loop — safe from either thread, everything behind one lock."""

    def __init__(self, kite, contract, session_start: datetime):
        self.kite = kite
        self.contract = contract
        # Orders from before the daemon started are the backlog problem
        # telegram_poller.py's _initial_offset() and bench_latency.py's Mode B
        # _Recorder both solve the same way: a reference instant, and anything
        # timestamped before it is history, not a new event to message on.
        self.session_start = session_start

        self.lock = threading.Lock()
        self.seen_order_ids: set = set()
        self.pending: Dict[GroupKey, _PendingGroup] = {}

        self.by_token: Dict[int, dict] = {}
        self._instruments_loaded_at: Optional[float] = None
        self._refresh_instruments()

    # ------------------------------------------------------------- caching

    def _refresh_instruments(self) -> None:
        self.by_token = {i["instrument_token"]: i for i in self.kite.instruments("NFO")}
        self._instruments_loaded_at = time.monotonic()
        logger.info("instruments cache refreshed: %d rows", len(self.by_token))

    def _maybe_refresh_instruments(self) -> None:
        if (self._instruments_loaded_at is None or
                time.monotonic() - self._instruments_loaded_at > INSTRUMENTS_REFRESH_SECONDS):
            self._refresh_instruments()

    # --------------------------------------------------------- observation

    def _group_key(self, o: dict) -> Optional[GroupKey]:
        symbol, side, price = o.get("tradingsymbol"), o.get("transaction_type"), o.get("price")
        if symbol is None or side is None or price is None:
            return None
        return (symbol, side, price)

    def observe(self, o: dict) -> None:
        """Called by EITHER channel for EVERY order it sees — live or not,
        old or new. Filters down to "genuinely new, still live" before doing
        anything, so both channels can call this freely without coordinating."""
        order_id = o.get("order_id")
        status = o.get("status")
        if not order_id or status not in LIVE_ORDER_STATUSES:
            return

        with self.lock:
            if order_id in self.seen_order_ids:
                return  # dedupe — a status transition on an order already grouped

            ts = parse_order_timestamp(o.get("order_timestamp")
                                       or o.get("exchange_timestamp"))
            # Backlog guard: anything sitting in the book before this process
            # started is history, not a fresh placement. ts is None only for
            # a malformed payload — err toward NOT messaging on it rather
            # than risk firing on stale backlog with an unparseable stamp.
            if ts is None or ts < self.session_start - timedelta(seconds=2):
                self.seen_order_ids.add(order_id)
                return

            self.seen_order_ids.add(order_id)
            key = self._group_key(o)
            if key is None:
                logger.warning("order %s missing symbol/side/price — cannot group",
                               order_id)
                return
            group = self.pending.setdefault(key, _PendingGroup(key=key))
            group.orders.append(o)
            group.deadline = time.monotonic() + DEBOUNCE_SECONDS

    # -------------------------------------------------------------- flush

    def flush_due(self) -> None:
        """Pull out every group whose debounce window has expired, price and
        send each. Pricing/Telegram happen OUTSIDE the lock — both can be
        slow (network), and holding the lock across them would stall
        observe() on the other channel's thread for no reason."""
        now = time.monotonic()
        due: List[_PendingGroup] = []
        with self.lock:
            for key in [k for k, g in self.pending.items() if now >= g.deadline]:
                due.append(self.pending.pop(key))

        for group in due:
            self._price_and_send(group)

    def _price_and_send(self, group: _PendingGroup) -> None:
        self._maybe_refresh_instruments()

        merged = dict(group.orders[0])
        merged["quantity"] = sum((o.get("quantity") or 0) for o in group.orders)
        n = len(group.orders)
        split_note = f" ({n} splits merged)" if n > 1 else ""

        vwap, source = resolve_vwap(None, self.contract.tradingsymbol)
        if vwap is None:
            logger.warning("skipping %s%s: %s", merged.get("tradingsymbol"),
                           split_note, source)
            return

        fut_key = f"NFO:{self.contract.tradingsymbol}"
        try:
            fut_ltp = self.kite.ltp([fut_key])[fut_key]["last_price"]
        except Exception as e:
            logger.warning("could not fetch futures LTP: %s: %s", type(e).__name__, e)
            return

        priced = price_order(self.kite, merged, self.by_token, self.contract,
                             vwap, fut_ltp)
        if priced is None:
            return

        logger.info("pricing %s%s: limit %s vs VWAP-equivalent %.2f",
                   priced["symbol"], split_note, priced["limit_price"],
                   priced["premium"])

        text = format_order_premium(
            priced["symbol"], priced["side"], priced["qty"], priced["status"],
            priced["limit_price"], priced["premium"], priced["diff"],
            priced["pct"], fut_ltp, vwap, priced["days"])
        if split_note:
            text += f"\n{n} splits merged"
        ok = send_telegram(text)
        logger.info("telegram: %s", "sent" if ok else "FAILED")


# =============================================================================


def _make_ws_handler(watch: OrderWatch):
    def on_order_update(ws, data):
        watch.observe(data)
    return on_order_update


def _polling_loop(kite, watch: OrderWatch, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            for o in kite.orders():
                watch.observe(o)
        except Exception as e:
            logger.warning("poll failed: %s: %s", type(e).__name__, e)
        stop_event.wait(POLL_INTERVAL_SECONDS)


def _flush_loop(watch: OrderWatch, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        watch.flush_due()
        stop_event.wait(FLUSH_CHECK_SECONDS)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env.")
        return 1

    kite = try_cached_session(config.KITE_API_KEY)
    if kite is None:
        print("No valid Kite session. Log in first: python3 token_helper.py "
             "(or send /login <request_token> to the bot).")
        return 1

    contract = get_current_month_contract(kite)
    if contract is None:
        print("Could not resolve a BANKNIFTY futures contract. Aborting.")
        return 1

    session_start = datetime.now(IST).replace(tzinfo=None)
    watch = OrderWatch(kite, contract, session_start)

    kws = KiteTicker(config.KITE_API_KEY, kite.access_token)
    kws.on_order_update = _make_ws_handler(watch)
    kws.on_connect = lambda ws, resp: logger.info("websocket connected")
    kws.on_close = lambda ws, code, reason: logger.warning("websocket closed: %s", reason)
    kws.on_error = lambda ws, code, reason: logger.warning("websocket error: %s", reason)
    kws.connect(threaded=True)

    stop_event = threading.Event()
    poll_thread = threading.Thread(target=_polling_loop, args=(kite, watch, stop_event),
                                   daemon=True, name="order-watch-poll")
    flush_thread = threading.Thread(target=_flush_loop, args=(watch, stop_event),
                                    daemon=True, name="order-watch-flush")
    poll_thread.start()
    flush_thread.start()

    logger.info("watching %s — websocket + %ds poll, %ds split debounce",
               contract.tradingsymbol, POLL_INTERVAL_SECONDS, int(DEBOUNCE_SECONDS))

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        kws.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
