# BN Smart Assistant - Architecture Context (Version 1)

## Project Goal

Build a professional Bank Nifty Futures decision-support system.

This is **NOT** an auto-trading bot.

The purpose of the application is to identify high-quality institutional
trading setups and generate alerts.

------------------------------------------------------------------------

# Core Philosophy

The system answers only one question:

> "Is this currently a high-quality trading opportunity?"

Everything in the application exists to improve that answer.

------------------------------------------------------------------------

# Broker

Version 1 supports **Kite Connect only**.

Do not build broker abstraction layers.

Keep the code clean, modular and optimized specifically for Kite
Connect.

------------------------------------------------------------------------

# Timeframe

Primary trading timeframe:

**5 Minute**

All calculations should match the 5-minute chart.

------------------------------------------------------------------------

# Trading Instrument

Current Month Bank Nifty Futures

------------------------------------------------------------------------

# Data Sources

Historical Data: - Kite Historical API

Live Data: - Kite WebSocket (MODE_FULL)

Do not use REST polling once WebSocket implementation is complete.

------------------------------------------------------------------------

# Data Flow

Kite Connect

↓

Historical Loader

-   

Live WebSocket

↓

Market Data Layer

↓

5 Minute Candle Builder

↓

Indicator Engine

↓

Signal Engine

↓

Decision Engine

↓

Alert Engine

↓

NiceGUI Dashboard

Telegram

------------------------------------------------------------------------

# Indicator Engine

Pure mathematical calculations only.

No BUY or SELL decisions.

Responsibilities

-   VWAP
-   EMA-10
-   Volume
-   Open Interest
-   Volume Profile
-   POC
-   VAH
-   VAL
-   HVN
-   LVN

Output

Calculated indicator values only.

------------------------------------------------------------------------

# Signal Engine

Transforms indicators into market signals.

Examples

-   Price Above VWAP
-   Price Below VWAP
-   EMA Pullback
-   POC Support
-   VAH Rejection
-   High Volume
-   OI Increasing

Output

Bullish/Bearish/Neutral signals.

No trade decisions.

------------------------------------------------------------------------

# Decision Engine

Combines all signals.

Produces

-   Long
-   Short
-   Neutral

Grades

-   A+
-   A
-   B
-   Ignore

Responsible for scoring and confidence.

------------------------------------------------------------------------

# Alert Engine

Responsible only for notifications.

Outputs

-   Dashboard Alert
-   Telegram Alert
-   Sound Alert

Should never calculate indicators.

------------------------------------------------------------------------

# Dashboard

NiceGUI

Display only.

Must never contain trading logic.

Show

-   Live Price
-   Current Candle
-   VWAP
-   EMA-10
-   Volume
-   Open Interest
-   Volume Profile
-   Current Signal
-   Current Grade
-   Alert History
-   Logs
-   Connection Status

------------------------------------------------------------------------

# Indicator Definitions

VWAP

-   Session VWAP
-   Reset every trading day
-   Typical Price = (High + Low + Close) / 3
-   Volume weighted

EMA

-   EMA-10
-   Source = 5-minute Close

Volume Profile

-   POC
-   VAH
-   VAL
-   HVN
-   LVN

------------------------------------------------------------------------

# Coding Principles

-   One responsibility per module.
-   No duplicated logic.
-   Modular architecture.
-   Deterministic calculations.
-   Extensive logging.
-   Easy debugging.
-   Clear separation between calculation and decision making.

------------------------------------------------------------------------

# Project Structure

Market Data

↓

Candle Builder

↓

Indicator Engine

↓

Signal Engine

↓

Decision Engine

↓

Alert Engine

↓

Dashboard

------------------------------------------------------------------------

# Version 1 Scope

Included

-   Kite Login
-   WebSocket
-   Historical Data
-   5 Minute Candle Builder
-   VWAP
-   EMA-10
-   Volume
-   Open Interest
-   Volume Profile
-   Signal Engine
-   Decision Engine
-   Alert Engine
-   NiceGUI Dashboard
-   Telegram Alerts

------------------------------------------------------------------------

# Explicitly Excluded (Version 2)

-   Market Structure
-   Backtesting
-   Strategy Optimization
-   ATR
-   ADX
-   Option Greeks
-   Option Chain Analysis
-   Multi-Timeframe Analysis
-   Auto Trading
-   AI Trade Journal

These features should not be implemented in Version 1.

------------------------------------------------------------------------

# Development Rule

Implement one phase completely before starting the next.

Each module must be independently testable.

Do not proceed to the next phase until the current phase is validated.
