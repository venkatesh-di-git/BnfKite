# BN Smart Assistant — Telegram Alert Format V2
## Findings & Implementation Plan

**Status:** Proposed for review  
**Scope:** Telegram presentation only  
**Core rule:** Same engine decision, same alert timing, same logs/replay — only the Telegram message becomes cleaner.

---

# 1. Objective

The current Telegram alert contains too much diagnostic information for a trader to read quickly.

Example of the current style:

```text
A Long
Price: 57799.0
Trend: Close 57799.00 above VWAP 57734.39, slope +0.73 (holding, exits below -4.30)
Pullback: Low 57744.20 <= EMA10 57778.66 < Close 57799.00
Rejection: Lower wick + bullish candle + Close above EMA10
Volume: Projected relative volume 1.31 >= 1.30 (3870 in 183s -> projected 6336)
Open Interest: OI 2115510 -> 2115150 (-0.02%)
Poc: Close 57799.00 above POC 57737.50
Vah: Close 57799.00 above VAH 57780.00
Val: Close 57799.00 above VAL 57690.00
Vwap Pullback: Low 57744.20 within 10 of VWAP 57734.39 < Close 57799.00
Vwap Rejection: Close 57799.00 held 98% of range above VWAP 57734.39
```

The goal is to turn this into a compact **trader-facing summary**, not another diagnostic log.

The architecture already defines the Alert Engine as responsible for notifications only and explicitly says it should not calculate indicators. fileciteturn23file0L1-L20

---

# 2. Agreed Target Format

```text
🟢 A LONG

Price: 57799

Setup: EMA Pullback + Rejection

VWAP: 57734 ↑
EMA10: 57779 ↑
Slope: +0.73
Volume: 1.31×
OI: Falling -0.02%
VP: Above VAH
```

For a VWAP setup:

```text
🟢 A LONG

Price: 57820

Setup: VWAP Pullback + Rejection

VWAP: 57816 ↑
EMA10: <existing value>
Slope: <existing value>
Volume: 1.44×
OI: Falling -0.01%
VP: Inside Value
```

The exact numbers above are examples only. The formatter must use the values already produced by the running engine.

---

# 3. What We Found — Verified Against V2 Backup

The V2 backup was inspected directly, including:

- `signal_engine.py`
- `decision_engine.py`
- `alert_engine.py`
- `engine.py`
- `session_runner.py`
- `Alert_Dashboard_Spec_V1-1.md`

The previous claim that VWAP/EMA10 values are simply "gone" after the decision is **incorrect**.

## 3.1 VWAP and EMA10 definitely exist as structured upstream data

`signal_engine.py` defines:

```python
@dataclass
class IndicatorSnapshot:
    vwap: Optional[float] = None
    ema10: Optional[float] = None
    ema10_prev: Optional[float] = None
    ...
```

`session_runner.py` explicitly builds this object from the engine snapshot:

```python
indicators = IndicatorSnapshot(
    vwap=s.get("vwap"),
    ema10=s.get("ema_10"),
    ema10_prev=s.get("ema_10_prev"),
    ...
)
```

Therefore:

> **VWAP and EMA10 are NOT lost inside the Indicator/Signal path.**

They are structured values available at evaluation time.

## 3.2 The project already preserves them for measured logging

`engine.py` contains `measured_fields()`:

```python
return {
    ...
    "ema_10": ema10,
    "vwap": indicators.vwap,
    "ema_slope": (
        ema10 - ema10_prev
        if ema10 is not None and ema10_prev is not None
        else None
    ),
}
```

This is especially important.

The project already has a canonical place where the exact values used by the rules are captured.

`SignalStateLog.record()` receives those measured fields and stores:

- `ema_10`
- `vwap`
- `ema_slope`

So the numerical values are already persisted in the signal log.

## 3.3 EMA slope is also already available

The current Trend rule calculates:

```python
slope = ind.ema10 - ind.ema10_prev
```

The same measured value is already captured by:

```python
"ema_slope": ...
```

Therefore we do **not** need to add an `ema_slope` calculation.

The important question is only whether Telegram should read the existing value directly or receive it through an existing structured object.

## 3.4 Decision does not carry IndicatorSnapshot directly

`decision_engine.py` defines:

```python
@dataclass
class Decision:
    ...
    signals: SignalSnapshot
    ...
    setup: Optional[str] = None
```

