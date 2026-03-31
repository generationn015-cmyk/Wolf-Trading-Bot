"""
Wolf Trading Bot — Paper Mode
Simulates all trades against live market data. No real money moves.
Paper mode runs CONTINUOUSLY until Jefe explicitly authorizes live.
The gate milestone triggers a Telegram alert to Jefe — it does NOT stop trading.
"""
import os
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
    venue: str        # polymarket | kalshi
    market_id: str
    side: str
    size: float
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    resolved: bool = False
    won: Optional[bool] = None
    market_end: float = 0.0  # unix timestamp of market expiry (0 = unknown)
    days_to_expiry: float = 0.0  # days until market resolves at entry time


class PaperTrader:
    def __init__(self, starting_balance: float = 10000.0):  # default to $10K; always overridden by main.py
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.trades: list[PaperTrade] = []
        self.open_trades: list[PaperTrade] = []
        self._load_from_db()

    def _load_from_db(self):
        """Restore all paper trades from DB on startup — resolved for stats, open for resolution."""
        try:
            import sqlite3
            db_path = config.DB_PATH
            if not os.path.exists(db_path):
                return
            with sqlite3.connect(db_path) as conn:
                # Load resolved trades for stats/gate
                rows = conn.execute(
                    "SELECT strategy, venue, market_id, side, size, entry_price, "
                    "exit_price, pnl, resolved, won, timestamp FROM paper_trades "
                    "WHERE resolved=1 AND simulated=0 ORDER BY timestamp ASC"
                ).fetchall()
                for row in rows:
                    t = PaperTrade(
                        timestamp=row[10], strategy=row[0], venue=row[1],
                        market_id=row[2], side=row[3], size=row[4],
                        entry_price=row[5], exit_price=row[6], pnl=row[7],
                        resolved=True, won=bool(row[9]),
                    )
                    self.trades.append(t)
                    if t.pnl is not None:
                        self.balance += t.pnl

                # Load open trades — skip any market already resolved (avoid lifecycle dupes)
                resolved_ids = {(r[0], r[2], r[3]) for r in rows}  # (strategy, market_id, side)
                open_rows = conn.execute(
                    "SELECT strategy, venue, market_id, side, size, entry_price, timestamp "
                    "FROM paper_trades WHERE resolved=0 AND simulated=0"
                ).fetchall()
                # Filter out markets that already have a resolved entry
                open_rows = [r for r in open_rows
                             if (r[0], r[2], r[3]) not in resolved_ids]
                for row in open_rows:
                    t = PaperTrade(
                        timestamp=row[6], strategy=row[0], venue=row[1],
                        market_id=row[2], side=row[3], size=row[4],
                        entry_price=row[5],
                    )
                    self.open_trades.append(t)

            resolved_count = len(self.trades)
            open_count = len(self.open_trades)
            if resolved_count or open_count:
                logger.info(
                    f"Restored {resolved_count} resolved + {open_count} open trades | "
                    f"balance=${self.balance:.2f}"
                )
        except Exception as e:
            logger.warning(f"Could not restore paper trades from DB: {e}")

    def place_trade(self, strategy: str, venue: str, market_id: str,
                    side: str, size: float, entry_price: float,
                    market_end: float = 0.0, days_to_expiry: float = 0.0) -> PaperTrade:
        trade = PaperTrade(
            timestamp=time.time(),
            strategy=strategy,
            venue=venue,
            market_id=market_id,
            side=side,
            size=size,
            entry_price=entry_price,
            market_end=market_end,
            days_to_expiry=days_to_expiry,
        )
        self.open_trades.append(trade)
        logger.info(f"[PAPER] {venue} {strategy} | {market_id} {side} ${size:.2f} @ {entry_price:.3f}")
        return trade

    def resolve_trade(self, market_id: str, outcome: str) -> Optional[PaperTrade]:
        """
        Resolve ALL open trades on a given market. outcome = 'YES' or 'NO'.
        Returns the last resolved trade (or None if none found).
        """
        resolved_trade = None
        to_remove = []

        for trade in list(self.open_trades):
            if trade.market_id == market_id:
                won = (trade.side == outcome)
                trade.won = won
                trade.resolved = True
                if won:
                    # Polymarket pays $1 per share at resolution.
                    # You paid entry_price per share → profit = size * (1/entry_price - 1)
                    # e.g. $40 at 0.25 → win $40*(4-1) = $120 profit
                    trade.exit_price = 1.0
                    trade.pnl = trade.size * (1.0 / trade.entry_price - 1.0)
                else:
                    # Shares expire worthless → lose full stake
                    trade.exit_price = 0.0
                    trade.pnl = -trade.size
                self.balance += trade.pnl
                self.trades.append(trade)
                to_remove.append(trade)
                result = "WIN" if won else "LOSS"
                logger.info(f"[PAPER] {result} | {market_id} P&L ${trade.pnl:+.2f} | Balance ${self.balance:.2f}")
                resolved_trade = trade

        for t in to_remove:
            if t in self.open_trades:
                self.open_trades.remove(t)

        return resolved_trade

    def has_passed_gate(self) -> tuple[bool, str]:
        """
        Check if paper trading milestone has been reached.
        NOTE: Passing the gate sends Jefe a Telegram alert — it does NOT stop trading.
        Wolf continues paper trading indefinitely until Jefe explicitly authorizes live mode.
        """
        resolved = [t for t in self.trades if t.resolved]
        total = len(resolved)
        if total < config.PAPER_GATE_MIN_TRADES:
            remaining = config.PAPER_GATE_MIN_TRADES - total
            return False, f"Need {remaining} more trades ({total}/{config.PAPER_GATE_MIN_TRADES})"
        wins = len([t for t in resolved if t.won])
        win_rate = wins / total if total > 0 else 0.0
        if win_rate < config.PAPER_GATE_MIN_WIN_RATE:
            return False, f"Win rate {win_rate:.1%} below {config.PAPER_GATE_MIN_WIN_RATE:.0%} gate"
        return True, f"Gate milestone: {total} trades @ {win_rate:.1%} win rate"

    def get_stats(self) -> dict:
        resolved = [t for t in self.trades if t.resolved]
        total = len(resolved)
        wins = [t for t in resolved if t.won]
        win_rate = len(wins) / total if total else 0.0
        total_pnl = sum(t.pnl for t in resolved if t.pnl is not None)
        gate_passed, gate_msg = self.has_passed_gate()
        by_strategy = {}
        for t in resolved:
            s = t.strategy
            if s not in by_strategy:
                by_strategy[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_strategy[s]["trades"] += 1
            by_strategy[s]["wins"] += int(bool(t.won))
            by_strategy[s]["pnl"] += t.pnl or 0.0
        return {
            "balance": self.balance,
            "starting_balance": self.starting_balance,
            "total_pnl": total_pnl,
            "total_trades": total,
            "win_rate": win_rate,
            "open_trades": len(self.open_trades),
            "gate_passed": gate_passed,
            "gate_message": gate_msg,
            "by_strategy": by_strategy,
        }
