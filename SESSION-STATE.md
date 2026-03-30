# SESSION-STATE.md — Active Working Memory

## Current Focus
- Wolf trading bot — copy trading wallets FIXED (see Recent Decisions)
- Model routing and compaction limits locked down

## Key Context
- Running on Hostinger VPS (Docker container)
- Primary model: openrouter/anthropic/claude-sonnet-4-6
- Fallback/Challenger: google/gemini-2.5-pro
- Web search: Perplexity (active)
- Brave key present (raw search fallback, not yet set as secondary provider)
- Auth file secured (chmod 600)
- Docker handles container restart on VPS reboot — no systemd needed

## Pending Actions
- [ ] Review videos Jefe sends on trading bots
- [ ] Review X.com post Jefe sends
- [ ] Design trading system architecture based on research
- [ ] Define target market (crypto/stocks/forex/futures) — awaiting Jefe input
- [ ] Define broker/exchange target — awaiting Jefe input
- [ ] Set up project memory file: memory/projects/wolf-trading.md

## Recent Decisions
- **Model routing:** Claude Sonnet 4.6 (OpenRouter) is PRIMARY for everything — trading, strategy, conversation, code
- **No fallbacks to OpenAI direct** — quota issues; no GPT as primary
- **Compaction:** reserveTokensFloor=50k → fires at ~150k tokens (not 200k); memoryFlush enabled before compaction
- **Polymarket feed fixed:** leaderboard now uses `data-api.polymarket.com/v1/leaderboard` (was 404); wallet activity uses `/activity?user=` endpoint; wallet positions via `/positions?user=`
- Copy trading strategy now pulls live top-20 wallets by PnL and scans their recent activity
- Perplexity = primary research/search layer
- Trading system phases: Research → Strategy → Risk Engine → Backtesting → Paper Trading → Live (small sizing)
- No live execution without explicit Jefe authorization

## Blockers
- Wolf not running as daemon yet — copy trading wallets will populate once wolf/main.py is started
- Need Jefe to confirm target market and exchange before advancing strategies

---
Last updated: 2026-03-29
