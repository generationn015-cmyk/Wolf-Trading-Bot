# wolf-trading.md — Project Memory

## Objective
Build Wolf into a disciplined, multi-layer trading system starting with Polymarket latency arbitrage, expanding to multi-market coverage.

## Current Phase
**Phase 1 — Strategy Research** (active)
- Reviewed: adiix article (latency arb blueprint)
- Reviewed: The Prediction Engineer YouTube channel (vibe coding + CLOB API)
- Reviewed: Multi-LLM consensus approach (Polymarket Copilot pattern)
- Reviewed: Polymarket market structure, fee dynamics, liquidity patterns

## Confirmed Architecture

### Wolf Engine Layers

**Layer 1 — Whale Tracker (monitoring)**
- Watch top Polymarket wallets in real time
- Flask + SQLite + Python fetcher (60s interval)
- Telegram alerts on large trades or significant market moves
- Builds a copy-signal intelligence feed

**Layer 2 — Latency Arb Engine (primary strategy)**
- Monitor Binance WebSocket (<50ms) for BTC/ETH price moves
- Detect Polymarket lag >0.3% from real price
- Execute via Polymarket CLOB API in <100ms
- Target: 200+ paper trades, 80%+ win rate before live
- Win rate ceiling: 85–98% when functioning

**Layer 3 — News Sniper (semi-automated)**
- Monitor news feeds (Perplexity, Brave, X)
- When catalyst detected: open terminal sniper for fast manual or semi-auto entry
- Inspired by The Prediction Engineer's sniper bot (283 lines, asyncio)
- Claude does the news parsing + probability update

**Layer 4 — Multi-LLM Consensus (research layer)**
- Claude primary analyst
- Gemini 2.5 Pro challenger / second opinion
- Perplexity for real-time web grounding
- Synthesize consensus before taking news-driven positions

**Layer 5 — Market Making (Phase 3)**
- Bid-ask spread capture on high-liquidity markets
- Strict inventory limits
- 2–5% monthly return baseline

### Risk Engine (hard rules — no exceptions)
- Max single position: 8% of portfolio
- Daily loss cap: -20% with auto halt
- Kill switch: -40% drawdown
- Max open positions: 2–3 simultaneous
- Liquidity filter: >$50K market volume only
- Kelly Criterion for position sizing
- Telegram alert on every threshold breach

## Key Decisions
- Start Polymarket only, prove the system, then expand markets
- Paper mode gate: 200+ trades at 80%+ win rate before live
- Rule-based risk engine is built BEFORE execution is connected
- No live execution without explicit Jefe authorization
- Trade journal required: every signal, trade, and result logged

## Market Insights (from research)
- Polymarket lag: ~2.7 seconds in Q1 2026 (was 12s in 2024) — window narrowing
- 70% of Polymarket traders lose money; top 0.04% capture most gains
- Bot advantage over humans: execution speed, consistent sizing, no fatigue, no drawdown panic
- NASDAQ entering prediction market space — sector going mainstream
- Polymarket added taker fees — affects certain strategies; edge still exists on latency arb
- Oracle arb (Chainlink divergence) = 78–85% win rate, less frequent but higher certainty
- US restriction on Polymarket: Jefe has existing account — cleared to proceed
- Kalshi = US-regulated alternative worth adding in Phase 2

## API Credentials Needed (store on VPS, NOT in chat)
```
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
POLYMARKET_WALLET_ADDRESS=
POLYMARKET_PRIVATE_KEY=
```

## Tech Stack
- Language: Python (asyncio for execution speed)
- Exchange interface: Polymarket CLOB API + py-clob-client (official Python SDK)
- Price feed: Binance WebSocket
- DB: SQLite (local trade log + whale tracker)
- Web layer: Flask (local dashboard)
- Alerts: Telegram (already connected)
- Hosting: Hostinger VPS (Docker container)
- Backups: GitHub (Wolf-Trading-Bot repo, auto-backup via heartbeat)

## Open Questions (pending Jefe input)
1. Polymarket API credentials — Jefe generating now
2. Starting capital for live phase — TBD
3. Kalshi setup (US-legal venue) — Phase 2
4. Lighter.xyz — need to research for US-legal execution

## Next Build Steps
1. [ ] Jefe generates Polymarket CLOB API credentials → stores in .env
2. [ ] Build Wolf engine scaffold (Python, CLOB API wrapper, Binance WebSocket)
3. [ ] Build whale tracker (monitoring layer)
4. [ ] Build paper mode (no live orders, logs simulated trades)
5. [ ] Run 200+ paper trades, validate 80%+ win rate
6. [ ] Build risk engine
7. [ ] Connect kill switch + Telegram alerts
8. [ ] Phase 1 live: start with $1–5 USDC per trade

---
Last updated: 2026-03-29
