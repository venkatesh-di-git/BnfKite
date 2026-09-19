# BN Smart Assistant — Volume Profile & Decision Support

Streams Bank Nifty futures ticks from Kite, builds a live 5-minute
session Volume Profile (POC / VAH / VAL), and runs that alongside
VWAP / EMA-10 / OI through a Signal Engine and Decision Engine to
surface a Long/Short/Neutral bias with a confidence grade — per the
architecture in `BN_SmartContext_V1.md`.

This is a decision-support tool, not an auto-trading bot: it never
places orders, it only surfaces signals and a grade for a human to
act on.

Two ways to run it:
- **`python app.py`** — live web dashboard (NiceGUI) at http://localhost:8080
- **`python main.py`** — headless CLI, same underlying engine, prints to console + writes files

## Why our own calculator instead of the `marketprofile` PyPI package

Checked it before building this — not recommended here:
- Last released Jan 2020, "Alpha" status, no meaningful maintenance since.
- It groups volume by each candle's exact **Close price**, not spread
  across the candle's high-low range. Fine for daily bars; on 1-minute
  Bank Nifty candles it collapses a lot of price action into single
  points and gives a choppier, less accurate profile than the bin-based
  approach here.
- Pulls in numpy/pandas/scipy for one function we've already built in
  ~80 lines and unit-tested (see `test_volume_profile.py`).

## Dashboard (`app.py`)

- Live cards for LTP / POC / VAH / VAL.
- Horizontal volume profile chart (Plotly) with POC/VAH/VAL reference
  lines and current price marked.
- Decision card: ENTRY/WAIT status, Long/Short/Neutral direction, and
  A+/A/B/Ignore grade, plus a chip per signal category (hover for detail).
