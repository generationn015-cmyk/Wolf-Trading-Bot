# 🐺 Wolf Trading Bot

Disciplined, multi-strategy trading system for Polymarket and Kalshi.
Built by Wolf for Jefe. Paper mode first — always.

---

## Phase Roadmap

### Phase 1 — Paper Validation (NOW)
- Latency arb: BTC/ETH 15-min markets on Polymarket
- Copy trading: top wallets, any market category
- Market making: BTC/ETH + Fed/macro markets with VPIN protection
- **Gate: 200+ paper trades at 80%+ win rate before any live money**

### Phase 2 — Go Live + Expand
- Pass paper gate → Jefe authorizes live mode
- Start: $1-5 per trade, watch every trade for first week
- Add: Kalshi venue (Fed rate decisions, economic indicators, sports)
- Add: sports markets + injury intelligence layer

### Phase 3 — News-Driven + Multi-LLM Consensus
- Perplexity + Brave monitoring for breaking catalysts
- Claude primary + Gemini challenger for news-driven positions
- Synthesized verdict before any news-driven trade

### Phase 4 — Full Multi-Market
- Traditional markets: stocks, forex, futures
- Broker/exchange integrations (Alpaca, etc.)
- Portfolio-level risk management across all venues

---

## Setup

### 1. Install dependencies
```bash
cd /data/.openclaw/workspace/wolf
pip install py-clob-client websockets aiohttp flask requests python-dotenv
```

### 2. Add credentials to `~/.openclaw/.env`
```bash
# Polymarket
POLYMARKET_PRIVATE_KEY=your_private_key_here
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_secret
POLYMARKET_API_PASSPHRASE=your_passphrase
POLYMARKET_WALLET_ADDRESS=your_wallet_address

# Kalshi (Phase 2)
KALSHI_API_KEY_ID=your_kalshi_api_key_id
KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi_private.pem

# Telegram (already set)
TELEGRAM_BOT_TOKEN=already_configured
TELEGRAM_CHAT_ID=already_configured
```

### 3. Generate Polymarket API credentials
```bash
python3 /data/.openclaw/workspace/scripts/setup_polymarket_keys.py
```

### 4. Run Wolf in paper mode
```bash
cd /data/.openclaw/workspace/wolf
python3 main.py
```

### 5. View dashboard
Open in browser: http://127.0.0.1:5000

---

## Risk Parameters (all configurable via env)

| Parameter | Default | Description |
|---|---|---|
| MAX_POSITION_PCT | 8% | Max single position as % of balance |
| DAILY_LOSS_LIMIT | -20% | Auto halt when daily loss hits this |
| KILL_SWITCH_THRESHOLD | -40% | Full stop, all trading halted |
| MAX_OPEN_POSITIONS | 3 | Max simultaneous open positions |
| MIN_MARKET_VOLUME | $50,000 | Liquidity filter |
| MIN_CONFIDENCE | 0.65 | Fee-aware entry threshold |
| LATENCY_ARB_THRESHOLD | 0.3% | Divergence required to trigger arb |
| VPIN_SPIKE_THRESHOLD | 0.15 | Informed money detection level |

---

## Paper Mode Gate

Wolf will NOT go live until:
1. At least 200 paper trades completed
2. Win rate ≥ 80%
3. Jefe explicitly sets `WOLF_PAPER_MODE=false`

When the gate is passed, Wolf sends a Telegram alert to Jefe.
Jefe must manually set the env variable to authorize live trading.

---

## Going Live (Phase 2)

After paper gate is passed:
1. Review paper trade journal — confirm strategy is working
2. Set in `~/.openclaw/.env`: `WOLF_PAPER_MODE=false`
3. Set starting trade size: `WOLF_LIVE_STARTING_SIZE=2` (start at $2/trade)
4. Restart Wolf: `python3 main.py`
5. Watch first 10 live trades manually
6. Scale only when live results match paper results

---

## File Structure

```
wolf/
├── main.py              ← Start here
├── config.py            ← All settings
├── risk_engine.py       ← Hard risk rules (Kelly, kill switch)
├── paper_mode.py        ← Paper trading simulation + gate
├── strategies/
│   ├── latency_arb.py   ← Latency arbitrage
│   ├── copy_trading.py  ← Wallet copy with demo validation
│   └── market_making.py ← Both-sides + VPIN detection
├── feeds/
│   ├── binance_feed.py  ← Real-time BTC/ETH price (no API key needed)
│   └── polymarket_feed.py ← Market data + wallet intelligence
├── execution/
│   └── order_manager.py ← Routes paper/live, logs everything
├── monitoring/
│   ├── health_check.py  ← Dead Man's Switch + heartbeat
│   └── whale_tracker.py ← Large trade alerts
├── alerts/
│   └── telegram_alerts.py ← All alerts to Jefe
├── journal/
│   └── trade_logger.py  ← SQLite logging, stats, CSV export
└── dashboard/
    └── app.py           ← Local web dashboard
```

---

## Telegram Alerts

Wolf sends alerts for:
- 🐺 INFO: heartbeat, trade executed, whale move
- ⚠️ WARNING: feed stale, API issues
- 🚨🚨🚨 CRITICAL: kill switch, daily halt, system down

---

*Wolf — built to survive first, compound second.*
