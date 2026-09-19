# BN Smart Assistant
## Alert Engine & Dashboard Specification (Version 1)

**Version:** 1.0

---

# Purpose

This document defines the responsibilities of the **Alert Engine** and **Dashboard**.

The **Signal Engine** and **Decision Engine** are already complete.

These modules only consume the output of the Decision Engine.

**No trading logic belongs here.**

---

# Architecture

```text
Decision Engine
        │
        ▼
   Alert Engine
     ├────────► Dashboard
     └────────► Telegram
```

The Alert Engine receives a completed decision and distributes it to all output channels.

---

# Design Principles

## Alert Engine must NEVER

- Calculate indicators
- Generate signals
- Make trading decisions
- Modify Decision Engine output

## Dashboard must NEVER

- Calculate indicators
- Generate signals
- Make trading decisions

Both modules are display and notification layers only.

---

# Alert Engine Responsibilities

## Input

- DecisionSnapshot

## Responsibilities

- Create an AlertRecord
- Store Alert History
- Update Dashboard
- Send Telegram notification

The Alert Engine should not contain any trading logic.

---

# Alert Lifecycle

```text
Decision Engine
        │
        ▼
DecisionSnapshot
        │
        ▼
Alert Engine
        │
        ▼
Create AlertRecord
        │
        ▼
Store Alert History
      ├────────► Dashboard
      └────────► Telegram
```

Every trading decision becomes an immutable AlertRecord.

---

# Alert Types

## Trading Alert

- A+ Long
- A Long
- B Long
- A+ Short
- A Short
- B Short

## Error Alert

- WebSocket disconnected
- Historical data unavailable
- Unexpected application error

---

# AlertRecord

Fields

- id
- timestamp
- type
- direction
- grade
- confidence
- current_price
- reason_list

---

# Duplicate Alert Rule

Generate an alert only when the Decision Engine changes state.

Example:

WAIT → A+ ✔

A+ → A+ ✘

WAIT → A+ ✔

---

# Dashboard

Display only.

No calculations.

## Sections

### System Status
- Connection Status
- Market Status
- Current Contract

### Market
- Live Price
- Current 5-Minute Candle
- VWAP
- EMA-10
- Current Volume
- Open Interest

### Volume Profile
- POC
- VAH
- VAL
- HVN
- LVN

### Signals
Display Signal Engine output only.

### Decision
- Direction
- Grade
- Confidence
- Reason Summary

### Current Alert
- Time
- Direction
- Grade
- Price
- Reason Summary

### Alert History
- Time
- Direction
- Grade
- Price
- Confidence

Newest first.

---

# Dashboard Update Rules

- Live Price: Every tick
- Current Candle: Every tick
- Indicators: When Indicator Engine updates
- Signals: When Signal Engine updates
- Decision: When Decision Engine updates
- Current Alert: On new alert
- Alert History: Append on new alert

---

# Acceptance Criteria

## Alert Engine

- Receives DecisionSnapshot
- Creates AlertRecord
- Stores Alert History
- Updates Dashboard
- Sends Telegram notification
- Prevents duplicate alerts

## Dashboard

- Displays live market information
- Displays indicator values
- Displays signal states
- Displays decision output
- Displays latest alert
- Displays alert history
- Contains no trading logic

---

# Final Architecture

```text
Kite Connect
        │
        ▼
Market Data Layer
        │
        ▼
5-Minute Candle Builder
        │
        ▼
Indicator Engine
        │
        ▼
Signal Engine
        │
        ▼
Decision Engine
        │
        ▼
Alert Engine
      ├────────► Dashboard
      └────────► Telegram
```

# Final Rule

- Indicator Engine calculates.
- Signal Engine interprets.
- Decision Engine decides.
- Alert Engine distributes.
- Dashboard displays.
