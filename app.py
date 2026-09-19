"""
app.py — live dashboard for the BN Volume Profile service.

Run with:  python app.py
Then open: http://localhost:8080

Price/volume/OI stream live over Kite's websocket (KiteTicker, via the
LiveTickFeed wrapper); REST historical_data is used only once at
startup to seed bars from earlier in the session. Kite's blocking REST
calls (login, contract resolution) are pushed to a worker thread
(run.io_bound) so the UI never freezes.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from fastapi.responses import HTMLResponse
from nicegui import app, run, ui

import config
from kite_auth import try_cached_session, get_login_url, complete_login
from instruments import get_current_month_contract, FutureContract
from engine import is_market_hours, read_recent_log
# Direction/Grade/Status only — the engines themselves live in session_runner now.
from decision_engine import Direction, Grade, Status
from alert_engine import read_recent_alerts
from session_runner import SessionRunner

IST = ZoneInfo("Asia/Kolkata")

# Which machine is serving this page. Both dashboards are reached at
# localhost:8080 — the local one directly, the VM's through an SSH tunnel — so
# without a label two identical-looking tabs show entirely different data.
# Defaults to LOCAL so an unlabelled process is never mistaken for the
# authoritative one; the systemd unit sets BN_INSTANCE=VM.
INSTANCE = os.environ.get("BN_INSTANCE", "LOCAL")

# The engine lives in session_runner, which never imports NiceGUI — that is what
# stops engine work drifting back inside a browser-dependent ui.timer, the 10 Aug
# failure where the scanner ran 14 hours and produced nothing.
_runner = SessionRunner()

# ALIAS, not a copy. The ~120 `state[...]` readers below are unchanged by the
# extraction. SessionRunner only ever mutates self.state, never rebinds it — a
# rebind would silently detach the dashboard and updates would just stop.
state = _runner.state
_alert_engine = _runner.alert_engine
_last_seen_candle_count = 0

# ---------------------------------------------------------------
# Kite session bootstrap
# ---------------------------------------------------------------
def try_restore_session():
    if not config.KITE_API_KEY or not config.KITE_API_SECRET:
        state["error"] = "KITE_API_KEY / KITE_API_SECRET not set — see README."
        return
    kite = try_cached_session(config.KITE_API_KEY)
    if kite:
        state["kite"] = kite
        _resolve_contract()
    else:
        state["login_url"] = get_login_url(config.KITE_API_KEY)


def _resolve_contract(custom_symbol: str = None):
    """Blocking — must be called via run.io_bound. Never calls ui.* here."""
    try:
        if custom_symbol:
            symbol_to_find = custom_symbol.strip().upper()
            instruments = state["kite"].instruments("NFO")
            matched = None
            for inst in instruments:
                if inst.get("tradingsymbol") == symbol_to_find:
                    matched = FutureContract(
                        instrument_token=inst["instrument_token"],
                        tradingsymbol=inst["tradingsymbol"],
                        expiry=inst["expiry"],
                        lot_size=inst["lot_size"],
                    )
                    break
            if matched:
                state["contract"] = matched
                state["result"] = None
                state["current_price"] = None
                state["vwap"] = None
                state["ema_10"] = None
                state["oi"] = None
                state["oi_pattern"] = None
                state["candle_count"] = 0
                state["error"] = None
            else:
                state["error"] = f"Could not find contract with symbol '{symbol_to_find}' in NFO."
                return
        else:
            contract = get_current_month_contract(state["kite"])
            state["contract"] = contract
            state["error"] = None
        state["profile"] = state["kite"].profile()
    except Exception as e:
        state["error"] = f"Could not resolve futures contract: {e}"


def _blocking_fetch_ltp(kite, tradingsymbol: str):
    """Fetch just the last price for any NFO symbol. Non-fatal."""
    try:
        ltp_key = f"NFO:{tradingsymbol}"
        data = kite.ltp(ltp_key)
        return data.get(ltp_key, {}).get("last_price")
    except Exception:
        return None


async def _perform_login(request_token: str) -> bool:
    """Shared by the manual-paste flow and the auto-capture callback route."""
    try:
        # Kite's HTTP call blocks — run off the event loop so the UI stays responsive.
        kite = await run.io_bound(complete_login, config.KITE_API_KEY, config.KITE_API_SECRET, request_token)
        state["kite"] = kite
        state["login_url"] = None
        state["error"] = None
        await run.io_bound(_resolve_contract)
        await run.io_bound(_runner.ensure_started)
        return True
    except Exception as e:
        state["error"] = f"Login failed: {e}"
        return False


async def go_live_with_custom(symbol: str):
    if not symbol or not symbol.strip():
        ui.notify("Please enter a valid instrument/contract name.", type="warning")
        return
    _runner.stop()
    ui.notify(f"Resolving '{symbol.upper()}'...")
    await run.io_bound(_resolve_contract, symbol)
    if state["error"]:
        ui.notify(state["error"], type="negative")
        main_panel.refresh()
        return

    ui.notify(f"Switched to {state['contract'].tradingsymbol}", type="positive")

    # Always fetch LTP so the price card shows something, even outside market hours.
    ltp = await run.io_bound(_blocking_fetch_ltp, state["kite"], state["contract"].tradingsymbol)
    if ltp is not None:
        state["current_price"] = ltp

    # During market hours subscribe to the live WebSocket stream.
    now = datetime.now(IST)
    if is_market_hours(now):
        await run.io_bound(_runner.ensure_started)

    main_panel.refresh()


async def do_login(request_token: str):
    request_token = request_token.strip()
    if not request_token:
        ui.notify("Paste the request_token first.", type="warning")
        return
    ui.notify("Logging in…")
    if await _perform_login(request_token):
        ui.notify("Logged in to Kite.", type="positive")
    else:
        ui.notify(state["error"], type="negative")
    main_panel.refresh()


# ---------------------------------------------------------------
# Auto-capture request_token: Zerodha redirects here directly if this
# URL is registered as the app's Redirect URL (developers.kite.trade).
# Manual paste in the login card remains as a fallback.
# ---------------------------------------------------------------
@app.get("/kite/callback")
async def kite_callback(request_token: str = ""):
    request_token = request_token.strip()
    if not request_token:
        return HTMLResponse("<h3>Missing request_token in redirect URL.</h3>", status_code=400)
    ok = await _perform_login(request_token)
    main_panel.refresh()
    if ok:
        return HTMLResponse("<h3>Kite login successful — you can close this tab.</h3>")
    return HTMLResponse(f"<h3>Login failed: {state['error']}</h3>", status_code=400)


# ---------------------------------------------------------------
# Live tick stream (KiteTicker via the LiveTickFeed wrapper — handles
# reconnects/errors/staleness tracking that a bare KiteTicker doesn't).
# ---------------------------------------------------------------
def _refresh_all_panels():
    metrics_panel.refresh()
    status_header.refresh()
    signal_panel.refresh()
    current_alert_panel.refresh()
    alert_history_panel.refresh()
    history_panel.refresh()
    update_chart(force=True)


async def engine_poll():
    """Engine clock, slow lane. Runs on app.timer — CLIENT-INDEPENDENT.

    ui.timer would tie this to a browser: NiceGUI awaits client.connected() and
    cancels the timer on timeout, which on 10 Aug meant the engine never ran at
    all while the service looked healthy.

    io_bound is a THREAD pool (cpu_bound would be processes), which this needs:
    the runner mutates a shared dict and calls Kite's blocking REST client.
    """
    if state["kite"] is None:
        # try_restore_session() runs once at import and was never retried, so a
        # token written after startup — the normal case when the service boots
        # before token_helper.py — left state["kite"] None forever and the
        # dashboard parked on the login panel. Restart=on-failure cannot help:
        # nothing failed.
        await run.io_bound(try_restore_session)

    await run.io_bound(_runner.ensure_started)


async def engine_tick():
    """Engine clock, fast lane. Also app.timer, for the same reason.

    Everything it does — date guard, advance, write_output, staleness, feed
    rebuild, signals, alerts, logging — is inside SessionRunner, which cannot
    import NiceGUI. Nothing here touches the screen.
    """
    await run.io_bound(_runner.tick)


async def force_reconnect():
    """The Reconnect button. Unlike poll_once() this tears the existing feed
    down first — start_live_stream() short-circuits whenever state["feed"] is
    set, so without the teardown the button could never recover the one case
    you'd press it for: a feed that is present but has gone quiet.

    Note this rebuilds the LiveFiveMinuteSession too, re-seeding today's bars
    over REST. Accumulated tick state and the forming bar are discarded."""
    if not state["polling_enabled"]:
        ui.notify("Live feed is switched off — turn it on first.", type="warning")
        return
    ui.notify("Reconnecting to the live feed…")
    connected = await run.io_bound(_runner.rebuild_feed)
    if state["error"]:
        ui.notify(state["error"], type="negative")
    elif connected:
        ui.notify("Live feed reconnected.", type="positive")
    else:
        ui.notify("Not reconnected — outside market hours, or no contract resolved.", type="warning")
    _refresh_all_panels()


async def set_live_feed_enabled(enabled: bool):
    """The switch is a real on/off, not just a gate on reconnection: turning
    it off stops the websocket, which also stops bar building, CSV writes and
    alerts, since those all key off state["live_session"]."""
    state["polling_enabled"] = enabled
    if enabled:
        await run.io_bound(_runner.ensure_started)
        ui.notify("Live feed enabled." if state["feed"]
                  else "Enabled — waiting for market hours to connect.", type="positive")
    else:
        _runner.stop()
        ui.notify("Live feed stopped — no ticks, logging or alerts until re-enabled.", type="warning")
    _refresh_all_panels()


# Last-rendered fingerprint per panel. Re-rendering a panel replaces its
# DOM subtree, so panels whose contents haven't changed are left alone —
# only the clock in status_header() genuinely changes every second.
_panel_signatures = {}


def _refresh_if_changed(panel, key, signature):
    if _panel_signatures.get(key) != signature:
        _panel_signatures[key] = signature
        panel.refresh()


def _refresh_changed_panels():
    result = state["result"]
    _refresh_if_changed(metrics_panel, "metrics", (
        state["current_price"], state["vwap"], state["ema_10"],
        result.poc if result else None, result.vah if result else None, result.val if result else None,
        state.get("oi"), state.get("oi_pattern"), state["error"],
        state["contract"].tradingsymbol if state["contract"] else None,
    ))

    snapshot, decision = state.get("signal_snapshot"), state.get("decision")
    _refresh_if_changed(signal_panel, "signal", (
        (decision.status, decision.direction, decision.grade) if decision else None,
        (snapshot.trend, snapshot.pullback, snapshot.rejection, snapshot.volume,
         snapshot.open_interest, snapshot.poc, snapshot.vah, snapshot.val) if snapshot else None,
    ))

    alert = state.get("current_alert")
    _refresh_if_changed(current_alert_panel, "current_alert",
                        (alert.timestamp, alert.grade, alert.direction) if alert else None)
    _refresh_if_changed(alert_history_panel, "alert_history", len(_alert_engine.history))


def refresh_dashboard_ui():
    """Screen clock. Stays on ui.timer, which is correct for a browser — it only
    runs when someone is actually looking, and draws nothing otherwise.

    Deliberately holds no engine work. That separation is the whole point of the
    extraction: engine on app.timer, screen on ui.timer.
    """
    for message, kind in _runner.drain_notices():
        ui.notify(message, type=kind)

    # The engine no longer calls history_panel.refresh() on bar close — it has no
    # way to reach the UI. The screen watches the counter instead.
    global _last_seen_candle_count
    if state["candle_count"] != _last_seen_candle_count:
        _last_seen_candle_count = state["candle_count"]
        history_panel.refresh()

    status_header.refresh()
    _refresh_changed_panels()
    update_chart()


# ---------------------------------------------------------------
# Chart
# ---------------------------------------------------------------
def _place_level_labels(fig, levels):
    """Draw each level's dotted line at its exact price, but merge the
    annotation TEXT for any levels within LEVEL_PROXIMITY_POINTS of each
    other — the same threshold the Decision Engine already uses to call
    price "at" POC or "rejected" at VAH/VAL. Without this, two labels
    whose prices are only a few points apart (VWAP sitting right on top
    of EMA10 is the common case) render as stacked, unreadable text at
    the same pixel height. `levels` is [(label, price, color), ...] with
    None prices already filtered out.
    """
    if not levels:
        return
    ordered = sorted(levels, key=lambda lv: lv[1])
    groups = [[ordered[0]]]
    for item in ordered[1:]:
        if item[1] - groups[-1][-1][1] <= config.LEVEL_PROXIMITY_POINTS:
            groups[-1].append(item)
        else:
            groups.append([item])

    for group in groups:
        for label, price, color in group:
            fig.add_hline(y=price, line_dash="dot", line_color=color)

        if len(group) == 1:
            label, price, color = group[0]
            text = f"<span style='color:{color}'>{label} {price:.2f}</span>"
            y = price
        else:
            text = "<br>".join(f"<span style='color:{color}'>{label} {price:.2f}</span>"
                               for label, price, color in group)
            y = sum(p for _, p, _ in group) / len(group)

        fig.add_annotation(xref="paper", x=1.0, xanchor="left", xshift=6,
                           yref="y", y=y, text=text, showarrow=False, align="left")


def build_profile_figure():
    result = state["result"]
    if result is None:
        return go.Figure()

    prices = sorted(result.bins.keys())
    volumes = [result.bins[p] for p in prices]
    in_va = [p in result.value_area_bins for p in prices]
    colors = ["#2f6fed" if v else "#3a3f4b" for v in in_va]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=prices, x=volumes, orientation="h",
        marker_color=colors, showlegend=False,
        hovertemplate="Price %{y}<br>Volume %{x:.0f}<extra></extra>",
    ))

    levels = [
        ("VWAP", state["vwap"], "#8b5cf6"),
        ("EMA 10", state["ema_10"], "#38bdf8"),
        ("VAH", result.vah, "#e0a13a"),
        ("POC", result.poc, "#e04f4f"),
        ("VAL", result.val, "#e0a13a"),
    ]
    _place_level_labels(fig, [(l, p, c) for l, p, c in levels if p is not None])

    if state["current_price"]:
        fig.add_hline(y=state["current_price"], line_color="#4fe0a1", line_width=2,
                      annotation_text=f"LTP {state['current_price']:.2f}",
                      annotation_position="left")

    fig.update_layout(
        margin=dict(l=10, r=80, t=10, b=10),
        height=500,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title="Volume",
        yaxis_title="Price",
        # Constant across redraws, so Plotly treats each update as the same
        # chart and keeps whatever zoom/pan the user has applied instead of
        # resetting the axes on every tick.
        uirevision="volume-profile",
    )
    return fig


# The Plotly element is created once and then mutated in place. Rebuilding
# it (i.e. calling ui.plotly() again from a refreshable) re-mounts the whole
# widget in the browser, which is slow enough at 1s intervals to make the
# rest of the page unclickable.
_chart = {"container": None, "element": None, "drawn_at": None}


def chart_view():
    """Placeholder container, rendered once per main_panel build. The chart
    itself is filled in (and thereafter updated) by update_chart()."""
    _chart["element"] = None
    _chart["drawn_at"] = None
    _chart["container"] = ui.column().classes("w-full")
    update_chart(force=True)


def update_chart(force: bool = False):
    """Push the current profile into the existing Plotly element. Throttled
    to config.CHART_REFRESH_SECONDS unless forced (e.g. first draw, or a
    settings change the user expects to see immediately)."""
    container = _chart["container"]
    if container is None or state["result"] is None:
        return

    now = datetime.now(IST)
    drawn_at = _chart["drawn_at"]
    if not force and drawn_at is not None and \
            (now - drawn_at).total_seconds() < config.CHART_REFRESH_SECONDS:
        return

    figure = build_profile_figure()
    element = _chart["element"]
    if element is None:
        with container:
            _chart["element"] = ui.plotly(figure).classes("w-full")
    else:
        element.update_figure(figure)  # in-place Plotly.react, no re-mount
    _chart["drawn_at"] = now


# ---------------------------------------------------------------
# UI
#
# Split into independently-refreshable regions so that the live-tick
# refresh timer only touches the metrics/chart/history data — it never
# recreates the Settings panel or expansion panels, so mid-typed values
# and expanded/collapsed state are never disturbed.
#
# Within those regions, re-rendering is kept as narrow as possible: only
# status_header() (the clock) is rebuilt every second. The metric cards,
# signal chips and alert tables are rebuilt only when their contents
# actually change, and the chart is never rebuilt at all — it's updated
# in place on a slower cadence. Redrawing everything at 1Hz kept the
# browser's main thread busy enough that clicks elsewhere were dropped.
# ---------------------------------------------------------------
@ui.refreshable
def status_header():
    """Clock/feed-status only. Split from metrics_panel() because these
    strings change every single second, whereas the metric cards below
    only change when the numbers do."""
    last = state["last_updated"]
    ui.label(f"Updated {last.strftime('%H:%M:%S') if last else '—'}").classes("text-sm text-gray-400")
    ui.label(f"{state['candle_count']} candles this session").classes("text-sm text-gray-400")
    feed = state.get("feed")
    if feed:
        stale = feed.seconds_since_last_tick()
        if stale is None:
            ui.label("Waiting for first tick…").classes("text-sm text-gray-400")
        elif stale > config.TICK_STALE_SECONDS:
            ui.label(f"⚠ No tick for {stale:.0f}s").classes("text-sm text-red-400")
        else:
            ui.label(f"● live ({stale:.0f}s ago)").classes("text-sm text-green-400")


@ui.refreshable
def metrics_panel():
    contract = state["contract"]
    with ui.row().classes("w-full items-center justify-between"):
        with ui.column():
            ui.label(f"{contract.tradingsymbol}").classes("text-2xl font-bold")
            ui.label(f"Expires {contract.expiry}").classes("text-sm text-gray-400")
            profile = state["profile"]
            if profile:
                ui.label(f"Logged in as {profile.get('user_name')} ({profile.get('user_id')})").classes("text-sm text-gray-400")
        with ui.column().classes("items-end"):
            status_header()

    if state["error"]:
        ui.label(state["error"]).classes("text-red-400")

    result = state["result"]
    with ui.row().classes("w-full gap-4 flex-wrap"):
        for label, value, color in [
            ("LTP", state["current_price"], "text-green-400"),
            ("VWAP", state["vwap"], "text-purple-400"),
            ("EMA 10", state["ema_10"], "text-sky-400"),
            ("POC", result.poc if result else None, "text-red-400"),
            ("VAH", result.vah if result else None, "text-yellow-400"),
            ("VAL", result.val if result else None, "text-yellow-400"),
        ]:
            with ui.card().classes("flex-1 min-w-[100px]"):
                ui.label(label).classes("text-sm text-gray-400")
                ui.label(f"{value:.2f}" if value is not None else "—").classes(f"text-xl font-bold {color}")

        oi_value = state.get("oi")
        oi_pattern = state.get("oi_pattern") or "OI N/A"
        oi_color = ("text-green-400" if oi_pattern in ("Fresh Longs", "Fresh Shorts")
                    else "text-orange-400" if oi_pattern in ("Short Covering", "Long Liquidation")
                    else "text-gray-400")
        with ui.card().classes("flex-1 min-w-[140px]"):
            ui.label("OI").classes("text-sm text-gray-400")
            ui.label(f"{oi_value:.0f}" if oi_value is not None else "—").classes("text-xl font-bold")
            ui.label(oi_pattern).classes(f"text-xs {oi_color}")


_STATUS_COLOR = {Status.ENTRY: "text-green-400", Status.WAIT: "text-gray-400"}
_DIRECTION_COLOR = {Direction.LONG: "text-green-400", Direction.SHORT: "text-red-400", Direction.NEUTRAL: "text-gray-400"}
_GRADE_COLOR = {Grade.A_PLUS: "text-green-400", Grade.A: "text-green-300", Grade.B: "text-yellow-400", Grade.IGNORE: "text-gray-500"}

@ui.refreshable
def signal_panel():
    """Display only — reads the latest snapshot/decision evaluate_signals()
    stored on `state`."""
    snapshot = state.get("signal_snapshot")
    decision = state.get("decision")
    if not snapshot or not decision:
        return

    with ui.card().classes("w-full p-4 gap-2"):
        with ui.row().classes("items-center gap-4"):
            ui.label(decision.status.value).classes(f"text-2xl font-bold {_STATUS_COLOR.get(decision.status, '')}")
            ui.label(decision.direction.value).classes(f"text-xl font-bold {_DIRECTION_COLOR.get(decision.direction, '')}")
            ui.label(decision.grade.value).classes(f"text-xl font-bold {_GRADE_COLOR.get(decision.grade, '')}")
        with ui.row().classes("gap-2 flex-wrap"):
            for label, key, category_state in [
                ("Trend", "trend", snapshot.trend), ("Pullback", "pullback", snapshot.pullback),
                ("Rejection", "rejection", snapshot.rejection), ("Volume", "volume", snapshot.volume),
                ("OI", "open_interest", snapshot.open_interest),
                ("POC", "poc", snapshot.poc), ("VAH", "vah", snapshot.vah), ("VAL", "val", snapshot.val),
            ]:
                ui.chip(f"{label}: {category_state.value}", color="blue-grey").tooltip(snapshot.details.get(key, ""))


@ui.refreshable
def current_alert_panel():
    """Alert Engine output only — this never computes a decision itself,
    just displays the latest AlertRecord signal_panel() produced."""
    alert = state.get("current_alert")
    if not alert:
        return
    with ui.card().classes("w-full p-4 gap-1"):
        ui.label("Current Alert").classes("text-sm font-bold text-gray-300")
        with ui.row().classes("items-center gap-4"):
            ui.label(alert.timestamp.strftime("%H:%M:%S")).classes("text-sm text-gray-400")
            ui.label(f"{alert.grade} {alert.direction}").classes(
                f"text-lg font-bold {_DIRECTION_COLOR.get(Direction(alert.direction), '')}")
            ui.label(f"@ {alert.current_price:.2f}" if alert.current_price is not None else "@ —") \
                .classes("text-sm text-gray-400")
            ui.label(f"{alert.confidence}% confidence").classes("text-sm text-gray-400")
        ui.label(" | ".join(alert.reason_list)).classes("text-xs text-gray-500")


@ui.refreshable
def alert_history_panel():
    rows = [
        {"time": a.timestamp.strftime("%H:%M:%S"), "direction": a.direction, "grade": a.grade,
         "price": a.current_price, "confidence": a.confidence}
        for a in reversed(_alert_engine.history[-20:])
    ]
    if not rows:
        ui.label("No alerts yet.").classes("text-sm text-gray-500")
        return
    ui.table(
        columns=[
            {"name": "time", "label": "Time", "field": "time"},
            {"name": "direction", "label": "Direction", "field": "direction"},
            {"name": "grade", "label": "Grade", "field": "grade"},
            {"name": "price", "label": "Price", "field": "price"},
            {"name": "confidence", "label": "Confidence", "field": "confidence"},
        ],
        rows=rows,
    ).classes("w-full")


def alert_history_section():
    """Expansion wrapper rendered once — same pattern as history_section()."""
    with ui.expansion("Alert History", icon="notifications").classes("w-full"):
        alert_history_panel()


async def send_test_telegram():
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        ui.notify("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first (see README), then restart.",
                   type="warning")
        return
    ui.notify("Sending test message…")
    # Telegram's HTTP call blocks — run off the event loop, same as Kite's calls.
    ok = await run.io_bound(_alert_engine.send_test_telegram_message)
    if ok:
        ui.notify("Test message sent — check Telegram.", type="positive")
    else:
        ui.notify("Failed to send — check your bot token/chat ID and the terminal log.", type="negative")


def settings_panel():
    """Rendered once — never recreated by polling, so typed values and expand state persist."""
    with ui.expansion("Settings", icon="tune").classes("w-full"):
        with ui.row().classes("items-center gap-4"):
            # Explicit widths: Quasar truncates .q-field__label with an ellipsis
            # when the label is wider than its field, which was hiding the
            # "Value area %" caption at the inputs' intrinsic width.
            bin_input = ui.number("Bin size (pts)", value=config.BIN_SIZE, min=1, step=1).classes("w-44")
            va_input = ui.number("Value area %", value=config.VALUE_AREA_PCT * 100,
                                 min=10, max=100, step=5).classes("w-44")

            def apply_settings():
                config.BIN_SIZE = float(bin_input.value)
                config.VALUE_AREA_PCT = float(va_input.value) / 100
                if state["live_session"]:
                    state["live_session"].set_profile_parameters(config.BIN_SIZE, config.VALUE_AREA_PCT)
                    # Pull the rebuilt profile into `state` before redrawing —
                    # otherwise the forced redraw below paints the OLD profile and
                    # the new one only lands on a later, throttled update.
                    state.update(state["live_session"].snapshot())
                update_chart(force=True)  # bypass the throttle — the user is watching for this
                ui.notify("Applied — profile rebuilt immediately.", type="positive")

            ui.button("Apply", on_click=apply_settings)

        polling_switch = ui.switch("Live feed enabled", value=state["polling_enabled"])
        polling_switch.on_value_change(lambda e: set_live_feed_enabled(e.value))
        ui.button("Reconnect now", icon="refresh", on_click=force_reconnect)

        with ui.row().classes("items-center gap-2"):
            ui.button("Test Telegram Alert", icon="send", on_click=send_test_telegram)
            ui.label("Sends a plain test message — verifies TELEGRAM_BOT_TOKEN/CHAT_ID, "
                     "doesn't touch Alert History.").classes("text-xs text-gray-500")


@ui.refreshable
def history_panel():
    rows = read_recent_log(20)
    if not rows:
        ui.label("No history yet.")
    else:
        ui.table(
            columns=[
                {"name": "timestamp", "label": "Time", "field": "timestamp"},
                {"name": "poc", "label": "POC", "field": "poc"},
                {"name": "vah", "label": "VAH", "field": "vah"},
                {"name": "val", "label": "VAL", "field": "val"},
                {"name": "oi_pattern", "label": "OI Pattern", "field": "oi_pattern"},
            ],
            rows=rows,
        ).classes("w-full")


def history_section():
    """Expansion wrapper rendered once, so polling refreshes the table without collapsing it."""
    with ui.expansion("History (last 20)", icon="history").classes("w-full"):
        history_panel()


@ui.refreshable
def main_panel():
    if state["login_url"]:
        if state["error"]:
            ui.label(state["error"]).classes("text-red-400")
        with ui.card().classes("w-full gap-4 p-6"):
            ui.label("Kite login required (once per trading day)").classes("text-lg font-bold")
            ui.label("Step 1 — open this link and log in with your Kite credentials + 2FA/TOTP. "
                      "This dashboard detects the login automatically once you finish.").classes("text-sm text-gray-400")
            ui.link("Open Kite login page ↗", state["login_url"], new_tab=True).classes("text-base")
            ui.label("Didn't redirect back here? Paste the request_token from the redirect URL instead:").classes("text-sm text-gray-400 mt-2")
            with ui.row().classes("w-full items-center gap-2"):
                token_input = ui.input(placeholder="Paste request_token here").classes("flex-grow").props("clearable autofocus")
                token_input.on("keydown.enter", lambda: do_login(token_input.value))
                ui.button("Complete login", on_click=lambda: do_login(token_input.value))
        return

    if not state["kite"]:
        if state["error"]:
            ui.label(state["error"]).classes("text-red-400")
        ui.spinner(size="lg")
        ui.label("Connecting to Kite…")
        return

    with ui.card().classes("w-full p-4 gap-2"):
        ui.label("Switch instrument / contract").classes("text-sm font-bold text-gray-300")
        with ui.row().classes("w-full items-center gap-4"):
            inst_input = ui.input(
                label="Tradingsymbol (e.g. BANKNIFTY26AUGFUT)",
                value=state["contract"].tradingsymbol if state["contract"] else "",
            ).classes("flex-grow").props("clearable")
            ui.button("Go Live", icon="bolt",
                      on_click=lambda: go_live_with_custom(inst_input.value))

    if not state["contract"]:
        if state["error"]:
            ui.label(state["error"]).classes("text-red-400")
        ui.label("Waiting for futures contract resolution…")
        return

    # Panels are rebuilt from scratch here, so drop the fingerprints —
    # they're re-established on the next tick.
    _panel_signatures.clear()
    # Decision first: it's what's actually being watched tick to tick, and the
    # metric cards + chart below are tall enough to push it off the first screen.
    signal_panel()
    current_alert_panel()
    metrics_panel()
    chart_view()
    settings_panel()
    alert_history_section()
    history_section()


try_restore_session()

ui.dark_mode(True)
with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
    with ui.row().classes("items-center gap-3"):
        ui.label("BNF Kite Dragon").classes("text-3xl font-bold")
        # Colour is the redundant channel — the text carries the meaning.
        # Green = the VM, the authoritative session. Amber = a local process,
        # which should not normally be running while the VM service is up.
        # A dry run is never the authoritative session, so it can never be green
        # however BN_INSTANCE is set — the badge must not be able to read as live.
        ui.badge(f"{INSTANCE} (DRY RUN)" if config.DRY_RUN else INSTANCE).classes(
            "text-sm " + ("bg-green-700" if INSTANCE == "VM" and not config.DRY_RUN
                          else "bg-amber-600")
        )
    main_panel()

# Retries starting the live stream periodically — covers launching before
# market open, or the feed needing a restart. Cheap: start_live_stream()
# returns immediately once a feed is already running.
# ENGINE on app.timer — runs with no browser attached. This is the 10 Aug fix.
app.timer(1.0, engine_tick)
app.timer(30.0, engine_poll)
app.timer(0.1, engine_poll, once=True)
# SCREEN on ui.timer — correctly client-bound; nothing to draw with nobody there.
ui.timer(1.0, refresh_dashboard_ui)

app.on_shutdown(_runner.close)
# Let any queued Telegram messages finish sending before the process exits.

# host: NiceGUI defaults to 0.0.0.0, which on the VM would expose a page carrying
# a Kite login panel to every interface. Bound to loopback and reached over an
# SSH tunnel instead, so nothing listens off-box and no auth layer is needed.
# BN_HOST=0.0.0.0 restores LAN access on the dev machine when you want it.
# show: the default True tries to spawn a browser at startup — there isn't one
# on a headless VM. The URL is still printed.
# Instance first in the title: tab strips truncate from the right, and the tab
# strip is exactly where two localhost:8080 pages get confused.
ui.run(title=f"{INSTANCE} — BN Volume Profile",
       host=os.environ.get("BN_HOST", "127.0.0.1"),
       port=8080, show=False, reload=False)
