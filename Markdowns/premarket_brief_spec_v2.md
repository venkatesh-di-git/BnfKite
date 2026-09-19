# Premarket AI Brief — Spec (elaborate)

Runs once before open. Fetches everything obtainable, sends a full briefing to
Telegram, and stores a compact 1–2 line summary for the alert overlay.

**New file:** `premarket.py`
**New units:** `deploy/kite-premarket.service`, `deploy/kite-premarket.timer`
**Touches:** `config.py` (`PREMARKET_STATE_FILE`), `session_runner.py` (load at
startup), `requirements.txt` (`openai` — no `yfinance`, see Global cues)

---

## STATUS — not built. This is the next major piece.

Nothing here exists yet. It is the last dependency for `telegram_inbound_plan.md`
§2 (`on_auth_success`), so it blocks that too.

**Buildable without a Kite login.** Verified 05 Sep while planning: archived
1-min candles carry `oi` alongside OHLCV, so prev close, PDH, PDL, POC/VAH/VAL
and prev-day futures OI all compute offline. A live read against the archive
returned 385 candles for 24 Aug and POC/VAH/VAL 57442.5 / 57530.0 / 57220.0.

Three tiers by what each needs:

| Tier | Items | Needs |
|---|---|---|
| Archive | prev close, PDH/PDL, POC/VAH/VAL, futures OI | nothing |
| Public internet | global cues (Yahoo `query2`), FII/DII (NSE) | network only |
| Kite session | India VIX, Nifty spot, bank heavyweights, option chain | a login |

**Use the archive as the test path, not necessarily the production one.** It
depends on `backfill` having run, whereas `historical_data` daily is
authoritative and always current. Write the fetchers against Kite as specified
below and put a seam in so they can be exercised offline.

**Reuse the Groq client.** `ai_overlay._build_client()` already carries the
empty-key guard that stopped the scanner failing to start on the VM. Promote it
to a public `build_client()` and call it from both rather than writing a second
copy — the settings differ (high effort, 1200 tokens, 120s), the client does
not.

---

## Reuse — do not rewrite

Four things already exist and must be called, not reimplemented:

