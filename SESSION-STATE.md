# SESSION-STATE.md — Active Working Memory

## Current Focus
- Wolf trading bot initial build — setup phase complete, entering Build Mode
- Waiting on videos from Jefe for trading strategy breakdown

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
- Model stack: Claude Sonnet 4.6 (OpenRouter) primary, Gemini 2.5 Pro challenger
- Perplexity = primary research/search layer
- Brave = raw fallback search
- Trading system phases: Research → Strategy → Risk Engine → Backtesting → Paper Trading → Live (small sizing)
- No live execution without explicit Jefe authorization
- Cost discipline: model calls only where judgment is needed; rule-based logic elsewhere

## Blockers
- None — waiting on videos/X post for strategy research

---
Last updated: 2026-03-29