- Current Alert card + Alert History table (see Alert Engine below).
- Adjustable bin size, value area %, and poll interval — right in the UI.
- Settings panel has a "Test Telegram Alert" button — sends a plain
  test message so you can verify `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
  without waiting for a real ENTRY decision; doesn't touch Alert History.
- History table of the last 20 computed profiles.
- Kite login happens in-browser: paste the `request_token` into a text
  field instead of a terminal prompt.
- Built with NiceGUI rather than Streamlit specifically because this
  needs a background loop continuously pushing fresh data to the
  screen — NiceGUI's timers do that natively; Streamlit's script-rerun
  model would need extra plumbing (autorefresh components, manual
  session-state juggling) to behave the same way.

## Signal Engine & Decision Engine

Implemented per `Signal_Decision_Engine_Specification_V1.md`. Both are
pure, stateless, class-based, and never touch Kite or the UI — they
only consume the current forming candle + an indicator snapshot that
`engine.py` already computes.

`signal_engine.py`'s `SignalEngine.evaluate_all(candle, indicators)`
returns a `SignalSnapshot` with one state per category (every category
also has an `UNKNOWN` state for when there isn't enough data yet —
e.g. before 20 bars exist for the SMA20 volume average):

- **Trend** — Bullish/Bearish requires **Close** on the correct side of VWAP
  *and* the EMA slope to agree; otherwise Neutral. EMA10's own position
  relative to VWAP is deliberately **not** required: EMA10 is a smoothed,
  lagging value, so on a genuine reclaim price crosses VWAP first and EMA10
  follows minutes later. Demanding both turned the gate into a direction lock —
  on the 05 Aug tape Close held above VWAP on 41 rows but Close *and* EMA10 on
  only 1, while the mirror case (Close below, EMA10 above) occurred 0 times, so
  the lag runs one way only. The slope is measured
  across `EMA_SLOPE_LOOKBACK_SECONDS` of live EMA samples, not against the
  last closed bar: within a bar the closed EMA is constant, so that form
  collapsed to a *distance* from it and flipped sign on every tick across
  it. The slope then runs through **Schmitt-trigger hysteresis**, exactly like
  a PLC analog alarm that trips at 80 and clears at 78: enter Bullish when
  slope > +`EMA_SLOPE_HYSTERESIS_POINTS`, but *hold* Bullish until slope drops
  below −that. Once you have crossed, the threshold moves away from you, so a
  slope resting on zero cannot chatter. `VOLUME_HYSTERESIS` does the same for
  the High/Low band edges.
  `EMA_SLOPE_HYSTERESIS_POINTS` is **not a universal constant** — it ships at
  p75 of |slope| from one midday session, and p75 varied 1.6x across hours
  within that same session. A threshold in points scales with both volatility
  and price level, so expect to retune it per regime from the logged
  `ema_slope` column.
- **Pullback** — the current candle's Low/High crossing EMA10 while
  Close holds the other side (Bullish/Bearish/None).
- **Rejection** — a wick + candle direction + Close-vs-EMA10 combo
  (Bullish/Bearish/None) — new in this rewrite, wasn't in V1 originally.
- **Volume** — the forming bar is **projected to a full-bar equivalent**
  before comparison, so a bar is judged on its participation rate rather
  than on how far into it we are: `volume x (BAR_SECONDS / elapsed)` vs.
  SMA20, giving High (≥1.30x), Normal (0.80–1.30x), Low (<0.80x).
  Comparing raw partial volume against a full-bar average made the
  mandatory Volume gate near-unreachable for the first ~3 minutes of every
  bar. Two guards report `Unknown` instead of a fake spike:
  `VOLUME_PROJECTION_FLOOR_PCT` (enough traded to extrapolate from) and
  `VOLUME_PROJECTION_MIN_ELAPSED_SECONDS` (enough time). Both are needed —
  the floor bounds the numerator, not the multiplier.
  The floor applies only to **High and Normal**, the two states that open the
  Decision Engine's volume gate. Low is exempt: it blocks entry exactly as
  Unknown does, and a bar too thin to clear the floor is its own evidence of
  thin volume. Applying it to Low as well meant a genuinely quiet bar still
  read "too early to project" at 285s of 300 — on the 05 Aug session that hid
  the Volume state on 31 of 54 evaluations without changing a single decision.
- **Open Interest** — Rising/Falling/Flat from current vs. previous OI
  alone (no price coupling) — `OI_FLAT_THRESHOLD_PCT` sets the Flat band.
- **POC / VAH / VAL** — each Above/Below plus a third state (`At` for
  POC, `Rejected` for VAH/VAL) within `LEVEL_PROXIMITY_POINTS`.

### Continuous vs. session-anchored indicators

Two kinds of indicator, and they must be warmed differently:

| Indicator | Kind | Warmed from |
|---|---|---|
| EMA10 | **Continuous** | prior sessions + today |
| SMA20 volume | **Continuous** | prior sessions + today |
| VWAP | **Session-anchored** | today only |
| Volume Profile (POC/VAH/VAL) | **Session-anchored** | today only |
| OI pattern | **Session-anchored** | today only |

Without prior-session warm-up, EMA10 at 09:20 *equals* the first bar's close
and the SMA20 volume average is a one-bar "average" — so relative volume is
~1.00 by construction until the 20th bar completes at **10:55**. `SEED_BARS`
prior-session bars (searched back over `SEED_LOOKBACK_DAYS` so weekends and
holidays need no special handling) fix both.

Those seed bars are held in a **separate list** from today's completed bars,
which is what keeps them out of the session-anchored three. Feeding them to
VWAP or the profile would produce multi-day values; feeding them to the OI
pattern would compare yesterday's last bar against today's first and read the
**overnight** OI change as fresh conviction on bar one.

**Known issue — contract rollover.** The volume baseline is warmed from the
*same instrument token's* prior sessions. On the first session of a new
monthly contract, those are sessions where it was the far month and thinly
traded, so the baseline is low and relative volume reads high — Volume can sit
at High (and grades at A+) on ordinary activity all morning. There is no
automatic guard; `instruments.py` prints a warning near expiry.

`decision_engine.py`'s `DecisionEngine.evaluate(snapshot)` returns a
`Decision(direction, status, grade)`:

- **Status** — `ENTRY` only if Trend, Pullback, Rejection, Volume, OI,
  and the relevant level (VAH for Long / VAL for Short "not Rejected")
  *all* pass for one direction; otherwise `WAIT`. An `UNKNOWN` state
  never satisfies a condition, so missing data safely falls through to
  WAIT instead of crashing or guessing.
- **Grade** — only meaningful on ENTRY: `A+` if Volume=High and
  OI=Rising (both ideal), `A` if exactly one of {Volume=Normal,
  OI=Flat}, `B` if both are at the weaker-but-passing reading,
  `Ignore` on WAIT.

The spec leaves a few thresholds undefined (OI's Flat band, wick
significance, exact grade cutoffs) — the values above are documented
assumptions, tunable in `config.py`, not hard requirements from the
spec itself. See `test_signal_engine.py` / `test_decision_engine.py`
for the exact behavior of every rule.

## Alert Engine

Implemented per `Alert_Dashboard_Spec_V1-1.md`. `alert_engine.py`'s
`AlertEngine` consumes a `Decision` and distributes it — it never
calculates indicators/signals/decisions or modifies what the Decision
Engine produced.

- **Duplicate rule**: only alerts when `(status, direction, grade)`
  changes from the last decision — `WAIT → A+` alerts, repeated `A+`
  doesn't, `A+ → WAIT → A+` alerts again (the spec's own example).
  Only `A+`/`A`/`B` grades ever alert; `WAIT`/`Ignore` never do.
- **AlertRecord**: id, timestamp, type (Trading/Error), direction,
  grade, confidence, current_price, reason_list. `confidence` is a
  display-only mapping the Decision Engine doesn't produce itself —
  `A+`=95, `A`=80, `B`=65 (`config.GRADE_CONFIDENCE`).
- **Storage**: in-memory history + `csv/alert_log.csv` (same pattern as
  `csv/volume_profile_log.csv`), read back via `read_recent_alerts()`.
- **Telegram**: a plain `requests` POST to the Bot API — no-ops with a
  logged warning if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't set,
  so nobody without a bot configured is blocked. See Setup below to
  configure it, and the dashboard's "Test Telegram Alert" button to
  verify it without waiting for a real decision.
- **Error Alerts**: currently just feed disconnection (from
  `kite_ticker.py`'s staleness tracking) — not every possible failure
  mode from the spec's list, kept intentionally minimal for now.

See `test_alert_engine.py` for the exact dedupe behavior.

## What this does NOT do (yet)

- No sound alerts — Telegram + dashboard alerts are built; audio isn't.
- No HVN/LVN — deferred to Version 2 (see Roadmap below); the Volume
  Profile and Signal Engine currently work off POC/VAH/VAL only.
- No automatic contract rollover 3 days before expiry — it always
  tracks the nearest-expiry contract and just prints/shows a warning
  when you're within 3 trading days of expiry, per your rollover rules.
  You'd compare current vs next-month manually at that point, same as
  you do with the live Kite MCP framework.
- No feed back into the Pine script automatically — Pine can't read
  local files or hit a local dashboard. For now, glance at the
  dashboard (or `latest_volume_profile.json`) and type the numbers
  into the script's manual POC/VAH/VAL inputs.

## Roadmap (Version 2, per `BN_SmartContext_V1.md`)

Explicitly out of scope for now — don't build until V1 is validated:

- **HVN / LVN** — high/low volume nodes within the profile, alongside
  the existing POC/VAH/VAL.
- **Sound alerts** — Telegram + dashboard alert delivery are built (see
  Alert Engine above); audio isn't.
- Fuller Error Alert coverage (historical-data-unavailable, unexpected
  application errors — today it's just feed disconnection).
- Market Structure, Backtesting, Strategy Optimization, ATR, ADX,
  Option Greeks, Option Chain Analysis, Multi-Timeframe Analysis, Auto
  Trading, AI Trade Journal.

## Setup

1. **Create a Kite Connect app** at https://developers.kite.trade
   (this is separate from Claude's Kite MCP connector — this script
   runs standalone and needs its own API key/secret).

2. **Open this folder in VS Code**, then create a virtual environment
   so dependencies stay isolated (VS Code will usually offer to do
   this for you when it detects `requirements.txt`):
   ```
   python -m venv .venv
   ```
   Select `.venv` as the interpreter (Ctrl+Shift+P → "Python: Select
   Interpreter") — `.vscode/settings.json` already points at it.

3. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

4. **Set your credentials.** Never hardcode these in `config.py` —
   if this folder is ever zipped up, backed up, or pushed to git, a
   hardcoded secret goes with it and can't be un-shared.

   **Recommended: `.env` file** (works identically whether you run
   from a terminal or press F5 in VS Code):
   ```
   cp .env.example .env
   ```
   Then edit `.env` and fill in your real `KITE_API_KEY` and
   `KITE_API_SECRET`. `config.py` loads this automatically via
   `python-dotenv`. `.env` is already in `.gitignore` — it will never
   get committed by accident.

   **Optional — Telegram alerts:** also add `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` to `.env` to get alerts delivered to Telegram.
   Create a bot via [@BotFather](https://t.me/BotFather) for the token;
   message your new bot once, then get your chat ID from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (or ask
   [@userinfobot](https://t.me/userinfobot)). Without these set, the
   Alert Engine still works — it just skips Telegram delivery (logs a
   warning) and everything still shows up on the dashboard/CLI. Use the
   dashboard's "Test Telegram Alert" button (in Settings) to verify
   delivery once configured.

   **Alternative: real environment variables** (terminal-only — note
   this may not carry over into VS Code's F5 debugger depending on
   which terminal session it spawns the process in, which is why
   `.env` is the more reliable option here):
   ```
   export KITE_API_KEY="your_api_key"
   export KITE_API_SECRET="your_api_secret"
   ```
   On Windows (PowerShell):
   ```
   $env:KITE_API_KEY="your_api_key"
   $env:KITE_API_SECRET="your_api_secret"
   ```

5. **Run it:**
   ```
   python app.py        # dashboard at http://localhost:8080
   ```
   or
   ```
   python main.py        # headless CLI
   ```

## Daily login

Kite access tokens expire ~6 AM IST every day by exchange rule.

- **Dashboard:** on first load each day, you'll see a login card with
  a link and a text field. Click the link, log in with your usual
  2FA/TOTP, then paste the `request_token` from the redirect URL back
  into the field.
- **CLI:** same flow, but in the terminal — it prints the URL and
  prompts you to paste the token.

This is the standard, documented Kite Connect flow (login_url ->
request_token -> generate_session) — no automation of the login step
itself. The resulting token is cached in `.kite_session_cache.json`
for the rest of that trading day (permissions locked to your user via
`chmod 600`).

## Tuning

Defaults live in `config.py`; the dashboard also lets you change bin
size and value area % live without restarting:
- `BIN_SIZE` — price bin width in points (default 5, matching your
  existing approximation method).
- `VALUE_AREA_PCT` — value area volume target (default 0.70).
- `SNAPSHOT_INTERVAL_SECONDS` — how often the UI/console reads the
  in-memory session state (default 2; cheap, no network call).

Signal Engine thresholds (no live UI control yet — edit `config.py`
and restart):
- `LEVEL_PROXIMITY_POINTS` — how close price must be to POC/VAH/VAL to
  count as "At"/"Rejected" that level (default 10 points).
- `VOLUME_LOOKBACK_BARS` — SMA period for relative volume (default 20,
  per spec).
- `VOLUME_HIGH_THRESHOLD` / `VOLUME_LOW_THRESHOLD` — relative-volume
  cutoffs for High/Normal/Low (default 1.30 / 0.80, per spec).
- `OI_FLAT_THRESHOLD_PCT` — % OI change below which it's classified
  Flat rather than Rising/Falling (default 0.05% — spec doesn't
  specify a number, this is a documented assumption).

Alert Engine:
- `GRADE_CONFIDENCE` — display-only grade -> confidence % mapping
  (default A+=95, A=80, B=65).
- `ALERT_LOG_FILE` — where Alert History persists (default `csv/alert_log.csv`).
- `SIGNAL_LOG_FILE` — one row per change in any signal/decision state
  (default `csv/signal_log.csv`). Alerts only record transitions that cleared the
  mandatory gate, so they can't show *which* signal flipped or how often one
  flipped without moving the grade. This is the log to read when judging
  whether an indicator change actually helped.

  Each row also carries the **measured inputs the rules compared** —
  `open/high/low`, `ema_10`, `vwap`, `ema_slope`, alongside the existing
  `current_price`, `bar_volume`, `sma20_volume`, `elapsed_seconds`. These are
  in `MEASURED_FIELDS`, never in the change key: they move every tick, so
  including one would write a row per evaluation and destroy the compression.

  They are logged because they cannot be recovered afterwards. Rows are written
  only on change (median gap 13s, max 40min on the 05 Aug tape), so a bar's
  running `high`/`low` is invisible between rows, and a 60s-lookback `ema_slope`
  has no earlier sample to difference against on 9% of rows. `relative_volume`
  is deliberately *absent* — it is derivable from `bar_volume`, `sma20_volume`
  and `elapsed_seconds`, which `test_signal_log.py` verifies.

- **`engine_version`** — an 8-char hash of the rule modules' source plus the
  tuning constants, stamped on every row, with one line per process start in
  `csv/run_manifest.csv` recording the actual values. On 05 Aug the log silently
  mixed two engine versions (44 rows written before a volume-rule fix, the rest
  after) with nothing to tell them apart; a replay across that file draws wrong
  conclusions. If the header no longer matches `SignalStateLog.FIELDS` the old
  log is rotated to `signal_log.<timestamp>.csv` rather than appended to, which
  would otherwise produce a silently ragged CSV.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — see Setup above.

## Validating the approximation

Per your "multi-session validation" note — run this alongside your
actual chart for a few sessions and compare POC/VAH/VAL against what
you see visually. If it's consistently off, the most likely culprits
are `BIN_SIZE` (try 10 if Bank Nifty's range feels too granular at 5)
or the even-volume-per-bin-touched assumption in `build_bins()` — that
method is documented in `volume_profile.py` so it's easy to swap for
a different distribution rule if the approximation doesn't hold up.

## Files

- `volume_profile.py` — the actual POC/VAH/VAL math (unit tested).
- `test_volume_profile.py` — sanity tests with synthetic data. Run
  with `python test_volume_profile.py` any time you tweak the algorithm.
- `signal_engine.py` — candle + indicators -> `SignalSnapshot` (see
  `Signal_Decision_Engine_Specification_V1.md`).
- `test_signal_engine.py` — one test group per signal category.
- `decision_engine.py` — `SignalSnapshot` -> `Decision` (direction/status/grade).
- `test_decision_engine.py` — Long/Short/WAIT decision tests.
- `alert_engine.py` — `Decision` -> `AlertRecord` (dedupe, CSV history, Telegram).
- `test_alert_engine.py` — dedupe sequence + confidence mapping tests.
- `kite_auth.py` — login flow (both CLI-blocking and GUI-friendly
  variants) + daily token caching.
- `kite_ticker.py` — websocket wrapper (KiteTicker) that turns raw
  ticks into the (price, volume, oi, timestamp) shape engine.py wants.
- `instruments.py` — resolves the current-month Bank Nifty futures
  instrument token.
- `config.py` — all settings.
- `engine.py` — shared fetch/compute/persist logic used by both entry points,
  including `LiveFiveMinuteSession` and `SignalStateLog`.
- `test_live_session.py` — session tests: bar-bucket handling, snapshot
  immutability, EMA slope, prior-session seeding, elapsed derivation.
- `test_signal_log.py` — change-triggered signal logging.
- `main.py` — headless CLI runner.
- `app.py` — NiceGUI live dashboard.

