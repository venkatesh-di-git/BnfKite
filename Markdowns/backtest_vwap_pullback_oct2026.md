# Backtest finding: VWAP pullback with volume confirmation

**Status:** provisional. The setup is not in the playbook yet. Re-validate at month end with the rules unchanged.
**Tested on:** 15 Oct 2026
**Data:** BANKNIFTY26OCTFUT, 1-min Kite bars, **24 Sep – 15 Oct 2026 (15 sessions)**. This is the in-sample period.

## Usable result (the only one that cleared 65% on win rate, average and drawdown together)

| Setup (5-min) | Exit | Trades | Win % | Avg pts | Total pts | Max DD | Worst trade |
|---|---|---|---|---|---|---|---|
| **A+**: VWAP pullback + 15m EMA20 slope + volume > MA20 | Stop 20 / target 30 | 8 | 75 | **+13.5** | +108 | −48 | −24 |
| A+ | Stop 20 / target 25 | 8 | 75 | +9.8 | +78 | −48 | −24 |
| A+ | Signal-bar stop (10–25) / target 30 | 8 | 75 | +12.2 | +98 | −58 | −29 |
| VWAP pullback + volume, bar also closes beyond 5m EMA10 (no 15m filter) | Stop 20 / target 30 | 9 | 78 | +14.9 | +134 | −24 | −24 |
| VWAP + volume + slope (earlier run of A+) | Stop 20 / target 30 | 7 | 86 | +18.9 | +132 | −24 | −24 |
| VWAP pullback, playbook score ≥ 75 | Stop 20 / target 30 | 9 | 67 | +9.3 | +84 | −24 | −24 |
| Same, with the 5m-close exit | 40-pt hard stop | 9 | 78 | +10.4 | +94 | −62 | −44 |

The two A+ runs show 7 and 8 trades because the earlier run's trigger also required the signal bar to close above EMA10 (not only above VWAP), which removed one signal. Rows 1-3 are the same 8 trades with different exits. Row 4 has no 15m filter, but its trigger also required the close to be beyond EMA10. Re-check on 15 Oct: its 9 trades are identical to '5m EMA10 slope agrees + volume >= 1.0x' (Candidate 2 below). A VWAP pullback with volume and no trend filter at all is 11 trades, 64%, +86. Rows 6-7 are the same 9 trades: VWAP-touch signals passing a replica of the playbook 6-factor score >= 75, with the fixed 20/30 exit and the playbook exit respectively.

All figures are in **futures points, net of 2 pts slippage each way**. As a rough conversion, 1 futures point ≈ ₹450 at 900 qty of an ATM option (delta ≈ 0.5).

**A+ trade log (stop 20 / target 30):**

| Date | Signal | Side | Entry | Result | Pts |
|---|---|---|---|---|---|
| 25 Sep | 10:44 | Short | 55,941 | Target | +26 |
| 25 Sep | 12:49 | Short | 55,901 | Target | +26 |
| 30 Sep | 14:29 | Short | 55,120 | Target | +26 |
| 01 Oct | 09:49 | Long | 55,185 | Target | +26 |
| 07 Oct | 14:29 | Long | 55,459 | Target | +26 |
| 13 Oct | 11:39 | Long | 55,417.6 | Stop | −24 |
| 15 Oct | 09:39 | Long | 55,660 | Stop | −24 |
| 15 Oct | 09:49 | Long | 55,651.4 | Target | +26 |

## Exact rules (freeze these for re-validation)

**Grade A: VWAP pullback in the HTF direction**
1. **Filter:** the slope of the 15-min EMA20 at the last *completed* 15-min bar must be > 0 for longs and < 0 for shorts. The EMA is continuous across sessions.
2. **Long trigger (5-min bar):**
   - The previous 5-min bar closed **above** session VWAP.
   - The signal bar's low is ≤ VWAP.
   - The signal bar closes **above** VWAP, and the close is above its open (green bar).
3. **Short trigger:** the mirror image: previous close below VWAP, high ≥ VWAP, red bar closing below VWAP.
4. **Signal window:** the signal bar closes between 09:30 and 15:00.

**Grade A+ = Grade A plus volume.** The signal bar's volume must be above the average volume of the previous 20 five-minute bars.

