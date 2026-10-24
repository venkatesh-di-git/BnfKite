# Backtest: external-method tests (24 Oct 2026)

**Status:** exploratory. Nothing changes in the playbook or the scanner. Leads go to the archive run.
**Data:** BANKNIFTY26OCTFUT 1-min bars with futures OI, plus NIFTY BANK spot 1-min, from Kite. Sessions 24 Sep – 23 Oct 2026 (20). IS = up to 15 Oct, OOS = 16–23 Oct.
**Engine:** `claude/bt_external.py` (raw output in `claude/bt_external_results.txt`).
**Base:** the same A / A+ VWAP-pullback signals as `bt_vwap_pullback.py`. The baseline reproduces the earlier docs: A = 33 trades, +8; A+ IS = 8 trades, +108; A+ OOS = 5 trades, 60%, +6.
**Execution:** entry at the next 1-min open, stop 20 / target 30, 2 pts slippage each way, unless stated.

**Context to keep in mind:**
- October futures fell about 2,280 pts over the window, and 12 of 20 days fell from 10:00 to 15:15. Any short-leaning rule looks good by drift alone.
- The random-direction benchmark (same entries and exits, random side) for A is 45%, −1.5. The 90% band is −8.8 to +4.8, so **A at +0.2 sits inside random. A+ at +10.6 is at the top edge of its band.**

## Results

| # | Test | Result | Verdict |
|---|---|---|---|
| T1 | **OI in the grade.** Signal-bar OI vs the previous 5m bar, using the live ±0.05% rule. | A by OI state:<br>• Rising: 14 trades, 64%, +8.1<br>• **Flat: 11 trades, 27%, −10.4**<br>• Falling: 8 trades, 50%, +1.0<br><br>A+ with OI Rising: 7 trades, 86%, +18.9, but **43%, −2.6 at +1 min latency**. A+ without OI: 13 trades, 69%, +10.6.<br><br>**A with "OI not Flat": 22 trades, 59%, +5.5 at both 0 and +1 min latency.** | **Don't remove OI yet.** The weak state is Flat, not Falling. The live engine does the opposite: Flat passes as a weakness and Falling blocks. **Lead: block Flat.** |
| T2 | **Time-of-day relative volume.** Baseline = the same 5m slot over ≤10 prior front-month sessions, minimum 5. | Opening bias confirmed: at 09:15–09:40, MA20 reads > 1.3 on 75% of bars, TOD on 10%.<br><br>Only 19 signals have a TOD baseline (13–23 Oct). On them, A+ by TOD > 1.0 was 8 trades, −42, vs A+ by MA20 at 8 trades, +8. | **Inconclusive.** Too few days. Re-run on the archive; keep MA20 meanwhile. |
| T3 | **Day type at 10:00.** One reading per day, outcome measured to 15:15. | **c3, VWAP distance ≥ 75: 12 days, 83%, +104**, covering both directions (5 up, 7 down).<br>Noise band (spot move from the open ÷ the 14-day average move at 10:00, threshold 1×): 9 days, 56%, −28.<br>Open-drive: 6 days, 83%, +102, but all 6 were down days.<br>Rejection-reverse: 11 days, 45%.<br>Gap extension: 7 days, −114.<br>Combining the features adds nothing. A/A+ split by noise band shows no gain. | **Keep c3 as the day-type lead.** Drop the noise band in this form. Open-drive can't be separated from October's drift. The gap lead (no fill before 11:00) doesn't carry to the close. |
| T4 | **Stop-entry** 1 pt beyond the signal bar, valid 15 min. | A+: −114 to −214, against +138 at market. Worse under both the optimistic and pessimistic fill assumption, and at both latencies. | **Reject.** Chasing the bar costs the edge, consistent with the earlier finding that fills nearest VWAP win. |
| T5 | **Pullback depth / second entry.** | **A+ with VWAP pierce ≤ 10: 5 of 5, +26.0, and 4 of 5, +16 at +1 min.** A: pierce 0–10 is 58%, +5.2; 10–20 is 36%, −5.8.<br>Second+ signal after the last session extreme: 50%, +1.0, vs first at 48%, −0.2. | **Depth: lead,** but it is the same October data the wick lead came from, so it is **not an independent confirmation**. **Second entry: drop.** |
| T6 | **Trades per day** (A, in the order taken). | Trade #1: 43%, −2.6. #2: 67%, +9.3. #3+: 7 trades, 29%, −9.7.<br>Cap 2/day: 26 trades, +76, vs +8 uncapped. Stop after 2 losses: no change. | **Lead: cap 2/day.** n is tiny. |
| T7 | **ADX(14, 5m)** vs the 15m EMA20 slope. | Worse at every threshold. ADX > 25 with DI agreeing: 10 trades, −40. ADX < 20: 27 trades, 41%, −98. | **Drop.** The 15m slope stays. |
| S1 | **Dynamic exits** (separate track). | 20-pt stop + hold, or + VWAP trail: stopped out 90%+ of the time, A −230 to −366.<br>**VWAP-only exit with a 60-pt disaster stop:** A at 22% win, avg +19.5, total +527, DD −278. **The random-direction version of the same exit averages +5.5, band −18.7 to +29.5**, so the result is the October trend, not the signal.<br>Target 30, then trail: A+ +170 vs +138, but OOS worse. | **No change.** Keep 20/30 (target 40 stays a logged variant). Revisit a trailing exit only on c3-flagged days. |
| S2 | **2σ VWAP fade on balance days** (separate strategy). | The balance-day rule (flat VWAP 09:45→10:30 and price inside prior value) found 1 day out of 20, in a trending month. The unconditional fade lost: 29 trades, −146. | **Untestable in October.** Needs the archive with range-bound months. |

## For the archive run (in order)

1. **OI state on A:** block Flat vs block Falling vs no OI gate. This is the one with potential: 22 trades, robust to latency, and it reverses the current live logic. Use the live definition, the 5m bar vs the previous bar at ±0.05%.
2. **A+ pierce ≤ 10 pts.**
3. **c3 day type:** VWAP distance ≥ 75 at 10:00, as a daily label, forward to the close.
4. **Cap 2 trades/day.**
5. **TOD relative volume** vs MA20. It needs ≥ 5 front-month sessions of slot history, so the archive should handle it.

Each of these was picked on the same 20 sessions. Freeze the rules as written in `bt_external.py`, and judge them only on archive data outside 24 Sep – 23 Oct, against the random-direction band.