The `Decision` stores the `SignalSnapshot`, not the `IndicatorSnapshot`.

`SignalSnapshot` contains the evaluated states:

```text
trend
pullback
rejection
volume
open_interest
poc
vah
val
vwap_pullback
vwap_rejection
details
```

It does **not** contain structured `vwap`, `ema10`, or `ema_slope` fields.

So the precise finding is:

> The raw indicator values are available upstream and are already persisted in `SignalStateLog`, but they are **not currently fields on `Decision` / `SignalSnapshot`**.

That is different from saying the values are "gone."

## 3.5 The current Telegram alert is built from `reason_list`

`alert_engine.py` constructs:

```python
reason_list=[
    f"{k.replace('_', ' ').title()}: {v}"
    for k, v in decision.signals.details.items()
]
```

and stores:

```python
AlertRecord(
    ...
    current_price=current_price,
    setup=getattr(decision, "setup", None),
    reason_list=...
)
```

The Telegram renderer then simply joins those strings:

```python
text = (
    f"{record.grade} {record.direction}\n"
    f"Price: {record.current_price}\n"
    + "\n".join(record.reason_list)
)
```

This explains exactly why the current Telegram message contains:

```text
Trend: Close 57799.00 above VWAP 57734.39, slope +0.73 ...
```

The numeric VWAP/EMA information is embedded in the **reason string**, not stored as dedicated fields on `AlertRecord`.

## 3.6 The current alert therefore has a real structural limitation

For Telegram V2 we want:

```text
VWAP: 57734 ↑
EMA10: 57779 ↑
Slope: +0.73
```

But `AlertRecord` currently has no:

```python
vwap
ema10
ema_slope
```

fields.

The formatter cannot reliably obtain those values from `AlertRecord` except by parsing the human-readable `reason_list`, which would be brittle and is explicitly the wrong architecture.

### Therefore the original reviewer's conclusion needs correction

The correct statement is:

> **The values already exist upstream and are already recorded by `SignalStateLog`, but they are not currently carried into `AlertRecord`.**

We should **not** add them blindly to `SignalSnapshot`.

The cleanest V2 implementation should first decide where the alert formatter gets its display context. Because `session_runner.evaluate_signals()` already has both:

```text
decision
indicators
```

the preferred minimal design is to pass the already-computed display values into the alert path rather than recalculate them.

# 4. VWAP

Current alert already contains:

```text
VWAP 57734.39
```

and a slope value:

```text
slope +0.73
```

Therefore VWAP price and the existing slope are available.

The simplified representation can be:

```text
VWAP: 57734 ↑
Slope: +0.73
```

## Verification required

Before implementation, verify exactly what the existing `slope` field represents.

Do not assume that `slope` is VWAP slope merely because it appears beside VWAP.

The formatter should label the value according to the existing upstream meaning.

**No new slope calculation belongs in this change.**

---

# 5. Price vs VWAP

The current alert contains enough information to know whether price is above or below VWAP.

Example:

```text
Close 57799.00 above VWAP 57734.39
```

For Telegram, we do not need to print the full comparison.

The direction can simply be represented by:

```text
VWAP: 57734 ↑
```

If a numerical distance from VWAP is desired later, that would be a new derived display value:

```text
Distance: +64.6
```

It is intentionally **not required for V2**.

Keep V2 simple.

---

# 6. Volume

Current alert:

```text
Projected relative volume 1.31 >= 1.30
(3870 in 183s -> projected 6336)
```

The useful trader-facing information is:

```text
Volume: 1.31×
```

The following are diagnostic implementation details and should disappear from Telegram:

- threshold comparison
- raw current volume
- elapsed seconds
- projected volume

The underlying volume logic remains unchanged.

The architecture already treats Volume as an Indicator Engine input and a Signal/Decision input. fileciteturn23file0L20-L45

---

# 7. Open Interest

Current alert:

```text
OI 2115510 -> 2115150 (-0.02%)
```

For Telegram:

```text
OI: Falling -0.02%
```

The raw before/after values are unnecessary.

### Important boundary

The formatter may translate an existing OI change into simple wording such as:

- Rising
- Falling
- Flat

provided this is derived directly from the existing OI change.

It must not introduce a new OI trading interpretation.

For example, do not create new rules such as:

```text
OI Falling = bearish
```

unless that already exists in the decision logic.