**Execution as tested**
- Entry at market on the open of the next 1-min bar.
- Stop 20 pts, target 30 pts (futures). The 20/25 setting was also tested.
- One position at a time. Forced exit at 15:15.
- No averaging and no adding.
- If the stop and target are both touched in the same 1-min bar, the trade is counted as a stop.
- VWAP is the session VWAP from 1-min bars, using typical price × volume.

## What was tested and rejected (so it isn't re-tried blindly)

- **3-min chart:** every setup lost. The median 1-min range was about 25 pts, roughly the size of the target, so results were mostly noise.
- **5-min EMA10 pullback** (current playbook trigger): 138 trades, 29% won, −1,312 pts.
- **Playbook 6-factor score:** won 31–33% at every score level, so it doesn't predict outcomes. Volume was the only factor that helped. VP and trend points rewarded moves that were already extended.
- **Trap / failed VWAP break, VWAP 2σ band fade, ORB + first pullback, EMA 9/21 cross:** all negative.
- **Skip the first stop hit:**
  - With no limit, it is effectively no stop. One trade sat 188 pts against.
  - With a 40-pt hard stop, it is just a 40-pt stop. Drawdown doubled.
- **Wider stops (30/40) or close-based and time stops:** the win rate rose because losses were bigger. Total profit was flat and drawdown roughly doubled.
- **Split entry, market + add at VWAP −5:** this is averaging down. Of the 6 trades where both halves filled, 5 were losers. Worst trade was −53 to −63.
- **Limit entry at VWAP ±5:** filled only 10 of 20 signals. The ones it missed were mostly winners.
- **A without volume confirmation:** 12 trades, 50% won, +12 pts. Roughly breakeven, so skip it or trade it at half size at most.

## Month-end validation plan

- **Out-of-sample window:** 16 Oct – 27 Oct 2026 (October expiry), BANKNIFTY26OCTFUT. Run the rules exactly as above, with no parameter changes.
- **Pass criteria:** win rate > 60% **and** average ≥ +5 pts/trade at stop 20 / target 30.
- **Expected sample:** about 4–6 A+ trades. Combine them with the in-sample trades before deciding, and note the combined count.
- **If it passes:** update the playbook. A+ becomes the core entry and the EMA10 pullback trigger is dropped.
- **If it fails:** keep the playbook unchanged and record the result here.
- **Re-run:** the engine is saved alongside this file as `claude/bt_vwap_pullback.py`. Usage: `python3 bt_vwap_pullback.py <kite_1min_json> <trades_out.json>`. Feed it 1-min bars of the reference contract, plus one prior session for EMA seeding. Before running, set `FIRST` in the script to the first session to trade; it is currently "2026-09-24".

**Caveat:** 8 A+ trades is a small sample. At that size a 75% win rate is not statistically different from about 50%.

## Live-engine mapping (added 15 Oct, from the scanner code review)

The backtest A / A+ are **not** the live scanner's A / A+. Do not read this result as validating the live grades.

| | Backtest A+ | Live engine (Bnf_kite) |
|---|---|---|
| HTF filter | 15m EMA20 slope must agree | **None.** Trend is session VWAP + 5m EMA10 slope (±4.3 /min), no 15m input |
| Volume | Completed 5m bar volume > MA20 of the prior 20 bars (ratio > 1.0) | Forming bar's volume projected to 300 s ÷ SMA20; **"High" needs ratio ≥ 1.30** (Low < 0.80); Unknown for the first 30 s of a bar |
| VWAP pullback trigger | Prior close on the trade side; wick touches VWAP; closes back beyond it, in the trade direction | Same shape, but with a ±10 pt proximity band to VWAP, plus six gates that must all pass |
| Grade | HTF slope + volume > MA20 | Counts only two weaknesses: Volume = Normal and OI = Flat. A+ = neither, A = one, B = both. 15m trend, VP and POC play no part in the grade |
| Telegram | n/a | All of B, A and A+ are sent. Opposite-direction alerts within 5 min are withheld unless A/A+ |

**Backtest A+ trades at the live volume threshold (ratio ≥ 1.30, completed-bar volume):** 6 of the 8 trades remain (the 01 Oct and 13 Oct trades had ratios of 1.10 and 1.12): 5 wins, 1 loss. The live engine uses a projected, mid-bar ratio, so the real set would differ. Sample is far too small to conclude anything.

