"""
config.py

Central settings. API credentials are read from environment variables
so they never end up hardcoded in a file you might share or commit —
set them once in your shell profile or a .env file you load yourself.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # no-op if .env is absent; real env vars still take effect

# --- Kite Connect app credentials (from developers.kite.trade) ---
KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET", "")

# --- Volume profile parameters (per your memory: 5-point bins, 70% VA) ---
BIN_SIZE = 5.0
VALUE_AREA_PCT = 0.70
EMA_PERIOD = 10

# The Trend rule's slope is measured across this window rather than against
# the last closed bar's EMA. Within a bar the closed EMA is constant, so the
# old form reduced to alpha * (price - closed_ema) — a DISTANCE from the last
# bar's EMA, which flips sign every time price ticks across it. Sampling the
# live EMA over a window makes it alpha * (price_now - price_then): an actual
# rate of change. 30 is a reasonable alternative to 60.
EMA_SLOPE_LOOKBACK_SECONDS = 60
# Schmitt-trigger hysteresis on the Trend slope, exactly like a PLC analog
# alarm that trips at 80 and clears at 78: enter Bullish when slope > +h, but
# HOLD Bullish until slope < -h. Once you have crossed, the threshold moves
# away from you, so a slope sitting on zero physically cannot chatter.
#
# This beats a symmetric deadband of the same size (measured: 2.9 alerts and
# 38% clustering vs 4.1 / 62%) because a deadband still chatters at its own
# two edges, whereas a Schmitt trigger has nowhere left to chatter.
#
# Interpretable: slope = alpha * price change over the lookback, so 4.3 points
# of slope is ~23 points of BANKNIFTY movement inside the lookback window.
#
# NOT a universal constant. 4.3 is p75 of |slope| on the 05 Aug session, taken
# from the 55-70s-lookback subset (the full sample averages a 94s lookback and
# overstates p75 by 15%). The previous 3.0 came from synthetic random walks.
# p75 varied 1.6x across hours WITHIN that same session, and a threshold in
# points scales with both volatility and price level — so this needs retuning
# per regime. Once ema_slope is logged directly it can be measured, not inferred.
EMA_SLOPE_HYSTERESIS_POINTS = 4.3

# --- Live session cadence ---
# With ticks driving the session live, there's no more REST polling —
# SNAPSHOT_INTERVAL_SECONDS just controls how often the UI/console
# reads the in-memory session state (cheap, no network).
SNAPSHOT_INTERVAL_SECONDS = 2

# How often to actually persist to latest_volume_profile.json /
# volume_profile_log.csv. Kept separate and coarser than the UI refresh
# so the CSV log doesn't fill up with a near-duplicate row every 2s.
LOG_WRITE_INTERVAL_SECONDS = 30

# How long without a tick during market hours before we treat the feed
# as stale and surface a warning instead of silently going quiet.
TICK_STALE_SECONDS = 30

# How long the feed may stay silent before the dashboard tears it down and
# rebuilds it. KiteTicker retries internally, but when it gives up nothing
# else recovers the connection — previously that meant restarting the app.
# Must be comfortably longer than TICK_STALE_SECONDS so the warning has a
# chance to clear on its own before we force a reconnect.
FEED_RECOVERY_SECONDS = 120

# How often to redraw the volume-profile chart. Deliberately much coarser
# than the 1s dashboard tick: a Plotly redraw is expensive in the browser,
# and at 1s it monopolises the main thread so clicks/hovers elsewhere on
# the page get dropped. The numeric cards still update every second.
CHART_REFRESH_SECONDS = 5

# --- Signal Engine tuning (per Signal_Decision_Engine_Specification_V1.md) ---
# SMA period for the Volume signal's "relative volume" (current bar
# volume / SMA20 volume).
VOLUME_LOOKBACK_BARS = 20
# Relative volume thresholds from the spec: >=1.30 High, 0.80-1.30
# Normal, <0.80 Low.
VOLUME_HIGH_THRESHOLD = 1.30
VOLUME_LOW_THRESHOLD = 0.80
# These thresholds apply to the PROJECTED ratio. Comparing a part-formed bar's
# raw volume against a full-bar average biased the reading by position within
# the bar, making the mandatory Volume gate near-unreachable for the first ~3
# minutes of every bar — a false-negative class invisible from the outside.
BAR_SECONDS = 300.0
# Projection needs enough traded volume to extrapolate from. Below this
# fraction of the average, Volume reports Unknown rather than a noisy multiple
# of a tiny number.
VOLUME_PROJECTION_FLOOR_PCT = 0.25
# ...and enough elapsed time. The volume floor bounds the NUMERATOR, not the
# multiplier: at exactly the floor, 1s elapsed still projects to 75x, 2s to
# 37x, 5s to 15x — all High. Do not set this to 0.0 and rely on the floor
# alone. In practice the floor binds first (~75s at normal pace), so this
# only bites during a genuine surge, which is precisely when it should.
VOLUME_PROJECTION_MIN_ELAPSED_SECONDS = 30.0
# Same Schmitt-trigger idea on the High/Low band edges: enter High at 1.30,
# hold High until the projected ratio falls below 1.30 - this. Stops a ratio
# resting on a threshold from toggling the state every tick.
VOLUME_HYSTERESIS = 0.05
# The spec doesn't give a numeric threshold for OI "Flat" vs
# Rising/Falling — using a % change (not absolute contracts) since OI
# is a large raw count that doesn't scale with a fixed point threshold.
OI_FLAT_THRESHOLD_PCT = 0.0005
# Price counts as "near" a volume-profile level (POC/VAH/VAL) when
# within this many points of it — POC "At", VAH/VAL "Rejected". Also the
# tolerance for the VWAP Pullback rule: on 06 Aug price turned 1.5 points
# ABOVE VWAP and rallied 100, so a strict straddle (low <= vwap) records
# nothing on exactly the setup the rule exists to catch.
LEVEL_PROXIMITY_POINTS = 10.0
# VWAP Rejection: how much of the bar's range the close must hold, measured
# from the far end (close-to-low for bullish). 0.5 = closed in the upper half.
#
# Deliberately NOT the wick test the EMA10 Rejection rule uses. That requires
# low < min(open, close), which a bar opening at its low can never satisfy —
# and a clean V-reversal produces exactly that shape (the 11:50 and 11:55 bars
# on 06 Aug both opened at their low, so bullish rejection was structurally
# impossible for the whole 100-point move). Loosening it to <= is worse: low is
# by definition the bar minimum, so the condition becomes a tautology. Close
# position in range measures "probed down and got rejected" independently of
# where the open happens to sit.
VWAP_REJECTION_CLOSE_PCT = 0.5

# --- Prior-session warm-up (see engine.LiveFiveMinuteSession) ---
# Without these, everything derives from today's bars alone: at 09:20 EMA10
# literally equals the first bar's close, and the SMA20 volume average is a
# one-bar "average" so relative volume is ~1.00 by construction. The 20th bar
# of the day doesn't complete until 10:55.
# 36 bars = 3 hours: clears SMA20's 20-bar need and decays the EMA seed to
# well under 1% weight, with slack for half-days and thin sessions.
SEED_BARS = 36
# Calendar days to search back. Weekends and NSE holidays make "the previous
# trading day" a calendar problem, so fetch a window and take the last
# SEED_BARS before today's open — holidays then need no special handling.
SEED_LOOKBACK_DAYS = 5

# --- Market hours (IST) ---
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# --- Dry run ---
# Read ONCE, here. Not per call: a mode that can change mid-session is a mode you
# cannot reason about afterwards when reading the logs. Default off — an unset
# variable must never silently disable live alerting, so the failure mode is loud
# (alerts fire) rather than quiet (alerts vanish).
#
# One flag, not two. A separate BN_NO_TELEGRAM and BN_CSV_DIR would give four
# states, two of which are wrong: Telegram live with CSVs redirected, and vice
# versa. See Markdowns/dry_run_section.md.
DRY_RUN = os.environ.get("BN_DRY_RUN", "").strip().lower() not in ("", "0", "false", "no")

# --- Output ---
# CSV logs live in csv/ so the project root stays code-only. Created on import
# rather than at first write, because three separate modules append to these
# paths and none of them should each have to worry about the directory.
#
# The makedirs MUST stay below the redirect: write_output() and SignalStateLog
# both open by path and neither creates its parent, so a dry run whose directory
# was never made dies at 09:15 for an entirely boring reason.
CSV_DIR = os.path.join(os.path.dirname(__file__), "csv-dryrun" if DRY_RUN else "csv")
os.makedirs(CSV_DIR, exist_ok=True)

# The live heartbeat, ALWAYS at the project root whatever BN_DRY_RUN says.
# healthcheck.py stats this one: its job is to assert that the *live* engine is
# producing, and a dry run on the VM is never legitimate, so dry-run output must
# never be able to satisfy it. Without this the probe would happily watch the
# file a stray dry run is refreshing — every check green, nothing delivered,
# which is precisely the 10 Aug failure shape.
LIVE_OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "latest_volume_profile.json")

# What the engine writes. Moves inside CSV_DIR under a dry run so the invariant
# holds: a dry run writes nothing outside csv-dryrun/, which also makes cleanup a
# single rm -rf.
OUTPUT_FILE = os.path.join(CSV_DIR, "latest_volume_profile.json") if DRY_RUN else LIVE_OUTPUT_FILE
LOG_FILE = os.path.join(CSV_DIR, "volume_profile_log.csv")
# One row per change in any signal/decision state. Alerts only record
# transitions that cleared the Decision Engine's gate, so they can't show
# which signal flipped, or how often one flipped without moving the grade —
# this is what makes an indicator change measurable in a single session.
SIGNAL_LOG_FILE = os.path.join(CSV_DIR, "signal_log.csv")

# --- Alert Engine (per Alert_Dashboard_Spec_V1-1.md) ---
# Optional — Telegram delivery no-ops (with a logged warning) if unset.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALERT_LOG_FILE = os.path.join(CSV_DIR, "alert_log.csv")
# Alerts the flip cooldown withheld. A SEPARATE file, not extra rows in
# alert_log.csv: that log is what alerting policy gets scored against, and mixing
# undelivered rows into it would silently change every count ever taken from it.
#
# The cooldown is shipping without evidence that suppressing helps — outcome
# measurement is parked in review_session.py — so this file IS the evidence
# collection. Every withheld alert is recorded with what blocked it and by how
# much, so the question can be answered later from data rather than re-argued.
SUPPRESSED_LOG_FILE = os.path.join(CSV_DIR, "suppressed_log.csv")
# The spec's Decision Engine only outputs a letter grade, not a numeric
# confidence — this display-only mapping is an Alert Engine assumption,
# not a spec requirement.
GRADE_CONFIDENCE = {"A+": 95, "A": 80, "B": 65, "Ignore": 0}
