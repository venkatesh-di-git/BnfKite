"""
replay/data.py — the candle archive, and the replay engine's data source.

These are deliberately ONE thing. The archive decision and the replay cache spec
described the same artifact twice: 1-minute candles with OI, one day per request,
keyed by day, never silently rewritten. Building both would mean two copies of
every candle and two gap checks that drift apart.

WHY THIS IS TIME-CRITICAL. Expired monthly futures are dropped from
kite.instruments("NFO") entirely — BANKNIFTY26JULFUT already returns nothing — and
historical_data needs a token. So the moment a contract settles, its candles become
permanently unreachable. BANKNIFTY26AUGFUT settles 25 Aug 2026; everything it has
must be on disk before then.

KEYED ON tradingsymbol, NOT instrument_token. The token dies with the contract and
becomes unresolvable; the symbol is what still means something afterwards.

ONE DAY PER REQUEST. Kite documents a 60-day window for minute data, but there is
an unresolved report of only ~4 trading days coming back per request. A bulk pull
would return partial data with no way to tell — and for an archive with a hard
deadline, a silent gap is the one failure that cannot be repaired later.
"""

import argparse
import calendar
import gzip
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo("Asia/Kolkata")

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "archive")

# Written when a weekday legitimately has no candles — a holiday, or before the
# contract listed. Without it every backfill re-requests those days forever, and
# "no file" could not be distinguished from "fetch failed", which is exactly the
# distinction the gap check exists to make.
EMPTY_MARKER = ".empty"


def _day_path(tradingsymbol: str, day: date, suffix: str = ".json.gz") -> str:
    return os.path.join(ARCHIVE_DIR, tradingsymbol, f"{day:%Y-%m-%d}{suffix}")


def _read(path: str) -> list:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _write(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write to a temp name and rename: a process killed mid-write would otherwise
    # leave a truncated file that reads as a cache hit forever after.
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    os.replace(tmp, path)


def _normalise(raw: list) -> list:
    """Kite rows -> plain JSON, timestamps ISO with an explicit IST offset.

    Naive timestamps get IST attached rather than being left bare: the same
    normalisation fetch_session_five_minute_candles does (engine.py:154), and a
    stored naive timestamp is one nobody can safely interpret later.
    """
    out = []
    for r in raw:
        d = r["date"]
        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)
        out.append({"date": d.isoformat(), "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"], "volume": r["volume"],
                    "oi": r.get("oi")})
    return out


def fetch_minute_candles(kite, tradingsymbol: str, instrument_token: int,
                         day: date, allow_api: bool = True) -> list:
    """One trading day of 1-minute candles with OI. Archive first, API on miss.

    Returns [] for a day with no session (holiday, or before the contract listed).
    """
    path = _day_path(tradingsymbol, day)
    if os.path.exists(path):
        return _read(path)
    if os.path.exists(_day_path(tradingsymbol, day, EMPTY_MARKER)):
        return []
    if not allow_api:
        raise FileNotFoundError(f"{tradingsymbol} {day} not archived and allow_api=False")

    frm = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=9)
    to = datetime.combine(day, datetime.min.time(), tzinfo=IST).replace(hour=15, minute=45)
    rows = _normalise(kite.historical_data(instrument_token, frm, to, "minute", oi=True))

    if rows:
        _write(path, rows)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(_day_path(tradingsymbol, day, EMPTY_MARKER), "w").close()
    return rows


def aggregate_five_minute(candles: list) -> list:
    """1-minute archive rows -> FiveMinuteCandle, for seed warm-up.

    Imported here rather than at module scope so `replay.data` stays importable
    without pulling in the engine — the archive is also useful on its own.
    """
    from engine import FiveMinuteCandle, five_minute_start

    buckets = {}
    for c in candles:
        t = c["date"] if isinstance(c["date"], datetime) else datetime.fromisoformat(c["date"])
        buckets.setdefault(five_minute_start(t), []).append(c)

    out = []
    for start in sorted(buckets):
        rows = buckets[start]
        ois = [r["oi"] for r in rows if r["oi"] is not None]
        out.append(FiveMinuteCandle(
            start=start, open=rows[0]["open"],
            high=max(r["high"] for r in rows), low=min(r["low"] for r in rows),
            close=rows[-1]["close"], volume=sum(r["volume"] for r in rows),
            oi_open=ois[0] if ois else None, oi_close=ois[-1] if ois else None,
        ))
    return out


# ---------------------------------------------------------------- front month

_SYMBOL_RE = re.compile(r"^BANKNIFTY(\d{2})([A-Z]{3})FUT$")
_MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}

# NSE index F&O expire on the LAST TUESDAY of the expiry month. Verified against
# this archive rather than assumed: BANKNIFTY26AUGFUT settles 25 Aug 2026, which is
# the last Tuesday (the last Thursday is the 27th), so the rule is Tuesday. That
# puts July's expiry at Tue 28 Jul — and the archive's OI agrees precisely, ramping
# through 28 Jul (x1.29 to 2.18M) and then sitting flat near 2.1M from 29 Jul on.
_EXPIRY_WEEKDAY = calendar.TUESDAY


def monthly_expiry(year: int, month: int) -> date:
    """Last _EXPIRY_WEEKDAY of the month.

    Does NOT account for the expiry falling on a trading holiday, when the exchange
    moves it to the previous trading day. The error is one day and in the SAFE
    direction: a holiday-shifted expiry means the real front-month window opened
    earlier than computed, so the guard rejects one legitimate session rather than
    admitting a far-month one.
    """
    days = calendar.monthcalendar(year, month)
    return date(year, month, max(w[_EXPIRY_WEEKDAY] for w in days if w[_EXPIRY_WEEKDAY]))


