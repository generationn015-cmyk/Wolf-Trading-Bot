# Kalshi Strategy Notes — Phase 1.5 / Phase 2

## Why Kalshi belongs in Wolf
Kalshi does NOT have the BTC/ETH 15-minute crypto contracts that make Polymarket latency arb so attractive.
That does **not** weaken Wolf. It means Kalshi uses different edges:

- Fed rate decision markets → latency vs CME FedWatch / statement parsing
- Economic indicator markets → CPI / jobs / GDP release speed
- Sports markets → injury / lineup / odds movement edge
- Copy trading / orderflow monitoring → same concept, different venue

## Phase 1.5 plan
Add Kalshi cleanly without confusing the system:

### Phase 1.5 scope
- Market discovery wrapper (`feeds/kalshi_feed.py`)
- Fed market discovery
- Economic market discovery
- Basic orderbook reads
- No forced live execution
- Paper-mode only first

### Phase 2 scope
- Add authenticated Kalshi execution
- Add copy-trading heuristics where supported
- Add sports injury intelligence layer (free-tier APIs first)

## Strategy mapping

| Strategy | Polymarket | Kalshi |
|---|---|---|
| Crypto latency arb | ✅ BTC/ETH 15m | ❌ No crypto markets |
| Macro latency arb | ✅ Some markets | ✅ Strong fit |
| Copy trading | ✅ | ✅ (where discoverable) |
| Market making | ✅ | ✅ |
| Sports edge | ✅ | ✅ |

## Free-tier sports API recommendation
1. The Odds API — line movement + basic odds coverage
2. ESPN public endpoints — lightweight fallback
3. SportsRadar free trial — premium short-term test when Phase 2 begins

## Rule
Do NOT let Kalshi bloat Phase 1.
Design for it now. Build it in controlled steps.
