# SESSION-STATE.md — Wolf Session Continuity

## Purpose
Written before every session compact so nothing is lost.
Wolf reads this at the start of each new session to resume context instantly.

## Last Updated
2026-03-30 07:35 EDT

## Wolf Status
- Paper trading: ACTIVE
- Balance: ~$9,300+ (started $1,000)
- Win Rate: 65.7% (target: 72% to unlock live gate)
- Trades: 246 completed
- Mode: PAPER_MODE = True (do not change without explicit Jefe approval)

## What's Built & Working
- 8 strategies: value_bet, copy_trading, market_making, latency_arb, complement_arb, near_expiry, timezone_arb, ta_signal
- Learning engine: persists floors/bad ranges to learning_state.json — survives restarts
- Watchdog: auto-restarts Wolf if it crashes
- 6AM morning report: fires automatically, sends to Jefe via Telegram with buttons
- CLOB data feed: fixed — reads clob.polymarket.com/spread (not gamma-api midprices)
- VPIN threshold: 0.30 (raised from 0.15)
- Dedup index: scoped to open trades only (WHERE resolved=0)

## Key Config Values (do not change without diagnosis)
- MIN_CONFIDENCE: 0.68
- MIN_MARKET_VOLUME: $50,000
- COPY_TRADE_MIN_SIZE: $30
- VPIN_SPIKE_THRESHOLD: 0.30
- MAX_OPEN_POSITIONS: 8
- PAPER_MODE: True

## Learning Engine State
- value_bet floor: 0.85 (raised — WR was 60-62%)
- copy_trading floor: 0.68 (relaxed — WR 92%)
- Blocked ranges: [0.35-0.45] (weak WR historically)

## Live Gate Requirements (before going live)
1. WR >= 72% sustained ✅ (not yet — at 65.7%)
2. 100+ trades completed ✅ (246 done)
3. All 8 strategies have fired at least once
4. Jefe explicit approval

## Pending / Upcoming
- Vercel dashboard: Jefe building UI, Wolf will link API to it
  - Backend: FastAPI on port 5000 (0.0.0.0 — VPS accessible)
  - CORS: open (allow_origins=["*"])
  - WebSocket: ws://[VPS-IP]:5000/ws (live updates every 5s)
  - Key endpoints: /api/stats, /api/logs, /api/control/restart, /api/control/kill
- Latency arb + TA signal: not yet fired (need BTC volatility)
- Alert Wolf when WR crosses 72% threshold

## Cost Management
- Model: Claude Sonnet 4.6 via OpenRouter — keep for trading sessions
- Compact session proactively when context grows large
- Heartbeat: overnight reduce to every 2h (update HEARTBEAT.md)
- Normal daily cost: ~$1-2/day (not $3.78 — today was a build day)

## Files & Paths
- Config: /data/.openclaw/workspace/wolf/config.py
- DB: /data/.openclaw/workspace/wolf/wolf_data.db
- Learning state: /data/.openclaw/workspace/wolf/learning_state.json
- Morning report: /data/.openclaw/workspace/wolf/scripts/morning_report.py
- Watchdog: /data/.openclaw/workspace/wolf/watchdog.sh
- Dashboard backend: /data/.openclaw/workspace/wolf/dashboard/app.py
- Log: /data/.openclaw/workspace/wolf/wolf.log
