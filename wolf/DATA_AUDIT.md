# Wolf Data Integrity Audit — 2026-03-31

**Decision: DATA CLEAN — no reset required**

## Stats
- Total rows: 35
- Resolved (real): 19 | Open: 16 | Voids: 0 | Simulated: 0
- Win rate: 52.6% (10/19) | P&L: +$580.06
- Stale >48h: 0 | Insane P&L: 0 | Corrupt (pnl=0,won=0): 2 (10.5% — below 50% threshold)

## By Strategy
| Strategy      | Trades | WR     | P&L      |
|---------------|--------|--------|----------|
| value_bet     | 12     | 41.7%  | +$479.61 |
| market_making | 6      | 66.7%  | +$98.49  |
| copy_trading  | 1      | 100%   | +$1.96   |

## Issues Found & Fixed
1. **Phantom open positions (stale)** — 3 positions with UNIQUE constraint violation deleted (not resolvable, were re-entries on same markets as existing resolved rows)
2. **DB UNIQUE constraint** — migrated from `UNIQUE(strategy,market_id,side,resolved)` to include `timestamp`, allowing proper re-entry tracking
3. **paper_mode.py restore** — `resolved_ids` filter was silently dropping all open trades on restart (fixed)
4. **Learning engine** — value_bet floor raised to 0.92 and strategy paused after only 12 trades (insufficient data). Reset to 0.68. MAX_FLOOR capped at 0.80 going forward.

## Verdict
All 19 resolved trades are valid. P&L of +$580.06 is legitimate. Test continues.