---

# 8. Volume Profile

Current alert separately prints:

```text
Poc: Close 57799.00 above POC 57737.50
Vah: Close 57799.00 above VAH 57780.00
Val: Close 57799.00 above VAL 57690.00
```

This is redundant for Telegram.

The existing values are enough to derive a compact context:

```text
VP: Above VAH
```

or:

```text
VP: Inside Value
```

or:

```text
VP: Below VAL
```

### Example

Given:

```text
Price = 57799
VAH = 57780
VAL = 57690
```

Then:

```text
57799 > 57780
```

Therefore:

```text
VP: Above VAH
```

No new Volume Profile calculation is required.

The architecture already defines POC, VAH, VAL, HVN and LVN as Volume Profile outputs. fileciteturn23file0L20-L45

### V2 rule

Use existing POC/VAH/VAL values only to produce the compact display status.

Do not alter Volume Profile logic.

---

# 9. Setup

This is the most useful contextual field.

Instead of making the trader infer the setup from several gate explanations:

```text
Pullback: ...
Rejection: ...
VWAP Pullback: ...
VWAP Rejection: ...
```

show:

```text
Setup: EMA Pullback + Rejection
```

or:

```text
Setup: VWAP Pullback + Rejection
```

## Critical verification

Before implementation, verify that the existing `Decision` object already identifies the setup.

If `setup` already exists upstream:

> Display it directly.

If it does not exist:

> Do NOT infer it inside the Telegram formatter.

Adding setup classification would be decision logic and must be a separate change.

This preserves the architecture's separation between Signal/Decision logic and Alert formatting. fileciteturn23file0L20-L35

---

# 10. What Gets Removed

Remove these verbose Telegram sections:

### Trend explanation

```text
Trend: Close ... above VWAP ..., slope ... (holding, exits below ...)
```

Replace with compact fields:

```text
VWAP: ...
EMA10: ...
Slope: ...
```

### Pullback explanation

Remove:

```text
Pullback: Low ... <= EMA10 ... < Close ...
```

### Rejection explanation

Remove:

```text
Rejection: Lower wick + bullish candle + Close above EMA10
```

### Volume calculation

Remove:

```text
>= 1.30
(3870 in 183s -> projected 6336)
```

### Separate VP lines

Remove:

```text
Poc: ...
Vah: ...
Val: ...
```

Replace with:

```text
VP: Above VAH
```

### VWAP diagnostic lines

Remove:

```text
Vwap Pullback: ...
Vwap Rejection: ...
```

The setup field replaces the need to expose these diagnostics in Telegram.

---

# 11. What Must NOT Change

This is a **formatter-only refactor**.

Do not change:

- Decision Engine
- Signal Engine
- Gate conditions
- Gate thresholds
- Grades
- Latch
- Cooldown
- Alert triggering
- Setup qualification
- VWAP calculation
- EMA10 calculation
- Volume calculation
- OI calculation
- Volume Profile calculation
- Session Bias
- CSV output
- Replay output

The Alert Engine's responsibility remains notification delivery only. fileciteturn23file2L20-L45

---

# 12. Source-of-Truth Principle

There must be **one decision source**.

Telegram should not recreate:

```text
Trend
Pullback
Rejection
VWAP Pullback
VWAP Rejection
```

from raw values.

Instead:

```text
Indicator Engine
      ↓
Signal Engine
      ↓
Decision Engine
      ↓
Decision / existing fields
      ↓
Telegram Formatter
```

The formatter only converts already-existing state into trader-readable text.

This follows the project's explicit coding principle of one responsibility per module and avoiding duplicated logic. fileciteturn23file0L45-L60

---

# 13. Direct vs Derived vs Missing — Verified

| Field | Verified source in V2 | Telegram V2 action |
|---|---|---|
| Grade | `Decision` / `AlertRecord` | Display |
| Direction | `Decision` / `AlertRecord` | Display |
| Price | `AlertRecord.current_price` | Display |
| VWAP | `IndicatorSnapshot.vwap`; also `measured_fields()` | Pass existing value to formatter |
| EMA10 | `IndicatorSnapshot.ema10`; also `measured_fields()` | Pass existing value to formatter |
| EMA slope | `IndicatorSnapshot.ema10 - ema10_prev`; also `measured_fields()` | Pass existing value to formatter |
| Relative Volume | Existing Signal/Decision details | Display existing value |
| OI change | Existing Signal/Decision details / OI inputs | Display existing value |
| VP status | Existing POC/VAH/VAL state | Display existing state |
| Setup | `Decision.setup` | Display directly |
| Pullback explanation | `SignalSnapshot.details` | Remove from Telegram |
| Rejection explanation | `SignalSnapshot.details` | Remove from Telegram |
| VWAP Pullback details | `SignalSnapshot.details` | Remove from Telegram |
| VWAP Rejection details | `SignalSnapshot.details` | Remove from Telegram |

