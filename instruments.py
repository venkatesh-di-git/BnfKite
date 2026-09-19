"""
instruments.py

Resolves the instrument_token for the current-month Bank Nifty futures
contract, since Kite's historical data API needs a token, not a symbol.

V1 behaviour: always picks the NEAREST expiry (i.e. current-month by
definition), which naturally rolls over once that contract expires.

NOT YET IMPLEMENTED (per futures_rollover_addendum.md): the proactive
3-day-before-expiry liquidity comparison (volume/OI shift to next-month)
is a planned V2 addition. For now this will flag when you're within 3
trading days of expiry so you know to sanity-check manually, but it
will not auto-switch early.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

from kiteconnect import KiteConnect

BANKNIFTY_FUT_NAME = "BANKNIFTY"
NFO_SEGMENT = "NFO-FUT"


@dataclass
class FutureContract:
    instrument_token: int
    tradingsymbol: str
    expiry: date
    lot_size: int


def get_banknifty_futures(kite: KiteConnect) -> List[FutureContract]:
    """Fetch all live Bank Nifty futures contracts from the NFO instrument dump."""
    instruments = kite.instruments("NFO")

    contracts = []
    for inst in instruments:
        if inst.get("name") == BANKNIFTY_FUT_NAME and inst.get("segment") == NFO_SEGMENT:
            contracts.append(FutureContract(
                instrument_token=inst["instrument_token"],
                tradingsymbol=inst["tradingsymbol"],
                expiry=inst["expiry"],
                lot_size=inst["lot_size"],
            ))

    contracts.sort(key=lambda c: c.expiry)
    return contracts


def get_current_month_contract(kite: KiteConnect) -> Optional[FutureContract]:
    """Returns the nearest-expiry Bank Nifty futures contract (V1: no early rollover)."""
    contracts = get_banknifty_futures(kite)
    today = date.today()

    live_contracts = [c for c in contracts if c.expiry >= today]
    if not live_contracts:
        return None

    current = live_contracts[0]

    days_to_expiry = (current.expiry - today).days
    if 0 <= days_to_expiry <= 3:
        print(
            f"NOTE: {current.tradingsymbol} expires in {days_to_expiry} day(s) "
            f"({current.expiry}). Per your rollover rules, compare volume/OI "
            f"against the next-month contract manually before relying on this "
            f"profile for entries — auto-rollover isn't implemented yet."
        )
        print(
            "      Also treat the FIRST session on the new contract with "
            "suspicion: the volume baseline is warmed from that contract's "
            "prior sessions, where it was the far month and thinly traded. "
            "A low baseline inflates relative volume, so Volume can read High "
            "(and grades A+) on ordinary activity all morning. Known issue — "
            "no automatic guard."
        )

    return current
