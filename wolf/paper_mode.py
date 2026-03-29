"""
Wolf Trading Bot — Paper Mode
Simulates all trades against live market data. No real money moves.
Gate: 200+ trades AND 80%+ win rate required before going live.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
import config

logger = logging.getLogger("wolf.paper")

@dataclass
class PaperTrade:
    timestamp: float
    strategy: str
    venue: str  # polymarket | kalshi
    market_id: str
    side: str
    size: float
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    resolved: bool = False
    won: Optional[bool] = None

class PaperTrader:
    def __init__(self, starting_balance: float = 1000.0):
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.trades: list[PaperTrade] = []
        self.open_trades: list[PaperTrade] = []

    def place_trade(self, strategy: str, venue: str, market_id: str,
                    side: str, size: float, entry_price: float) -> PaperTrade:
        trade = PaperTrade(
            timestamp=time.time(),
            strategy=strategy,
            venue=venue,
            market_id=market_id,
            side=side,
            size=size,
            entry_price=entry_price,
        )
        self.open_trades.append(trade)
        logger.info(f"[PAPER] {venue} {strategy} | {market_id} {side} ${size:.2f} @ {entry_price:.3f}")
        return trade

    def resolve_trade(self, market_id: str, outcome: str) -> Optional[PaperTrade]:
        """Resolve a trade. outcome = 'YES' or 'NO'."""
        for i, trade in enumerate(self.open_trades):
            if trade.market_id == market_id:
                won = (trade.side == outcome)
                trade.won = won
                trade.resolved = True
                if won:
                    trade.exit_price = 1.0
                    trade.pnl = trade.size * (1.0 - trade.entry_price)
                else:
                    trade.exit_price = 0.0
                    trade.pnl = -trade.size * trade.entry_price
                self.balance += trade.pnl
                self.open_trades.pop(i)
                self.trades.append(trade)
                result = "WIN" if won else "LOSS"
                logger.info(f"[PAPER] {result} | {market_id} P&L ${trade.pnl:+.2f} | Balance ${self.balance:.2f}")
                return trade
        return None

    def has_passed_gate(self) -> tuple[bool, str]:
        """Check if paper trading gate is passed."""
        resolved = [t for t in self.trades if t.resolved]
        total = len(resolved)
        if total < config.PAPER_GATE_MIN_TRADES:
            remaining = config.PAPER_GATE_MIN_TRADES - total
            return False, f"Need {remaining} more trades ({total}/{config.PAPER_GATE_MIN_TRADES})"
        wins = len([t for t in resolved if t.won])
        win_rate = wins / total
        if win_rate < config.PAPER_GATE_MIN_WIN_RATE:
            return False, f"Win rate {win_rate:.1%} below {config.PAPER_GATE_MIN_WIN_RATE:.0%} gate"
        return True, f"Gate PASSED: {total} trades, {win_rate:.1%} win rate"

    def get_stats(self) -> dict:
        resolved = [t for t in self.trades if t.resolved]
        total = len(resolved)
        wins = [t for t in resolved if t.won]
        win_rate = len(wins) / total if total else 0
        total_pnl = sum(t.pnl for t in resolved if t.pnl)
        gate_passed, gate_msg = self.has_passed_gate()
        return {
            "balance": self.balance,
            "starting_balance": self.starting_balance,
            "total_pnl": total_pnl,
            "total_trades": total,
            "win_rate": win_rate,
            "open_trades": len(self.open_trades),
            "gate_passed": gate_passed,
            "gate_message": gate_msg,
        }