**Open items before the month-end run:** (1) add the 15m EMA20 slope direction and `vol_ratio` (bar volume ÷ SMA20, both projected and final) to `signal_log.csv`; (2) log suppressed and B alerts, with grade; (3) decide whether to validate the backtest rules as written or port the live trigger and gates exactly.

## Candidate variants for the month-end run (added 15 Oct)

Trend filter × volume threshold on VWAP-pullback signals (5-min, stop 20 / target 30, in-sample 24 Sep – 15 Oct):

| Trend filter | No volume gate | Volume ≥ 1.0x MA20 | Volume ≥ 1.3x MA20 (live "High") |
|---|---|---|---|
| None | 31 trades, 55%, +106 | 11, 64%, +86 | 8, 62%, +58 |
| 15m EMA20 slope agrees | 20, 60%, +120 | 8, 75%, +108 | **6, 83%, +106 (DD −24)** |
| 5m EMA10 slope sign agrees | 28, 57%, +128 | **9, 78%, +134 (DD −24)** | 7, 71%, +82 |
| 5m EMA10 slope beyond ±4.3/min (live threshold) | 3 | 2 | 2 (too few to judge) |

- **Candidate 1:** 15m EMA20 slope + volume ≥ 1.3x. **Candidate 2:** 5m EMA10 slope sign + volume ≥ 1.0x.
- Both were the best-looking cell in their row of a 12-cell grid, on 6–9 trades. The gap between them is 1–2 trades. Treat as rival hypotheses, not a finding.
- **Month-end run must compare all cells above on the logged live signals**, with no re-tuning, and report trade counts. Pass criteria stay as above (win rate > 60% and average ≥ +5 pts at stop 20 / target 30).
- Slope here = 5m EMA10 change per bar ÷ 5 (pts/min), a rough match for the live definition (live EMA10 sampled 60 s apart).
- 5m slope against the trade: 3 trades, 1 winner (0 for 2 with volume). Tiny, but it points the same way as the filters.

## Scanner changes decided 15 Oct (Venky's decisions, not yet verified in code)

1. **B alerts removed from Telegram.** They are still to be logged (alert_log / signal_log) with their grade, so B is not lost from the dataset. The change is at delivery only.
2. **15m EMA20 slope is calculated and logged with each alert. It does not affect the grade or any gate.** Use the last *completed* 15m bar, seeded from prior sessions, so the first alerts of the day are not computed from a cold EMA.
3. **Volume ratio logged as a number**, so thresholds of 1.0 / 1.1 / 1.2 / 1.3 can all be tested later from one column. Log both the projected ratio at alert time and the final ratio at 5m bar close (the final ratio is what the backtest used).
4. **Live grade definition is unchanged** (Volume High ≥ 1.30 and OI Rising). The backtest A+ is not the same as live A+; see the mapping above.

---

## Instructions for VS Code / Claude Code (scanner repo `Bnf_kite`)

Purpose: three small, **logging-and-delivery-only** changes so the month-end validation can run on the scanner's own data. **Do not change any gate, grade, threshold, trend state or setup logic.** Work in plan mode first: show the exact diff and wait for approval before writing anything. Run locally with `BN_DRY_RUN=1` so no duplicate Telegram alerts go out.

File references come from the 15 Oct code review and may have drifted; confirm each location before editing.

### Change 1: stop sending B alerts to Telegram (keep them in the logs)
- `alert_engine.py:38-39`: `TRADING_GRADES` includes B, and `_deliver_telegram` (`alert_engine.py:448-466`) has no grade filter.
- Add a delivery-only filter so B is not sent to Telegram. Prefer a config constant, e.g. `TELEGRAM_GRADES = {"A", "A+"}`, so it can be reversed in one place.
- B alerts must **still** be written to `alert_log.csv` and `signal_log.csv` with their grade. Do not remove B from `TRADING_GRADES` and do not touch the latch, the flip cooldown or the upgrade re-fire logic (B→A→A+ must still re-fire as before).
- Check: a simulated B alert appears in `alert_log.csv` and does not call the Telegram sender. A and A+ still send.

