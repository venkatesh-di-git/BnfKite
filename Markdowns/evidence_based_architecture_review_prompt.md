# Evidence-Based Architecture Review Prompt (Version 1)

## Objective

You are a senior software architect specializing in professional trading platforms and low-latency event-driven systems.

I am building a Python-based Bank Nifty Futures Decision Support System (NOT an auto-trading bot).

Do **NOT** review based on personal opinion or generic software best practices alone.

I want you to compare my implementation against **real production-grade architectures** and mature open-source trading systems.

## Reference Projects

Compare against relevant projects such as:

- HyperTrade
- NautilusTrader
- Lean (QuantConnect)
- Backtrader
- Freqtrade
- Hummingbot
- VN.py
- QSTrader
- Any other mature event-driven trading framework

---

# Review Rules

For every recommendation:

1. Verify whether it follows common industry practice.
2. Mention which project(s) implement a similar idea.
3. Explain **why** they implemented it that way.
4. If my implementation is actually better or simpler for Version 1, explicitly disagree and explain why.
5. Avoid overengineering. This is Version 1.

---

# Review ONLY These Five Improvements

## 1. Headless Pipeline

- Signal → Decision → Alert should execute independently of the UI.
- Dashboard should only display state.

---

## 2. Alert State Machine

- One alert per trading setup.
- Compare setup lifecycle/state machine versus simple cooldown logic.

---

## 3. Thread-safe Market Snapshot

- Freeze/copy candle and indicator state before Signal Engine evaluation.
- Assess whether immutable snapshots are necessary.

---

## 4. EMA Slope from Closed Bars

- Trend should be calculated from completed candles instead of the currently forming candle.
- Compare with how mature platforms calculate trend.

---

## 5. Non-blocking Alert Delivery

- Telegram notifications must never block the market data processing pipeline.
- Compare async queues, worker threads, or event-driven notification services.

---

# For EACH Item Provide

## Recommendation

- Implement
- Implement Later
- Reject

## Industry Comparison

Explain how mature trading systems solve the same problem.

Mention project/framework names wherever possible.

## Critique

Do you agree?

If not, explain why.

If a simpler Version 1 implementation exists, propose it.

## Risks

What happens if this change is NOT implemented?

## Complexity

- Low
- Medium
- High

## Version Suitability

- Version 1
- Version 2
- Future

---

# Final Deliverable

Create a Markdown document named:

`implementation_plan_v1.md`

Include:

- Executive Summary
- Final Recommendations
- Priority Order
- Rationale
- Implementation Strategy
- Estimated Implementation Effort
- Expected Benefits
- Explicitly Rejected Recommendations (with reasons)

---

# Important

Be critical.

Do not recommend changes simply because they are considered "best practice."

Recommend them only if they provide measurable improvements in:

- Reliability
- Correctness
- Maintainability
- Performance

for **this specific project**.
