# SESSION-STATE.md — Active Working Memory

## Current Focus
- Wolf paper trading LIVE and running continuously via watchdog
- All 3 strategies active: copy trading, market making, latency arb
- Learning engine online — adapting thresholds from trade history
- Paper mode runs INDEFINITELY until Jefe says stop

## Wolf Status (as of 2026-03-29 ~22:27 EDT)
- Watchdog PID running — auto-restarts on any crash
- ~41 resolved trades | ~65.9% WR | ~$521 P&L | Balance ~$1,521
- Gate milestone: 50 trades @ 55% WR — will Telegram alert Jefe when hit, NOT stop trading
- Dedup active: 15 market IDs blocked from re-firing
- Learning engine: copy trading floor at 0.70, 1 bad price range blocked

## Credentials
- POLYMARKET_PRIVATE_KEY: stored in wolf/.env ✅
- POLYMARKET_API_KEY/SECRET/PASSPHRASE: derived and stored ✅
- CLOB client: authenticated ✅
- WOLF_PAPER_MODE=true — live execution locked until Jefe authorizes

## Infrastructure
- VPS: Hostinger Docker container
- Primary model: openrouter/anthropic/claude-sonnet-4-6
- GitHub backup: auto on every heartbeat
- Wolf log: /data/.openclaw/workspace/wolf/wolf.log
- Wolf DB: /data/.openclaw/workspace/wolf/wolf_data.db
- Watchdog: bash /data/.openclaw/workspace/wolf/watchdog.sh

## Pending Actions
- [ ] Jefe to authorize live trading when satisfied with paper results
- [ ] Win rate target: 85-95% (currently ~66%, learning engine pushing it up)
- [ ] Market making and latency arb strategies need more signal volume
- [ ] Consider news sniper as 4th strategy (future capability)

## Recent Decisions
- Paper mode never stops on gate — gate = Telegram alert only
- All bugs fixed: dedup, resolve-all-per-market, graceful shutdown, logging
- Polymarket private key stored; full CLOB auth working
- wolf/.env gitignored — credentials never pushed to GitHub
- Learning engine adapts confidence floors from loss analysis every 5 min

---
Last updated: 2026-03-29 22:29 EDT
