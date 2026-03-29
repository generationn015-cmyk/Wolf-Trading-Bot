# FUTURE_CAPABILITIES.md

## Purpose
Track capabilities that may be valuable later but should not be installed prematurely.

## Rule
Future interest is not permission to install.
If it is not clean, necessary, and foundation-safe right now — it waits here.

---

## Capabilities Queue

### Live Execution Adapter
- **What:** Direct broker/exchange API connection for real order placement
- **Status:** Future — requires completed risk engine, backtesting validation, paper trading results, and explicit Jefe authorization
- **Target:** Alpaca (stocks), Binance/Coinbase (crypto), or IBKR depending on market target decision

### Vector Memory / Semantic Search
- **What:** Embedding-based memory search across trade history, research, and decisions
- **Status:** Future — useful once trade history volume justifies it; file-based memory first
- **Notes:** Requires embedding API; evaluate cost vs value when history grows

### Real-Time Market Data Feed
- **What:** WebSocket or streaming price/volume feeds for live signal generation
- **Status:** Future — needed for live trading phase; not needed during research/backtest/paper phases
- **Target:** Polygon.io, Alpaca data, or exchange websockets depending on market target

### Sentiment Analysis Pipeline
- **What:** Automated news/social sentiment scoring as a trading signal layer
- **Status:** Future — useful addition after core strategy is validated; don't build on sentiment alone
- **Notes:** Perplexity + Claude can cover on-demand sentiment now; automated pipeline is Phase 3+

### Portfolio Dashboard
- **What:** Visual dashboard for trade tracking, P&L, positions, risk metrics
- **Status:** Future — useful during paper trading phase and beyond
- **Notes:** Can be lightweight; don't over-engineer before there's data to show

---

## Standard
Build the core right first. Capabilities get added when the foundation justifies them, not before.
