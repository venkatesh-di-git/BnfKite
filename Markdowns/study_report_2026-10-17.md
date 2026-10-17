# Study report: review-list tests on the full archive

**Run:** 17 Oct 2026. **Code:** `replay/study_suite.py` (read-only), re-run with `python -m replay.study_suite`.
**Data:** BANKNIFTY Aug / Sep / Oct futures archives. 47 front-month sessions traded; 167 VWAP-pullback signals (backtest rules) and 580 live-engine alerts (replayed, 5-min flip cooldown).
**Not in the data:** 26-31 Aug and 28-29 Sep were never archived. The Nov contract and 16 Oct onward are not archived yet.

## Bottom line

1. **No test found a setup, filter or exit that makes money.** Every cut of the data has an average at or below zero. The closest to positive are three small cells (below). None has a 90% interval wholly above zero.
2. **The VWAP-pullback signal is worse than random entries**, not just no better. Matched on side and time of day, random entries average -4.0 pts/trade; the signals average -8.0 (they beat only 3% of 300 random runs).
3. **No stop/target, time stop, breakeven or trail rescues it.** Every cell of the 36-cell stop x target grid is negative.
4. **Two results point at engine changes worth a forward test**, not a change now: the trend gate on the VWAP setup (#11) and a longer flip cooldown (cooldown test).
5. **Grade ordering holds: A+ > A > B** (B is clearly the worst), so dropping B from Telegram on 15 Oct is supported.

**Recommendation:** no engine, alert or playbook change from this report. Log the candidate ideas below and test them on 16 Oct onward, the one stretch of data none of these tests have seen.

## How to read the numbers

All figures are futures points per trade, net of 2 pts slippage each way, stop 20 / target 30, forced exit 15:15, unless stated.
- **raw / ind:** raw signals vs independent trades. Same-day, same-side signals within 30 minutes of the last one counted collapse to one, because same-bar upgrades (B to A to A+) and repeat signals are not independent. 167 signals became 130 independent; 580 live alerts became 302.
- **CI:** bootstrap 90% interval of the average over the independent trades.
- **sess+:** sessions with a positive total / sessions with any trade (sign consistency).
- **Multiple comparisons:** this report has about 100 cells. A few will look good or bad by chance alone. Treat any single cell as a hypothesis.

## Results, in the order you set

### #21 Random-entry benchmark (same side, same time of day, 300 runs)

| Set | Independent trades | Actual avg | Random avg (90% range) | Actual beats |
|---|---|---|---|---|
| All VWAP-pullback signals | 130 | -8.0 | -4.0 (-7.5 to -0.5) | 3% of runs |
| A+ (15m slope + vol > 1.0) | 33 | -11.9 | -5.0 (-11.9 to +1.8) | 4% of runs |

Random entries lose about 4 pts because of costs and a 40% win rate at 20/30. The signals lose twice that. The trigger appears to pick poor moments, not neutral ones. Do not trade this as a reversal (the cost drag stays); log it as a hypothesis.

### #1 MFE / MAE and stop x target

Median distance price went for you / against you after entry (130 independent signals):

| Window | Favourable (p25 / median / p75) | Adverse (p25 / median / p75) |
|---|---|---|
| 5 min | 5 / 14 / 30 | 10 / 25 / 37 |
| 15 min | 12 / 29 / 51 | 23 / 35 / 60 |
| 30 min | 16 / 38 / 70 | 27 / 48 / 86 |

The median adverse move (25 pts in 5 min) is larger than the 20-pt stop. Avg pts/trade by stop (rows) x target (columns):

| stop \ target | 15 | 20 | 30 | 40 | 50 | 60 |
|---|---|---|---|---|---|---|
| 10 | -5.9 | -5.0 | -5.1 | -5.5 | -6.3 | -5.6 |
| 20 | -8.4 | -7.4 | -8.0 | -8.5 | -10.3 | -10.3 |
| 30 | -8.0 | -9.0 | -9.2 | -11.1 | -12.7 | -12.2 |
| 40 | -7.6 | -8.0 | -7.0 | -8.2 | -9.3 | -9.6 |

(Rows 15 and 25 omitted for space; all negative.) Tighter stops lose less only because each loss is smaller. There is no sweet spot. The best cell (-5.0) is optimistic after picking from 36.

### #17 Slippage break-even

Negative at 2 pts per side: all signals -8.0 (CI -11.4 to -4.5, positive in 16 of 41 sessions); A+ -11.9 (CI -17.9 to -5.8, 6 of 23 sessions). At 3 and 4 pts the losses deepen to -10/-12 and -14/-16. The break-even slippage is below zero, so there is no cost level at which it works. **Per your rule, stop here for this setup.**

### #2 Playbook exit vs fixed 20/30: **not run**

The playbook exit is not written down in the repo or the md (the md only mentions a "5m-close exit" with a 40-pt hard stop, without the rule). Write the rule down and this can run.

### #16 Entry delay, per month

| Entry | Aug | Sep | Oct | All |
|---|---|---|---|---|
| +0 min | -7.6 | -12.0 | +2.5 | -8.0 |
| +1 min | -0.2 | -7.0 | +5.4 | -2.1 (CI -5.5 to +1.4) |
| +2 min | -4.4 | -11.0 | +2.5 | -6.0 |
| +3 min | -8.1 | -6.0 | +5.4 | -5.5 |

- Late entry is **not** the cost the 15 Oct reanalysis feared. Entering one minute after the signal is the best row, and the pattern is not smooth (+1 good, +2 worse), so it looks like noise rather than a rule.
- Oct is the only month at or above zero, on 17 independent trades (CI -6.4 to +11.3). Sep is the worst (positive in only 3 of 14 sessions).
- Telegram latency plus reaction time puts live entry at roughly +1 to +3 min: -2 to -6 pts, still negative.

### #3 / #4 Time stop, breakeven, trailing

| Exit | Win % | Avg | CI |
|---|---|---|---|
| Baseline 20/30 | 32 | -8.0 | -11.3 to -4.7 |
| Time stop 15 / 30 min | 32 | -8.4 / -8.0 | no change |
| Breakeven after +10 / +15 | 20 / 23 | -7.7 / -8.2 | no change |
| Trail (trigger 15, distance 10) | 45 | -6.9 | -9.6 to -4.0 |
| Trail (trigger 20, distance 10) | 42 | -6.7 | -9.6 to -3.6 |

Trailing lifts the win rate but not the average. Nothing here turns it positive.

### #11 Gate value (outcome if the only failing gate had passed)

Alerts that fired average -5.4 (EMA setups) and -5.2 (VWAP setup). Blocked events that would have done *better* than that mean the gate may be costing good signals; *worse* means the gate earns its place.

| Gate (events it alone blocked) | Independent | Win % | Avg | Reading |
|---|---|---|---|---|
| **trend** (VWAP setup) | 173 | 47 | **-0.7** (CI -3.7 to +2.6; 29 of 43 sessions +) | **Better than fired.** Gate may be hurting |
| open_interest (VWAP setup) | 133 | 41 | -3.4 | Slightly better than fired |
| open_interest (long EMA) | 50 | 46 | -1.0 | Better than fired, small sample |
| volume (VWAP setup) | 266 | 38 | -5.0 | Same as fired: **no discrimination** |
| volume (short EMA) | 163 | 33 | -7.4 | Worse than fired: useful |
| rejection (VWAP) | 119 | 35 | -6.5 | Slightly worse: mildly useful |
| pullback (VWAP) | 326 | 32 | -8.0 | Worse than fired: **useful** |
| level (VWAP / short) | 56 / 23 | 30 | -8.8 | Worse than fired: **useful** |

The trend-gate result is the strongest cell, and it is one of about 20, so a forward test must confirm it. If it holds, the 4.3 slope trend gate is removing the better VWAP signals. The volume gate looks decorative for the VWAP setup. (Fixed-exit outcomes only, hypothetical entries one minute after the first tick the event appeared.)

### #12 / #13 / #14 Does the grading add value? (live alerts, cooldown 5)

| Slice | Independent | Win % | Avg | CI | sess+ |
|---|---|---|---|---|---|
| Grade A+ | 113 | 42 | -3.2 | -7.2 to +0.3 | 19/42 |
| Grade A | 188 | 39 | -4.6 | -7.5 to -1.7 | 23/46 |
| Grade B | 141 | 30 | **-9.1** | -11.9 to -5.9 | 11/46 |
| Volume ratio 0.8-1.0 | 101 | 36 | -6.2 | -9.6 to -1.7 | 17/44 |
| Volume ratio 1.0-1.3 | 103 | 32 | -8.0 | -11.8 to -4.1 | 17/45 |
| Volume ratio > 1.3 | 205 | 39 | -4.5 | -7.2 to -1.6 | 20/47 |
| Setup ema | 121 | 34 | -7.0 | -10.4 to -3.7 | 18/44 |
| Setup vwap | 155 | 34 | -6.9 | -10.1 to -3.7 | 16/44 |
| Setup ema+vwap | 80 | 44 | -2.1 | -6.5 to +2.2 | 14/28 |
| A+ x ema | 34 | 50 | +1.0 | -6.4 to +8.4 | 10/21 |
| B x ema | 67 | 22 | **-12.8** | -16.5 to -8.3 | 8/39 |
| All live alerts | 302 | 35 | -6.4 | -8.8 to -4.3 | 10/47 |

- **Grade order A+ > A > B is real at the B end.** B is the only grade clearly worse than the others (30%, CI wholly below zero). A+ vs A differs by 1.4 pts, which is within noise.
- **Volume is only weakly informative and not monotonic.** Above 1.3 is best, but the middle band (1.0-1.3) is worse than 0.8-1.0. The engine never fires below 0.8, so that band is empty.
- **ema+vwap is the best setup** (-2.1), and A+ x ema is the only cell at or above zero (+1.0), but its CI runs -6.4 to +8.4.
- Every row's sess+ count is low (the all-alerts row is positive in only 10 of 47 sessions).

### #6 Time of day (signals)

| Bucket | All signals (independent) | A+ |
|---|---|---|
| 09:30-10:30 | 49, 37%, -5.6 (CI -10.7 to +0.5) | 12, 50%, +1.0 |
| 10:30-13:00 | 53, **25%, -11.7** (CI -16.5 to -7.0; 8 of 29 sessions +) | 12, **0%**, -24.0 |
| 13:00-15:00 | 30, 40%, -4.6 (CI -11.3 to +2.6) | 9, 22%, -12.9 |

Midday is the worst bucket on both sets (A+: 0 wins in 12). The CIs of midday and the other two overlap, so call it suggestive.

### #10 OI regime (15 min before entry)

| Regime | Independent | Win % | Avg |
|---|---|---|---|
| Long build-up | 38 | 32 | -8.7 |
| Short build-up | 40 | 25 | -11.5 |
| Short covering | 30 | 33 | -7.3 |
| Long unwinding | 29 | 38 | -5.0 |
| OI supports the side | 97 | 30 | -9.1 |
| OI opposes the side | 34 | 35 | -6.9 |

No regime separates winners from losers, and signals with OI supporting the side do slightly worse. OI adds nothing in this form.

### #5 Slope buckets around 4.3 (5m EMA10 slope in the trade direction, pts/min)

| Bucket | Independent | Win % | Avg | CI |
|---|---|---|---|---|
| Against (< 0) | 36 | 33 | -7.3 | -14.3 to -0.4 |
| 0 to 2 | 71 | 30 | -9.5 | -13.7 to -4.8 |
| 2 to 4.3 | 24 | 17 | **-15.7** | -21.9 to -9.4 |
| > 4.3 (live threshold) | 9 | 67 | +9.3 | -1.8 to +20.4 |

The one positive cell from the 15 Oct grid does not extend smoothly: the bucket right below it is the worst of all. The 9-trade cell is also contradicted by the live engine's own record (alerts that pass its trend gate average -5.4 over 302 independent trades). Likely noise. Forward-test only.

### Cooldown re-test (flip cooldown on the full corpus, one position at a time)

| Cooldown | Alerts | Removed vs 0 | Trades | Win % | Avg | Total pts | Avg of removed alerts |
|---|---|---|---|---|---|---|---|
| 0 (off) | 624 | 0 | 540 | 37 | -5.5 | -2,981 | n/a |
| 3 min | 611 | 15 | 535 | 37 | -5.4 | -2,911 | -14.0 |
| **5 min (live)** | 606 | 19 | 531 | 37 | -5.4 | -2,865 | -10.8 |
| 10 min | 590 | 34 | 519 | 38 | -5.2 | -2,677 | -12.2 |
| 12 min | 584 | 42 | 512 | 38 | -5.3 | -2,703 | -9.8 |
| 15 min | 580 | 44 | 508 | 38 | -5.2 | -2,657 | -9.4 |

- **Direction is right.** The alerts the cooldown removes do worse than average at every setting (-9 to -14 vs -5.5), so suppressing them helps.
- **The 5-minute plateau did not survive.** The 4-session study found 3-10 min suppressed the same three alerts; here removals climb steadily (15, 19, 34 for 3, 5, 10 min). 10-15 min gain 190-260 pts over 5 min across the 47 sessions.
- **Caveats:** the removed-alert averages come from 15-44 alerts and the gap to the overall average is about 1 standard error, so it is not statistically significant. Only B-grade flips can be withheld (A/A+ escape).
- **It may now be moot.** B alerts no longer go to Telegram (15 Oct), so withholding B flips changes little of what you receive. Check whether `_last_alert` should advance on B alerts that are not sent. Leave the cooldown as is for now.

## What to forward-test (16 Oct onward), if you choose to

Each is a hypothesis from this report; freeze the definition now so the test is clean:
1. VWAP-setup alerts that the trend gate blocked (#11): do they beat fired VWAP alerts?
2. Midday (10:30-13:00) signals underperform (#6).
3. Flip cooldown of 10-15 min vs 5 (cooldown test).
4. Slope > 4.3 pts/min in the trade direction (#5), noting the live engine contradicts it.

## Limits of this report

- The fixed 20/30 exit is not how you actually trade; real exits could change the results (#2 not run).
- The live-engine tests use synthetic ticks built from 1-minute bars, so alert times are approximate.
- Signals were tested independently of each other. Real trading holds one position at a time, so absolute P&L differs (the cooldown test is the exception).
- All results are in-sample on 41-47 sessions. The 16 Oct onward data is the only holdout.

---

## Addendum (17 Oct): trend-gate robustness and slope threshold 3.7

Run with `python -m replay.study_suite --only trendgate,slope`.

### Trend-gate lead (hypothesis 1) is weaker than it first looked

VWAP-setup events blocked only by the trend gate vs VWAP alerts that fired (independent trades, stop 20 / target 30):

| Cut | Blocked | Fired |
|---|---|---|
| All | 173, 47%, -0.7 | 207, 38%, -5.2 |
| Stop 10 / target 20 | -3.6 | -4.3 |
| Stop 20 / target 40 | +2.2 | -3.1 |
| Aug / Sep / Oct | +0.5 / -3.5 / +4.9 | -8.7 / -3.1 / -0.3 |
| First half / second half (by date) | +1.9 / **-3.5** | -7.8 / **-2.6** |
| Long / short | +2.2 / -3.2 | -3.8 / -6.3 |

- Difference (blocked - fired): +4.5 pts, 90% CI +0.2 to +8.8, blocked better in 96% of resamples. Borderline, and before allowing for the ~20 gate cells examined.
- **It does not hold across time.** The gap is large in Aug and the first half, absent in Sep, and reversed in the second half.
- 231 of the 271 blocked events sit within 30 minutes of a fired alert on the same side. They are mostly the same pullbacks at an earlier moment. The 40 that stand apart average -0.2 (CI -7.2 to +6.6).
- **Verdict:** a timing/regime effect that was strong early in the sample, not a stable gate failure. Keep as a forward-test item; no change.

### Slope threshold (live engine replayed, cooldown 5)

| Hysteresis | Alerts | One-at-a-time pts (trades) | All grades avg | A / A+ avg | VWAP setups avg |
|---|---|---|---|---|---|
| 3.0 | 679 | -2,921 (574) | -6.0 | -3.8 | -3.5 |
| **3.7** | 651 | **-3,189 (556)** | -6.6 | -4.8 | -4.0 |
| **4.3 (live)** | 606 | -2,865 (531) | -6.4 | -4.0 | -5.2 |
| 5.0 | 555 | -2,208 (483) | -5.1 | -3.8 | -5.6 |

- **3.7 does not help.** Total points are worse than live (-3,189 vs -2,865) and A/A+ average is worse (-4.8 vs -4.0).
- Nothing here is a lever: every value loses, the total is not smooth (3.0 better than 3.7), and all intervals overlap.
- VWAP-setup average improves steadily as the threshold falls (-5.6, -5.2, -4.0, -3.5), but the extra trades are still negative, so total points do not.
- Not changed: the threshold stays at 4.3.
