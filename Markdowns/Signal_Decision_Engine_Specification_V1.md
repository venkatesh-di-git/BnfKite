
# BN Smart Assistant
# Signal & Decision Engine Specification (Version 1)

## Purpose

This document defines the implementation of the Signal Engine and Decision Engine.

The Signal Engine converts indicator values into standardized market signals.

The Decision Engine consumes those signals and determines whether a trade setup is worth alerting.

---

# Architecture

Indicator Engine
↓
Signal Engine
↓
Decision Engine
↓
Alert Engine

---

# Signal Engine

## Responsibilities

- Receive indicator values
- Evaluate market conditions
- Produce standardized signals
- Never generate alerts
- Never access Kite APIs
- Never calculate indicators

## Inputs

### Candle
- Open
- High
- Low
- Close
- Volume
- Timestamp

### Indicator Snapshot
- VWAP
- EMA10
- SMA20 Volume
- Current OI
- Previous OI
- POC
- VAH
- VAL

## Output

SignalSnapshot containing:

- Trend
- Pullback
- Rejection
- Volume
- OpenInterest
- POC
- VAH
- VAL

## SignalEngine API

Public:

- evaluate_all()

Private:

- evaluate_trend()
- evaluate_pullback()
- evaluate_rejection()
- evaluate_volume()
- evaluate_open_interest()
- evaluate_volume_profile()

## Rules

### Trend

Bullish
- Close > VWAP
- EMA10 > VWAP
- EMA slope > 0

Bearish
- Close < VWAP
- EMA10 < VWAP
- EMA slope < 0

Else Neutral.

### Pullback

Bull:
- Low <= EMA10
- Close > EMA10

Bear:
- High >= EMA10
- Close < EMA10

### Rejection

Bull:
- Lower wick
- Bullish candle
- Close > EMA10

Bear:
- Upper wick
- Bearish candle
- Close < EMA10

### Volume

Relative Volume = Current Volume / SMA20 Volume

- High >= 1.30
- Normal 0.80–1.30
- Low < 0.80

### Open Interest

Return:
- Rising
- Falling
- Flat

### Volume Profile

Return:

POC:
- Above
- Below
- At

VAH:
- Above
- Below
- Rejected

VAL:
- Above
- Below
- Rejected

---

# Decision Engine

## Purpose

The Decision Engine evaluates the complete SignalSnapshot.

It does not calculate indicators.

It only decides whether the current setup qualifies for an alert.

## Inputs

SignalSnapshot

## Outputs

Direction:
- Long
- Short
- Neutral

Grade:
- A+
- A
- B
- Ignore

Status:
- ENTRY
- WAIT

## Decision Flow

1. Trend
2. Pullback
3. Rejection
4. Volume
5. Open Interest
6. Volume Profile

If any mandatory condition fails:

WAIT

If all mandatory conditions pass:

ENTRY

## Suggested Logic

Long:

- Trend = Bullish
- Pullback = Valid
- Rejection = Bullish
- Volume = High or Normal
- OI = Rising or Flat
- Price not rejected by VAH

Short:

- Trend = Bearish
- Pullback = Valid
- Rejection = Bearish
- Volume = High or Normal
- OI = Rising or Flat
- Price not rejected by VAL

Otherwise:

WAIT

## Grades

A+
- All conditions ideal

A
- Minor weakness

B
- Tradable but lower confidence

Ignore
- Do not alert

---

# Error Handling

- Validate all inputs
- Return UNKNOWN where data is insufficient
- Never crash
- Log every evaluation

---

# Unit Tests

Test:
- Trend
- Pullback
- Rejection
- Volume
- OI
- Volume Profile
- Long decision
- Short decision
- WAIT decision

---

# Definition of Done

- Signal Engine returns complete SignalSnapshot.
- Decision Engine consumes SignalSnapshot only.
- No indicator calculations inside Decision Engine.
- No alert generation inside either engine.
- Both modules are independently testable.
