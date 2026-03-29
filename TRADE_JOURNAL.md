# TRADE_JOURNAL.md

## Purpose
Log every signal, paper trade, backtest result, and live trade.
This becomes the most valuable asset in the system over time.

## Why This Matters
- Separates what actually works from what sounds good
- Catches when signals start drifting toward noise
- Builds an honest track record before live money
- Forces discipline: if you can't explain a trade, you shouldn't be in it

## Log Format

### Signal Entry
```
Date: YYYY-MM-DD HH:MM
Instrument: [ticker/pair]
Timeframe: [1m/5m/15m/1h/4h/1D]
Signal Type: [momentum/breakout/reversal/news-driven/etc]
Thesis: [Why this trade exists — 2-3 sentences max]
Confidence: [Low/Medium/High]
Key Levels: [Support/Resistance/Entry zone]
Invalidation: [What would make this thesis wrong]
Status: [Watching/Paper Entry/Closed]
```

### Trade Entry
```
Date: YYYY-MM-DD HH:MM
Instrument: [ticker/pair]
Direction: [Long/Short]
Entry Price: 
Stop Loss: 
Take Profit 1: 
Take Profit 2: (optional)
Position Size: [paper or real + size]
Risk %: [% of portfolio at risk]
Thesis: [Why — brief]
```

### Trade Close
```
Date Closed: YYYY-MM-DD HH:MM
Exit Price: 
Result: [Win/Loss/Breakeven]
P&L: 
Hold Time: 
What worked: 
What didn't: 
Lesson: 
```

## Backtest Entry
```
Date: YYYY-MM-DD
Strategy: 
Instrument(s): 
Timeframe: 
Period Tested: [start date → end date]
Total Trades: 
Win Rate: 
Avg Win: 
Avg Loss: 
Expectancy: 
Max Drawdown: 
Sharpe (or notes): 
Key Finding: 
Verdict: [Viable / Needs work / Reject]
```

## Review Schedule
- Weekly: review all paper trades, note patterns
- Monthly: review backtests and strategy performance
- Phase transition: full review before going from paper to live

## Standard
If it isn't logged, it didn't happen.
Gut feeling is not a thesis. Write it down or don't take it.