### Change 2: compute the 15m EMA20 slope and log it with each alert (no effect on grade or gates)
- There is no 15-minute logic in the live engine today (zero hits for `15m`, `htf`, `resample`, `ema20`).
- Build 15-minute bars from the same 5-minute bars or ticks the engine already holds. Calculate EMA20 on 15m closes, **seeded from prior sessions** so the first alerts of the day are not computed from a cold EMA (reuse the existing seed-bar mechanism used for the 20-bar volume average and EMA10, which pulls prior-session bars).
- Use only the **last completed** 15m bar. Slope = EMA20 at the last completed 15m bar minus the EMA20 at the 15m bar before it, in points per 15m bar.
- Log, with every row written to `alert_log.csv` and `signal_log.csv`: `ema20_15m`, `slope_15m`, and `slope_15m_dir` (+1 / −1 / 0).
- **This value must not be read by `decision_engine.py` or `signal_engine.py` for grading or gating.** It is a logged field only.
- Check: with a fixed set of replayed bars, grades, gate outcomes and alert counts are **byte-identical** before and after the change (use the golden-file replay; engine_version must not change for decision logic). Only new columns appear.

### Change 3: log the volume ratio as a number
- Current logic: `projected = candle.volume × (300 / elapsed_seconds)`; `relative = projected / sma20_volume` (`signal_engine.py:303-304`); `sma20_volume` from `_average_recent_volume` (`engine.py:362-371`); High is `relative ≥ 1.30`, Low `< 0.80`.
- Add two logged numeric fields, with no change to the High/Normal/Low classification:
  - `vol_ratio_projected`: the `relative` value used at the time of the row/alert.
  - `vol_ratio_final`: `final bar volume ÷ sma20_volume` at the 5-minute bar close (the completed-bar ratio). Write it to the row for the bar's closing tick, and make it available to alerts fired on that bar.
- Reason: the backtest used the completed-bar ratio. `signal_log.csv` only writes on state change, so today the final ratio is not recorded. Logging a number (not a pass/fail) lets any threshold (1.0, 1.1, 1.2, 1.3) be tested later from one column.
- Check: for a replayed day, `vol_ratio_projected` reproduces the existing High/Normal/Low labels at the 1.30 and 0.80 thresholds.

### Fields to add (summary)

| Field | In which logs | Affects grade or gates? |
|---|---|---|
| `ema20_15m`, `slope_15m`, `slope_15m_dir` | `alert_log.csv`, `signal_log.csv` | No |
| `vol_ratio_projected`, `vol_ratio_final` | `alert_log.csv`, `signal_log.csv` | No |
| `grade` and `suppressed` flag for withheld alerts | `alert_log.csv`, `suppressed_log.csv` (confirm both already carry the grade) | No |

Also record the new columns in `run_manifest.csv` (a `logging_schema_version` string is enough) so rows before and after the change can be told apart.

### Done when
1. B alerts no longer reach Telegram but are in `alert_log.csv` with grade B.
2. Replay of a past session gives **identical** grades, gate results and alert timestamps before and after.
3. The new columns are populated on every alert row, with a non-empty `slope_15m` from the first alert of the session (proves the seeding works).
4. A short note of what was changed, with file and line references.

### Do not
- Add the 15m slope to the grade, to any gate, or to the trend state.
- Change the 1.30 / 0.80 volume thresholds, the 4.3 slope threshold, the ±10 pt VWAP band, the latch, or the 5-minute flip cooldown.
- Restart the live service during market hours, or push changes to the VM before reviewing them.

---

## Reanalysis (15 Oct evening): robustness checks on A / A+

All on 5-min, stop 20 / target 30 unless stated. A = 20 trades, 60%, +120; A+ = 8 trades, 75%, +108.

