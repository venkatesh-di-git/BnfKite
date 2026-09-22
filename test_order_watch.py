"""
Tests for order_watch.py.

This is the daemon that would run unattended against a live account, so the
tests that matter are the same shape as telegram_poller's and ai_overlay's:
what it must NOT do.

  - splits of the same order collapse into ONE message, not N
  - a status transition on an order already grouped never re-fires it
  - orders sitting in the book before the daemon started never message
  - a stale/missing VWAP refuses rather than pricing off a wrong number
  - non-option and wrong-expiry orders are skipped, not priced

No network anywhere: Kite, the websocket, and Telegram are all replaced.
Not `test_order_watch.py` matching `*_test.py`/`test_*.py` by accident —
this one IS meant to be collected, unlike bench_latency.py/order_watch.py
themselves (verified separately that those are not).
"""

from datetime import date, datetime, timedelta

import pytest
from kiteconnect.exceptions import TokenException

import order_premium
import order_watch as ow


SEP_EXP = date(2026, 9, 29)


class FakeContract:
    tradingsymbol = "BANKNIFTY26SEPFUT"
    expiry = SEP_EXP
    instrument_token = 17507842


class FakeKite:
    """orders() is mutable per-test via .orders_to_return; ltp()/instruments()
    are fixed. access_token exists only because OrderWatch's caller reads it
    when constructing KiteTicker — not exercised here."""
    access_token = "fake"

    def __init__(self):
        self.orders_to_return = []

    def orders(self):
        return self.orders_to_return

    def instruments(self, seg):
        return [
            {"instrument_token": 1, "tradingsymbol": "BANKNIFTY26SEP56000CE",
             "strike": 56000.0, "instrument_type": "CE", "expiry": SEP_EXP,
             "lot_size": 30},
            {"instrument_token": 2, "tradingsymbol": "BANKNIFTY26DEC57000CE",
             "strike": 57000.0, "instrument_type": "CE",
             "expiry": date(2026, 12, 29), "lot_size": 30},
            {"instrument_token": 3, "tradingsymbol": "BANKNIFTY26SEPFUT",
             "instrument_type": "FUT", "expiry": SEP_EXP, "lot_size": 30},
        ]

    def ltp(self, keys):
        prices = {"NFO:BANKNIFTY26SEPFUT": 56539.0,
                  "NFO:BANKNIFTY26SEP56000CE": 782.0}
        return {k: {"last_price": prices[k]} for k in keys}


def order(order_id="1", symbol="BANKNIFTY26SEP56000CE", token=1, status="OPEN",
         price=772.0, qty=30, side="BUY", ts=None):
    return {"order_id": order_id, "tradingsymbol": symbol, "instrument_token": token,
           "status": status, "price": price, "quantity": qty,
           "transaction_type": side, "order_timestamp": ts}


@pytest.fixture
def wired(monkeypatch):
    """Fake Kite + captured sends + a resolvable VWAP, all through the SAME
    seams order_watch.py actually imports (from order_premium), so patching
    here is patching what the daemon really calls."""
    sent = []
    monkeypatch.setattr(ow, "send_telegram", lambda text: (sent.append(text), True)[1])
    monkeypatch.setattr(ow, "resolve_vwap", lambda explicit, sym: (56496.0, "test"))
    kite = FakeKite()
    now = datetime(2026, 9, 19, 12, 0, 0)
    watch = ow.OrderWatch(kite, FakeContract(), session_start=now)
    return watch, kite, sent, now


def _settle(watch):
    """Force every pending group's debounce window to have already expired,
    then flush — deterministic, no real sleeping."""
    for group in watch.pending.values():
        group.deadline = 0.0
    watch.flush_due()


# --------------------------------------------------------------- dedupe

def test_a_single_order_produces_one_message(wired):
    watch, kite, sent, now = wired
    ts = now + timedelta(seconds=5)
    watch.observe(order(order_id="A1", ts=ts))
    _settle(watch)
    assert len(sent) == 1
    assert "BANKNIFTY26SEP56000CE" in sent[0]


def test_status_transitions_on_the_same_order_never_refire(wired):
    """on_order_update fires PUT ORDER REQ RECEIVED -> VALIDATION PENDING ->
    OPEN PENDING -> OPEN for ONE order. Without dedupe this is four messages
    for one placement."""
    watch, kite, sent, now = wired
    ts = now + timedelta(seconds=5)
    for status in ("PUT ORDER REQ RECEIVED", "VALIDATION PENDING",
                   "OPEN PENDING", "OPEN"):
        watch.observe(order(order_id="A1", status=status, ts=ts))
    _settle(watch)
    assert len(sent) == 1


def test_seeing_the_same_order_via_both_channels_still_fires_once(wired):
    """The websocket callback and the poll loop both call observe() on every
    order they see — this is the situation that happens every real poll tick,
    not an edge case."""
    watch, kite, sent, now = wired
    ts = now + timedelta(seconds=5)
    watch.observe(order(order_id="A1", ts=ts))  # e.g. from the websocket
    watch.observe(order(order_id="A1", ts=ts))  # e.g. from the next poll tick
    watch.observe(order(order_id="A1", ts=ts))
    _settle(watch)
    assert len(sent) == 1


# ------------------------------------------------------------- backlog

def test_orders_from_before_startup_never_message(wired):
    """The exact failure telegram_poller.py's _initial_offset() and Mode B's
    _Recorder both guard against: a daemon that messages on its entire
    pre-existing order-book history the moment it starts."""
    watch, kite, sent, now = wired
    stale_ts = now - timedelta(hours=2)
    watch.observe(order(order_id="OLD1", ts=stale_ts))
    _settle(watch)
    assert sent == []