| Need | Use |
|---|---|
| BN futures instrument token | `get_current_month_contract(kite)` — [instruments.py:53](../instruments.py#L53) |
| POC / VAH / VAL | `compute_profile(candles, config.BIN_SIZE, config.VALUE_AREA_PCT)` — [volume_profile.py:135](../volume_profile.py#L135) |
| Kite session | `try_cached_session(config.KITE_API_KEY)` — [kite_auth.py:91](../kite_auth.py#L91). Never `get_authenticated_kite`: it blocks on `input()` under systemd |
| "Today" in IST | the `_today_str()` pattern at [kite_auth.py:59](../kite_auth.py#L59) |

`volume_profile.Candle` is the input dataclass — Kite's `historical_data` dicts
must be mapped into it, not passed raw.

**Option chain needs a filter that does not exist yet.** `instruments.py` only
matches `segment == "NFO-FUT"` ([instruments.py:41](../instruments.py#L41)).
Options are `NFO-OPT`. Call `kite.instruments("NFO")` **once** and filter twice
— it is a ~100k-row download and the heaviest thing in this script.

---

## Data availability — read this first

Of the 14 checklist items, 8 are fetchable from Kite, 3 need sources you do
not currently have, and 3 are AI synthesis over the rest.

| # | Item | Source | Status |
|---|---|---|---|
| 1 | Global cues (US, Asia) | Yahoo chart API, `query2` host | ✅ |
| 1 | GIFT Nifty | NSE IX — not on Kite; use `^NSEI` proxy | partial |
| 2 | Nifty / BN prev close, overnight move | `historical_data` daily | ✅ |
| 3 | FII / DII flows | NSE `fiidiiTradeReact` endpoint | ✅ prev-day only |
| 4 | India VIX level + change | `quote("NSE:INDIA VIX")` | ✅ |
| 5 | Banking sector strength | `quote` on 5 heavyweights | ✅ |
| 6 | PDH / PDL / close, S/R | `historical_data` daily | ✅ |
| 7 | Volume Profile POC/VAH/VAL | prev-day 1-min + `volume_profile.py` | ✅ |
| 8 | PCR, max pain, OI concentration | option chain via `quote` | ✅ |
| 9 | Futures OI buildup / unwinding | `historical_data` with `oi=True` | ✅ |
| 10 | News / events (RBI, Fed, earnings) | — | **NOT AVAILABLE** |
| 11 | Market regime | AI synthesis | ✅ |
| 12 | Opening bias | AI synthesis | ✅ |
| 13 | Key risks | AI synthesis | ✅ |
| 14 | Session context (2–3 to watch) | AI synthesis | ✅ |

**Only news remains genuinely unavailable.** Global cues and FII/DII both have
sources now (below). News has no reliable free source, and prior testing showed
news-derived session bias did not separate outcomes — left as `n/a`
deliberately, not as an oversight.

**Never let the model source its own facts.** `gpt-oss-120b` has no internet.
Asked for a figure it wasn't given, it produces a plausible, correctly-formatted
invention — and then reasons over it in the REGIME and BIAS sections, so the
whole brief becomes confident fiction. Every number must be fetched by Python
and passed in. The prompt's "do not invent numbers" line is load-bearing.

Related: do not use any library that *estimates* FII/DII from price action.
At least one public implementation does exactly this and returns it as if
real.

Note on PCR: rejected for the *live alert overlay* (latency, contested
signal, unevaluable). Included here because premarket has no latency
constraint and this is explicitly context, not a gate.

---

## VM facts — checked 05 Sep 2026, results below

Three assumptions this spec used to make about the VM. All now measured on
`bnvm`, and two of the answers changed the design. Re-run if the VM is rebuilt
or its external IP changes.

| Check | Result |
|---|---|
| NSE `fiidiiTradeReact` | **200, live data** — usable |
| Yahoo `query1` | **429, persistent** (3/3 attempts) |
| Yahoo `query2` | **200, all four tickers** — use this |
| `timedatectl` | `Asia/Kolkata (IST, +0530)`, NTP synced |

Commands, if they need re-running:

```bash
ssh bnvm '
  curl -s -o /dev/null -w "prime: %{http_code}\n" -A "Mozilla/5.0" \
       -c /tmp/nse https://www.nseindia.com
  curl -s -w "\napi: %{http_code}\n" -A "Mozilla/5.0" \
       -b /tmp/nse -c /tmp/nse https://www.nseindia.com/api/fiidiiTradeReact | head -c 300
  curl -s -o /dev/null -w "yahoo q2: %{http_code}\n" -A "Mozilla/5.0" \
       "https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d"
  timedatectl
'
```

The NSE priming call returns **403, and that is expected** — NSE refuses the
homepage to this User-Agent but still sets the cookie the API call needs. Only
the second status code means anything. Read the 403 as a failure and you will
delete a source that works.

**Nothing became `NOT AVAILABLE`.** Both sources this spec was unsure about
work from this VM, so the availability table above stands as written — but
neither works the obvious way. See the fetch stage.

---

## When

`OnCalendar=Mon-Fri 08:45`. The VM is `Asia/Kolkata` (measured above), so this
is IST and matches
[deploy/kite-tokencheck.timer](../deploy/kite-tokencheck.timer).

If the VM is ever rebuilt on a UTC image this becomes `Mon-Fri 03:15 UTC`. The
symptom of forgetting is a brief arriving at 14:15 IST, which reads as a broken
timer rather than a timezone bug.

`Environment=TZ=Asia/Kolkata` in the unit does **not** help here. That variable
is read by the process at runtime; `OnCalendar` is evaluated by systemd against
the system clock before the process exists. The scanner unit sets it
([deploy/kite-scanner.service](../deploy/kite-scanner.service)) for
`datetime.now()` inside Python, which is a different problem.

Not `Persistent` — a missed run should not fire late into the session.

---

## Fetch stage — deterministic, no AI

```
1. India VIX ................ quote("NSE:INDIA VIX"), level + % change
2. BN futures ............... prev close, PDH, PDL, prev-day OI + change
3. Nifty spot ............... prev close, % change
4. Bank heavyweights ........ quote: HDFCBANK, ICICIBANK, SBIN,
                              AXISBANK, KOTAKBANK — prev close + % change
5. Volume Profile ........... prev-day 1-min candles -> POC/VAH/VAL via
                              existing volume_profile.py
6. Option chain ............. ~10 strikes either side of ATM, CE+PE:
                              PCR (total PE OI / CE OI)
                              max pain (strike with least total payout)
                              top 3 CE OI strikes, top 3 PE OI strikes
                              -> which expiry: see below
7. Global cues .............. Yahoo query2: ^GSPC, ^IXIC, ^N225, ^HSI
8. FII/DII (prev day) ....... NSE fiidiiTradeReact
```

**Global cues — plain `requests`, pinned to `query2`. Not `yfinance`.**

Measured on the VM: `query1.finance.yahoo.com` returns **429 on every attempt**,
`query2` returns 200 for all four tickers. `yfinance` selects the host itself,
so on this IP it can silently produce four `n/a`s — the per-ticker `try/except`
firing every day rather than on a rare outage. Pinning the host removes that,
and the endpoint needs no cookie, no crumb, and no new dependency.

```python
CHART_URL = ("https://query2.finance.yahoo.com/v8/finance/chart/"
             "{sym}?range=5d&interval=1d")
TICKERS = {"S&P 500": "^GSPC", "Nasdaq": "^IXIC",
           "Nikkei": "^N225", "Hang Seng": "^HSI"}


def fetch_global_cues():
    out = {}
    for name, sym in TICKERS.items():
        try:
            r = requests.get(CHART_URL.format(sym=sym), timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            result = r.json()["chart"]["result"][0]
            # Yahoo returns null closes for holidays and half-days; they would
            # pass a bare length check and then poison the percentage.
            closes = [c for c in result["indicators"]["quote"][0]["close"]
                      if c is not None]
            if len(closes) < 2:
                out[name] = "n/a"; continue
            prev, last = closes[-2], closes[-1]
            out[name] = f"{last:,.0f} ({(last - prev) / prev * 100:+.2f}%)"
        except Exception:
            out[name] = "n/a"
    return out
```

`range=5d` not `2d` — a long weekend or holiday leaves `2d` with fewer than two
usable rows. **Per-ticker try/except**, not one wrapper: Yahoo can serve `^GSPC`
and fail on `^HSI`, and three cues plus one `n/a` beats four `n/a`s.

Verified 05 Sep 2026 — `^GSPC` returned five daily closes and
`exchangeTimezoneName: America/New_York`; the other three returned 200.

Closes are in local exchange time, so the Nikkei figure may be from a session
that closed hours after the S&P's. Fine as context — the brief must not imply
they are simultaneous.

**FII/DII — NSE:** requires a browser-like session or it returns 403.

```python
s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0 ..."})
s.get("https://www.nseindia.com")                    # cookies; may 403 — fine
r = s.get("https://www.nseindia.com/api/fiidiiTradeReact")
```

**The priming request returns 403 and must not be checked.** NSE refuses the
homepage to this User-Agent while still setting the cookie the API call needs.
`raise_for_status()` on that first call throws away a source that works — only
the second response matters.

Confirmed working from the VM, 05 Sep 2026:

```json
[{"category":"DII","date":"04-Sep-2026","netValue":"8930.12", ...},
 {"category":"FII/FPI","date":"04-Sep-2026","netValue":"-3111.94", ...}]
```

Published after close, so this is always **previous day's** flows. Label it
as such in the brief so the model does not imply it is live.

Each block wrapped independently. Any failure yields `n/a` for that block
only — never aborts the run.

Option chain is the heaviest call: ~40 instruments. `kite.quote()` batches,
but split into two calls if it exceeds the instrument limit.

**Which expiry: the nearest one — unless today IS that expiry, then the next.**

BANKNIFTY has no weeklies. NSE discontinued them from 20 Nov 2024, so the chain
is monthly-only and "nearest weekly" has nothing to select. One conditional
covers it:

```python
expiries = sorted({i["expiry"] for i in bn_options})
expiry = next((e for e in expiries if e > today), expiries[-1])
```

`> today`, not `>= today`. On expiry day the front month's PCR and max pain are
not merely noisy — they are structurally meaningless: max pain converges on
spot by definition as time value goes to zero, and OI collapses through the
session as positions square off. A brief that reports them as context is
reporting an artefact of the calendar as if it were positioning.

Label the expiry in the brief's data line so the figure can be read correctly:
`PCR 0.92 (30-Sep)`.

`get_current_month_contract` ([instruments.py:53](../instruments.py#L53))
already sorts by expiry and warns inside 3 days — reuse that ordering rather
than writing a second one.

---

## AI call

Same client and model as the alert overlay.

| Setting | Value |
|---|---|
| Model | `openai/gpt-oss-120b` |
| `reasoning_effort` | `"high"` |
| `max_tokens` | 1200 |
| `temperature` | 0 |
| `timeout` | 120 |

High effort is justified here — unlike the alert overlay, this is genuine
multi-source synthesis, and nothing is waiting on it.

**Token budget:** ~1,500 input + ~1,200 output ≈ 2,700. Free tier is 8K TPM,
200K TPD, so a single daily call uses ~1.4% of the daily ceiling. No pressure.

```python
PREMARKET_PROMPT = """You are writing a premarket briefing for a Bank Nifty \
intraday futures scalper who trades 5-minute pullback setups.

Work only from the data provided. Some fields may read 'n/a' — say so plainly \
rather than speculating or filling gaps from memory.

Produce these sections, in this order:

REGIME — trending, range-bound, or high-volatility, and why.
BIAS — bullish, bearish or neutral, with the single strongest supporting fact.
LEVELS — the 3-4 levels that matter most today, from the data given.
POSITIONING — what the option OI and futures OI suggest about where the market \
expects to sit.
RISKS — what would invalidate the bias.
WATCH — 2-3 specific things to monitor after 09:15.

Hard rules:
- This is context, not a trade signal. A rule-based engine decides entries.
- Never give an entry, stop, or target.
- Never predict a closing level or a day's range.
- Premarket bias is a starting point only. Live price action after 09:15 \
overrides it entirely — say this if the bias is weak.
- Do not invent numbers. Every figure must come from the data above.
- Keep each section to 1-2 sentences. Total under 200 words."""
```

---

## Output

**Telegram** — the full brief, plus a data line:

```
📋 PREMARKET — Bank Nifty

REGIME: ...
BIAS: ...
LEVELS: ...
POSITIONING: ...
RISKS: ...
WATCH: ...

VIX 11.30 (+0.4%) | BN 57,728 | PDH 57,890 | PDL 57,455
POC 57,650 | VAH 57,895 | VAL 57,420 | PCR 0.92 | Max pain 57,700
```

**State file** — `premarket_state.json`, for the scanner to load at start.

**Path: project root, not `CSV_DIR`.** `config.CSV_DIR` swaps to `csv-dryrun/`
under `BN_DRY_RUN` ([config.py:167](../config.py#L167)), so writing it there
means a dry-run scanner reads a different file — or none. This is a state file,
not a CSV artefact; it belongs beside `LIVE_OUTPUT_FILE`
([config.py:176](../config.py#L176)):

```python
PREMARKET_STATE_FILE = os.path.join(os.path.dirname(__file__),
                                    "premarket_state.json")
```

```json
{
  "date": "2026-08-31",
  "premarket_summary": "<REGIME + BIAS lines only, ~30 words>",
  "india_vix": 11.30,
  "poc": 57650, "vah": 57895, "val": 57420,
  "pdh": 57890, "pdl": 57455
}
```

Only `premarket_summary` and `india_vix` are read by the alert overlay. The
levels are stored because they are free once fetched and may be useful later.

**`date` must be IST.** Written with the `_today_str()` pattern from
[kite_auth.py:59](../kite_auth.py#L59), not `date.today()`. That helper is
IST-explicit precisely because a naive call ties the day boundary to whatever
timezone the host happens to be set to — correct on the VM only by accident of
its clock. The same field is the once-per-day guard for the login trigger (see
`telegram_inbound_plan.md`), so a wrong day boundary there means a double-send
or a skipped brief.

**Loaded in `SessionRunner.__init__`, not `main.py`.** `app.py` builds a
`SessionRunner` too; loading it in the runner gives both entry points the
context without a second call site. Populates
`state["india_vix"]` / `state["premarket_summary"]`, which
`initial_state()` gains for this purpose. A `date` mismatch means stale —
ignore it, leave both `None`, and the overlay reads `n/a`.

---

## Failure

Any unhandled exception — send `📋 Premarket brief unavailable`, exit 0.
No retry.

The scanner must start normally whether or not this ran.

---

## Verification

1. Run manually outside market hours — Telegram brief arrives, JSON written
2. Break the option-chain fetch deliberately — brief still sends, that
   section reads `n/a`
3. Delete the JSON, start the scanner — starts cleanly, overlay shows `n/a`
4. Stale JSON from a prior date — ignored, not used
5. `BN_DRY_RUN=1` and live both read the same `premarket_state.json`
6. `engine_version()` unchanged — this touches no hashed module
   (`config.py` and `session_runner.py` are both outside the hashed set; the
   `_VERSIONED_CONSTANTS` list is what makes a `config.py` edit matter, and
   `PREMARKET_STATE_FILE` is not in it)

---

## Out of scope

- No trade calls, no entry/stop/target
- Never gates or influences an alert; the alert prompt already forbids
  premarket contradicting a grade
- No news — no reliable free source, and news-derived bias did not separate
  outcomes in prior testing. Flagged `n/a`, never guessed
- No library that estimates FII/DII from price action

This brief is context only, never a gate — which is the same verdict
`Session_Bias_Engine_V1.md` reached from measurement: as a filter the
higher-timeframe bias deleted all three of 12 Aug's winning alerts and doubled
the day's loss. Nothing here may become an input to `Decision`.

---

## Resolved

- **Option expiry** — nearest, or the next one if today is that expiry. Labelled
  in the data line. See the fetch stage.
- **`OnCalendar` timezone** — decided by `timedatectl` output, not assumed.
- **`GROQ_API_KEY` and Kite credentials** — `EnvironmentFile=` in
  `kite-premarket.service`, so they are in the environment before Python
  starts. Does not depend on `WorkingDirectory` or on `import config` running
  `load_dotenv()` first.
- **Brief runtime vs. the login trigger** — the caller restarts the scanner
  first and replies before the brief starts. Owned by
  `telegram_inbound_plan.md`.

## Open points

None. The egress questions were measured on 05 Sep 2026 — see VM facts. Both
sources work; both needed a non-obvious detail to get there (NSE's 403 prime,
Yahoo's throttled `query1`), and both are now written into the fetch stage.
