"""
kite_ticker.py

Thin wrapper around kiteconnect.ticker.KiteTicker: owns the websocket
connection lifecycle (connect / subscribe / mode / reconnect) for a
single instrument, and normalizes each tick into the
(price, cumulative_volume, oi, timestamp) shape that
LiveFiveMinuteSession.apply_tick() (engine.py) expects. Neither app.py
nor main.py talk to KiteTicker directly.
"""

from datetime import datetime
from threading import Lock
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from kiteconnect import KiteTicker

IST = ZoneInfo("Asia/Kolkata")

OnTick = Callable[[float, float, Optional[float], datetime], None]


class LiveTickFeed:
    """One instrument, one websocket connection, FULL mode (OI is only
    included in FULL-mode packets — QUOTE mode omits it).

    KiteTicker reconnects on its own by default (reconnect=True), so we
    just track connected/last_error for the UI and let it retry. The
    connection is only torn down for good in stop().
    """

    def __init__(self, api_key: str, access_token: str, instrument_token: int, on_tick: OnTick):
        self.instrument_token = instrument_token
        self.on_tick = on_tick

        self.connected = False
        self.last_error: Optional[str] = None
        self._last_tick_at: Optional[datetime] = None
        self._lock = Lock()

        self.kws = KiteTicker(api_key, access_token)
        self.kws.on_connect = self._on_connect
        self.kws.on_ticks = self._on_ticks
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect
        self.kws.on_noreconnect = self._on_noreconnect

    def start(self):
        """Non-blocking — runs the websocket on a background thread."""
        self.kws.connect(threaded=True)

    def stop(self):
        """Stop retrying and close the connection. Deliberately NOT
        KiteTicker.stop() — that stops the shared Twisted reactor for
        the whole process, which would break any future feed too."""
        self.kws.close()
        self.connected = False

    def seconds_since_last_tick(self) -> Optional[float]:
        with self._lock:
            last = self._last_tick_at
        if last is None:
            return None
        return (datetime.now(IST) - last).total_seconds()

    # -----------------------------------------------------------
    # KiteTicker callbacks — invoked on the websocket's own thread.
    # -----------------------------------------------------------
    def _on_connect(self, ws, response):
        self.connected = True
        self.last_error = None
        ws.subscribe([self.instrument_token])
        ws.set_mode(ws.MODE_FULL, [self.instrument_token])

    def _on_close(self, ws, code, reason):
        self.connected = False
        self.last_error = reason or f"connection closed (code {code})"

    def _on_error(self, ws, code, reason):
        self.connected = False
        self.last_error = reason or f"error (code {code})"

    def _on_reconnect(self, ws, attempts_count):
        self.last_error = f"reconnecting (attempt {attempts_count})"

    def _on_noreconnect(self, ws):
        self.connected = False
        self.last_error = "gave up reconnecting"

    def _on_ticks(self, ws, ticks):
        for tick in ticks:
            if tick.get("instrument_token") != self.instrument_token:
                continue
            price = tick.get("last_price")
            volume = tick.get("volume_traded")
            if price is None or volume is None:
                continue
            oi = tick.get("oi")
            # Kite's tick timestamps are naive IST, same as historical_data's.
            timestamp = tick.get("exchange_timestamp") or tick.get("last_trade_time") or datetime.now(IST)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=IST)
            with self._lock:
                self._last_tick_at = datetime.now(IST)
            self.on_tick(price, volume, oi, timestamp)