def contract_expiry(tradingsymbol: str) -> date:
    m = _SYMBOL_RE.match(tradingsymbol)
    if not m:
        raise ValueError(f"cannot parse a contract month from {tradingsymbol!r}")
    return monthly_expiry(2000 + int(m.group(1)), _MONTHS[m.group(2)])


def front_month_window(tradingsymbol: str) -> Tuple[date, date]:
    """The dates on which this contract was the FRONT month, inclusive.

    Opens the day after the previous month's expiry and closes on its own. Derived
    from the symbol alone, so the check stays hermetic — no token, no API, and it
    keeps working after the contract settles and vanishes from instruments("NFO").
    """
    expiry = contract_expiry(tradingsymbol)
    prev_year, prev_month = (expiry.year, expiry.month - 1) if expiry.month > 1 \
        else (expiry.year - 1, 12)
    return monthly_expiry(prev_year, prev_month) + timedelta(days=1), expiry


def assert_front_month(tradingsymbol: str, *days: date) -> None:
    """Refuse to replay a day on which this contract was the FAR month.

    Far-month candles fetch fine and error on nothing, and they reproduce a session
    the engine never saw: the volume gate compares against a far-month baseline, the
    profile is built from thin trade, and OI moves for rollover rather than
    conviction. Silent nonsense is the whole failure mode, so this raises.

    Note the far month is not simply "low volume" — over 22-30 Jul 2026 the August
    contract already traded 239k-876k while still being the far month, so a
    volume threshold would have waved those sessions through. The expiry boundary is
    the only clean signal.
    """
    start, end = front_month_window(tradingsymbol)
    bad = sorted(d for d in days if not (start <= d <= end))
    if bad:
        raise ValueError(
            f"{tradingsymbol} was the front month only {start}..{end}; "
            f"refusing {', '.join(str(d) for d in bad)}. Replaying outside that "
            f"window reproduces a session the engine never saw.")


def previous_trading_day(tradingsymbol: str, day: date) -> Optional[date]:
    """The most recent archived session before `day`, or None."""
    earlier = [d for d in archived_days(tradingsymbol) if d < day]
    return earlier[-1] if earlier else None


def archived_days(tradingsymbol: str) -> list:
    """Dates with real candles on disk, sorted."""
    d = os.path.join(ARCHIVE_DIR, tradingsymbol)
    if not os.path.isdir(d):
        return []
    return sorted(date.fromisoformat(n[:-len(".json.gz")])
                  for n in os.listdir(d) if n.endswith(".json.gz"))


def gap_check(tradingsymbol: str, start: date, end: date) -> list:
    """Weekdays in range with neither candles nor an empty-marker.

    Reported rather than raised: a silent failure has to surface the next day, not
    at expiry when nothing can be re-fetched.
    """
    missing, day = [], start
    while day <= end:
        if day.weekday() < 5:
            if not os.path.exists(_day_path(tradingsymbol, day)) and \
               not os.path.exists(_day_path(tradingsymbol, day, EMPTY_MARKER)):
                missing.append(day)
        day += timedelta(days=1)
    return missing


def backfill(kite, tradingsymbol: str, instrument_token: int,
             start: date, end: date, verbose: bool = True) -> dict:
    stats = {"fetched": 0, "cached": 0, "empty": 0, "failed": []}
    day = start
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        cached = os.path.exists(_day_path(tradingsymbol, day))
        try:
            rows = fetch_minute_candles(kite, tradingsymbol, instrument_token, day)
        except Exception as e:                      # one bad day must not end the run
            stats["failed"].append((day, str(e)))
            if verbose:
                print(f"  {day}  FAILED  {e}")
            day += timedelta(days=1)
            continue
        if not rows:
            stats["empty"] += 1
        elif cached:
            stats["cached"] += 1
        else:
            stats["fetched"] += 1
            if verbose:
                first, last = rows[0]["date"][11:16], rows[-1]["date"][11:16]
                oi_ok = sum(1 for r in rows if r["oi"])
                print(f"  {day}  {len(rows):4} candles  {first}-{last}  oi {oi_ok}/{len(rows)}")
        day += timedelta(days=1)
    return stats


def _resolve(day_hint: Optional[date] = None):
    from kite_auth import try_cached_session
    from instruments import get_current_month_contract
    kite = try_cached_session(config.KITE_API_KEY)
    if kite is None:
        print("No usable Kite token. Run token_helper.py first.")
        sys.exit(1)
    contract = get_current_month_contract(kite)
    if contract is None:
        print("Could not resolve a current-month contract.")
        sys.exit(1)
    return kite, contract


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Archive 1-minute futures candles with OI.")
    p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    p.add_argument("--check-only", action="store_true", help="report gaps, fetch nothing")
    a = p.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)

    kite, contract = _resolve()
    print(f"{contract.tradingsymbol}  token {contract.instrument_token}  expires {contract.expiry}")
    print(f"archive: {ARCHIVE_DIR}")
    print(f"range:   {start} .. {end}\n")

    if not a.check_only:
        stats = backfill(kite, contract.tradingsymbol, contract.instrument_token, start, end)
        print(f"\nfetched {stats['fetched']}  already archived {stats['cached']}  "
              f"no session {stats['empty']}  failed {len(stats['failed'])}")
        for day, err in stats["failed"]:
            print(f"  FAILED {day}: {err}")

    days = archived_days(contract.tradingsymbol)
    missing = gap_check(contract.tradingsymbol, start, end)
    print(f"\n{len(days)} sessions archived"
          + (f", {days[0]} .. {days[-1]}" if days else ""))
    if missing:
        print(f"GAPS ({len(missing)}): {', '.join(str(d) for d in missing)}")
        return 1
    print("no gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