## Key architectural finding

There are three different layers:

```text
IndicatorSnapshot
    ↓
SignalSnapshot
    ↓
Decision
    ↓
AlertRecord
    ↓
Telegram
```

The current gap is specifically between:

```text
IndicatorSnapshot → AlertRecord
```

for the raw numeric display values.

It is **not** an indicator-calculation gap.

# 14. Proposed Formatter Contract

Conceptually, the formatter should consume something like:

```python
format_telegram_alert(
    decision=decision,
    snapshot=snapshot,
    ...
)
```

and produce only presentation.

It should **not** contain logic equivalent to:

```python
if price > vwap:
    ...
```

for deciding a trade direction.

It may perform harmless presentation transformations such as:

```python
format_price(...)
format_percent(...)
format_direction(...)
```

and the explicitly approved VP status derivation.

---

# 15. Implementation Plan — Verified Minimal Change

## Step 1 — Do not change indicator logic

No changes to:

- VWAP calculation
- EMA10 calculation
- EMA slope calculation
- Trend rule
- Pullback
- Rejection
- Volume
- OI
- Volume Profile
- Decision gates
- Grade
- Setup selection
- Latch

All of these already work.

## Step 2 — Reuse existing indicator values

`session_runner.evaluate_signals()` already has:

```python
indicators
decision
```

at the exact point where the alert is generated.

Use the existing:

```python
indicators.vwap
indicators.ema10
indicators.ema10_prev
```

rather than recalculating anything.

For slope, reuse the same existing calculation/representation already used by the engine. Do not create a second slope rule.

## Step 3 — Decide the smallest transport change

The current `AlertRecord` contains:

```python
current_price
setup
reason_list
```

but not raw VWAP/EMA values.

Add only the minimum display context required by Telegram, preferably to the alert-record/formatter boundary rather than to `SignalSnapshot`.

Candidate fields:

```python
vwap: Optional[float] = None
ema10: Optional[float] = None
ema_slope: Optional[float] = None
```

These are **transport/display fields**, not decision fields.

They must be populated from the already-existing `IndicatorSnapshot`.

## Step 4 — Keep `SignalSnapshot` unchanged

Do **not** add:

```python
vwap
ema10
ema_slope
```

to `SignalSnapshot` merely to make Telegram easier.

`SignalSnapshot` represents evaluated signals; `IndicatorSnapshot` already represents raw indicator values.

Preserve that separation.

## Step 5 — Keep `AlertRecord.reason_list` for existing CSV compatibility

The current CSV schema contains:

```text
reason_list
```

and the project already uses it for historical analysis.

Do not remove or change it in this formatter-only commit.

Telegram simply stops rendering the verbose `reason_list` as its primary message body.

## Step 6 — Build the compact formatter

The formatter should receive:

```text
AlertRecord
+ existing display context
```

and produce:

```text
🟢 A LONG

Price: 57799

Setup: EMA Pullback + Rejection

VWAP: 57734 ↑
EMA10: 57779 ↑
Slope: +0.73
Volume: 1.31×
OI: Falling -0.02%
VP: Above VAH
```

No decision logic is introduced.

## Step 7 — VP status

Use the existing POC/VAH/VAL information.

For V2:

```text
Price > VAH  → Above VAH
VAL < Price < VAH → Inside Value
Price < VAL → Below VAL
```

This is a display classification from already-available values, not a new trading gate.

If the project already has a suitable VP position field, use that directly instead.

# 16. Handling Missing Values

The formatter should fail gracefully.

Examples:

```text
EMA10: —
```

or omit the field if the value genuinely does not exist.

Do **not** calculate a replacement value inside Telegram.

Likewise:

```text
Setup: —
```

if setup is not available.

This prevents the presentation layer from silently becoming a second indicator engine.

---

