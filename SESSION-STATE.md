# SESSION-STATE.md — Active Working Memory

## Current Focus
- Wolf paper trading LIVE — accumulating trades toward gate (88 resolved, $+660 P&L, 38.6% win rate, gate needs 55%)
- All 3 strategies running: copy trading (20/20 wallets), market making (20 markets), latency arb
- Target: hit 55% win rate gate, then await Jefe go/no-go for live

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
- Win rate gate (38.6% → needs 55%) — resolving naturally as trades accumulate
- Need Jefe to confirm target market and exchange before advancing beyond Polymarket
- Polymarket credentials (private key, API key) needed before live mode

---
Last updated: 2026-03-29