| Check | Result | Reading |
|---|---|---|
| Random entries (same stop/target, 09:30-15:00) | Coin-flip direction: 39% won, -4.5/trade. 15m EMA20 direction: 37%, -5.5/trade | The VWAP pullback trigger beats random clearly (55-60% vs 37-39%). The 15m slope on its own has no edge. |
| Entry 1 minute late | A: unchanged (+120). **A+: 50%, +8** | A+ depends on the first minute after the signal (moves of +26, +58, +21 pts in that minute). Two of 8 trades flip. |
| Entry 2 minutes late | A: 50%, +20. A+: 50%, +8 | Both lose most of the edge. |
| Slippage 3 / 4 pts per side | A: +80 / +40. A+: +92 / +76 | Survives higher costs if entry is immediate. |
| Volume threshold 0.8x to 1.3x | 75-83% across the whole range | Robust plateau: volume is a real effect in this sample, not a lucky threshold. |
| Trend filter neighbours (10/15/30m x EMA10/20/30/50) | With volume: 67-86%. 15m EMA10 and EMA30 are weaker than EMA20 | 15m EMA20 is a local peak. Treat it as "any trend filter", not a specific parameter. |
| Targets (stop 20) | A+: t20 +48, t25 +78, t30 +108, **t40 +168**, t50 +18. A: t40 +240 | 40 beats 30, 50 collapses. Candidate to log, not to adopt. |
| Leave one day out | A total +68..+168; A+ +56..+132 | No single day carries the result. A+ trades fall on only 6 days. |
| Long vs short | A+ shorts 3/3, longs 3/5. A shorts 67%, longs 55% | Shorts did better; period included a strong down leg. |
| Significance vs 48% breakeven | A: p = 0.20. A+: p = 0.12 | **Neither is statistically significant.** Bootstrap 90% CI of avg: A -1.5..+16; A+ +1..+26. |
| Early-session liquidity (24-30 Sep, while Oct was still next-month) | No zero-volume 1-min bars; 0.5-1.1M contracts/day | In-sample data is usable. |

**Strongest findings today (large samples):**
1. 5m EMA10 pullback trigger: 138 trades, 29% won, -1,312 pts.
2. Replica of the 6-factor score: no predictive power across 175 signals (31-33% at every score band).
3. 3-min chart loses for every setup.

**Weakest links:** A+ on 8 trades, the 1-minute latency sensitivity, and selection from roughly 250 configurations tested today on the same 15 sessions.

**Added to the month-end run:** report the latency check (entry at +0 / +1 / +2 min), the random-entry benchmark, and target 40 as a logged variant.

---

## Wider-sample check (17 Oct): Aug + Sep + Oct archives, rules unchanged

**Engine:** `replay/bt_vwap_pullback.py`, a rebuild of the lost `claude/bt_vwap_pullback.py`. On Oct 1-15 it reproduces the A+ trade log above exactly. Run: `python -m replay.bt_vwap_pullback` (`--no-live` skips the live-engine replay).
**Data:** 47 sessions across the three front-month contracts (29 Jul - 25 Aug, 2 - 25 Sep, 1 - 15 Oct). Each contract is run as its own series, with no EMA carried across a roll. 26-31 Aug and 28-29 Sep were never archived. Aug and Sep were not used to build the rules.

**Result: the setup fails.** Stop 20 / target 30, net of 2 pts slippage each way.

| Contract | A+ | A |
|---|---|---|
| Aug | 19 trades, 32%, -156 | 42, 24%, -508 |
| Sep | 11 trades, 9%, -214 | 36, 22%, -464 |
| Oct | 5 trades, 60%, +30 | 14, 57%, +64 |
| **All** | **35 trades, 29%, avg -9.7, total -340** | **92, 28%, avg -9.9, total -908** |

- Exits at target 25 / 40 and entry +1 / +2 min late all lose money too.
- Candidate 1 (15m slope + vol >= 1.3x): 22 trades, 36%, -128. Candidate 2 (5m EMA10 slope sign + vol >= 1.0x): 45 trades, 33%, -347.
- No trend filter at all: 160 trades, 32%, -1,307. This is near the random-entry benchmark, so the trigger shows no edge on the larger sample.
- 15m EMA20 slope as a filter adds nothing (92 vs 160 trades: 28% vs 32%). Keep it logged only.
- Only 5m EMA10 slope beyond +/-4.3 per min (the live threshold) is positive: 10 trades, 70%, +110. Too few to act on; log and watch.
- Live engine replayed (531 trades, fixed 20/30 exit): A+ 42% / -370, A 39% / -1,034, B 31% / -1,460. Every grade loses.
- Verdict against the pass criteria (win rate > 60% and avg >= +5): **not met.** The Oct result looks like an in-sample fit.

**Decision (17 Oct):** no change to the engine, alerts, grades or playbook. Keep the logging-only changes from 15 Oct. Retest after about 15 more sessions (16 Oct onward) with the rules unchanged.

**Before the retest (time-critical):** archive 16 - 27 Oct before BANKNIFTY26OCTFUT settles on 27 Oct, then archive the Nov contract from 28 Oct. A settled contract drops out of `kite.instruments` and its candles can no longer be fetched.