# 17. Tests

## Required tests

### Test 1 — Long EMA setup

Input existing alert state:

```text
A Long
EMA Pullback = true
Rejection = true
```

Expected:

```text
Setup: EMA Pullback + Rejection
```

### Test 2 — Long VWAP setup

Input:

```text
A Long
EMA Pullback = false
VWAP Pullback = true
VWAP Rejection = true
```

Expected:

```text
Setup: VWAP Pullback + Rejection
```

### Test 3 — EMA10 always displayed

A VWAP setup where EMA pullback is false should still display:

```text
EMA10: <existing EMA10>
```

This is specifically important because the current formatter conditionally exposes EMA10 only inside the verbose pullback explanation.

### Test 4 — VP

Given:

```text
Price > VAH
```

Expected:

```text
VP: Above VAH
```

Given:

```text
VAL < Price < VAH
```

Expected:

```text
VP: Inside Value
```

Given:

```text
Price < VAL
```

Expected:

```text
VP: Below VAL
```

### Test 5 — No decision change

For the same input decision:

```text
direction
grade
gates
setup
```

must remain identical before and after the formatter change.

### Test 6 — CSV/replay unchanged

The formatter refactor must not alter:

- `alert_log.csv`
- replay alert output
- near-miss output

---

# 18. Smoke Test

After implementation:

1. Run the existing test suite.
2. Generate one existing Long alert.
3. Generate one Short alert.
4. Generate one EMA setup.
5. Generate one VWAP setup.
6. Confirm EMA10 appears in both.
7. Confirm VP status is correct.
8. Confirm Telegram is materially shorter.
9. Confirm alert count is unchanged.
10. Confirm CSV/replay output is unchanged.

---

# 19. Acceptance Criteria

The change is accepted only if:

- Telegram becomes materially shorter.
- EMA10 is displayed even when the EMA pullback gate is false.
- VWAP is displayed from the existing `IndicatorSnapshot` value.
- EMA10 is displayed from the existing `IndicatorSnapshot` value.
- Slope uses the existing slope meaning/value.
- Setup comes directly from `Decision.setup`.
- VP status uses existing VP data only.
- No indicator is recalculated in Telegram.
- No Decision/Gate/Grade/Latch behaviour changes.
- No alert timing changes.
- No CSV schema/output changes unless explicitly approved separately.
- No replay decision/output changes.

## Additional regression test

Add a formatter test proving:

```text
IndicatorSnapshot.vwap = X
IndicatorSnapshot.ema10 = Y
```

produces:

```text
VWAP: X
EMA10: Y
```

even when:

```text
pullback = None
```

This specifically prevents the old problem where EMA10 only appeared because it was embedded inside the verbose Pullback reason.

# 20. Final Design

The desired relationship is:

```text
                 EXISTING ENGINE
                       │
       ┌───────────────┼────────────────┐
       │               │                │
   Decision         Indicators       VP values
       │               │                │
       └───────────────┼────────────────┘
                       ↓
                Telegram Formatter
                       ↓
              SHORT TRADER ALERT
```

The formatter is a **view**, not another decision engine.

## Final message

```text
🟢 A LONG

Price: 57799

Setup: EMA Pullback + Rejection

VWAP: 57734 ↑
EMA10: 57779 ↑
Slope: +0.73
Volume: 1.31×
OI: Falling -0.02%
VP: Above VAH
```

This keeps the information that matters for **fast manual interpretation** while removing the implementation diagnostics that belong in logs/debug output.

---

# Decision

**Recommendation: APPROVE — with one small transport change.**

The backup verification changes our earlier implementation conclusion:

- VWAP is already calculated and available.
- EMA10 is already calculated and available.
- EMA slope is already calculated and already captured by `measured_fields()`.
- Setup already exists on `Decision`.
- VP states already exist upstream.
- The current Telegram formatter is the problem: it renders the verbose `reason_list`.
- The raw VWAP/EMA values are not fields on `AlertRecord`, so Telegram should not parse the reason strings.

Therefore:

> **Do not add indicator calculations. Do not add raw indicators to `SignalSnapshot`. Add only the minimum transport/display fields at the AlertRecord/formatter boundary, sourced from the existing `IndicatorSnapshot`.**

That keeps this a small, low-risk presentation refactor while preserving the architecture's separation between indicators, signals, decisions, and alerts.