def test_a_missing_timestamp_is_treated_as_backlog_not_new(wired):
    """Erring toward silence on a malformed payload, not toward messaging."""
    watch, kite, sent, now = wired
    watch.observe(order(order_id="NOTS", ts=None))
    _settle(watch)
    assert sent == []


def test_an_order_just_after_startup_does_message(wired):
    """The boundary the backlog guard must NOT eat: genuinely new orders
    placed seconds after the daemon starts."""
    watch, kite, sent, now = wired
    watch.observe(order(order_id="NEW1", ts=now + timedelta(seconds=1)))
    _settle(watch)
    assert len(sent) == 1


# ----------------------------------------------------------- split aggregation

def test_splits_of_one_order_collapse_to_one_message_with_summed_qty(wired):
    watch, kite, sent, now = wired
    ts = now + timedelta(seconds=5)
    # Same symbol, side, price — three children of one freeze-quantity split.
    watch.observe(order(order_id="S1", qty=30, price=772.0, ts=ts))
    watch.observe(order(order_id="S2", qty=30, price=772.0, ts=ts))
    watch.observe(order(order_id="S3", qty=15, price=772.0, ts=ts))
    _settle(watch)
    assert len(sent) == 1
    assert "3 splits merged" in sent[0]


def test_different_price_is_a_different_group(wired):
    """Grouped on (symbol, side, price) — a genuinely different limit price
    is a different order, not a split of the same intent."""
    watch, kite, sent, now = wired
    ts = now + timedelta(seconds=5)
    watch.observe(order(order_id="D1", price=772.0, ts=ts))
    watch.observe(order(order_id="D2", price=775.0, ts=ts))
    _settle(watch)
    assert len(sent) == 2


def test_a_late_split_after_debounce_expired_sends_its_own_message(wired):
    """The documented tradeoff: a split landing after the window closed is a
    second message, not a merge into one that already fired. This test
    exercises that the mechanism has no memory of an already-fired group
    key blocking a later, genuinely new placement at the same key."""
    watch, kite, sent, now = wired
    ts = now + timedelta(seconds=5)
    watch.observe(order(order_id="L1", price=772.0, ts=ts))
    _settle(watch)
    assert len(sent) == 1

    watch.observe(order(order_id="L2", price=772.0, ts=ts))
    _settle(watch)
    assert len(sent) == 2


# --------------------------------------------------------------- refusals

def test_a_stale_vwap_refuses_and_sends_nothing(wired, monkeypatch):
    watch, kite, sent, now = wired
    monkeypatch.setattr(ow, "resolve_vwap",
                        lambda explicit, sym: (None, "stale — written yesterday"))
    watch.observe(order(order_id="R1", ts=now + timedelta(seconds=5)))
    _settle(watch)
    assert sent == []


def test_a_non_option_order_is_skipped(wired):
    watch, kite, sent, now = wired
    watch.observe(order(order_id="F1", symbol="BANKNIFTY26SEPFUT", token=3,
                        ts=now + timedelta(seconds=5)))
    _settle(watch)
    assert sent == []


def test_wrong_expiry_is_skipped_not_priced_off_the_wrong_forward(wired):
    watch, kite, sent, now = wired
    watch.observe(order(order_id="W1", symbol="BANKNIFTY26DEC57000CE", token=2,
                        ts=now + timedelta(seconds=5)))
    _settle(watch)
    assert sent == []


def test_an_order_with_no_order_id_is_ignored(wired):
    watch, kite, sent, now = wired
    watch.observe({"tradingsymbol": "BANKNIFTY26SEP56000CE", "status": "OPEN",
                  "price": 772.0, "quantity": 30, "transaction_type": "BUY"})
    _settle(watch)
    assert sent == []


def test_a_non_live_status_is_ignored(wired):
    watch, kite, sent, now = wired
    for status in ("CANCELLED", "COMPLETE", "REJECTED"):
        watch.observe(order(order_id=f"X-{status}", status=status,
                            ts=now + timedelta(seconds=5)))
    _settle(watch)
    assert sent == []


# ---------------------------------------------------------- never trades

def test_orderwatch_has_no_placement_or_cancellation_surface():
    """Static check: this module must never grow a place_order/cancel_order/
    modify_order call. Grepping the source is deliberate here — a mock-based
    test could pass even if a real client call slipped in elsewhere."""
    import inspect
    src = inspect.getsource(ow)
    for forbidden in ("place_order", "cancel_order", "modify_order"):
        assert forbidden not in src, f"{forbidden} must never appear in order_watch.py"


# --------------------------------------------------- session goes stale mid-run

def test_a_token_exception_on_poll_exits_the_process(wired):
    """Measured live 22 Sep: a session that expires WHILE the daemon is
    already running (not just a stale one at startup) surfaced as
    TokenException on kite.orders() forever, logged as a WARNING and
    ignored — both channels silently dead for 6+ hours, service still
    reporting "active" the whole time. TokenException must now be fatal:
    exit_fn is called so systemd restarts the process and it re-reads the
    session cache fresh, rather than looping forever on a dead token."""
    watch, kite, sent, now = wired

    def _raise():
        raise TokenException("Incorrect `api_key` or `access_token`.")
    kite.orders = _raise

    calls = []
    ow._poll_once(kite, watch, exit_fn=lambda code: calls.append(code))
    assert calls == [1]


def test_an_ordinary_poll_failure_does_not_exit(wired):
    """A transient network blip is not the same failure — must not exit the
    process over it, or a single dropped connection kills the daemon."""
    watch, kite, sent, now = wired

    def _raise():
        raise ConnectionError("temporary network blip")
    kite.orders = _raise

    calls = []
    ow._poll_once(kite, watch, exit_fn=lambda code: calls.append(code))
    assert calls == []
