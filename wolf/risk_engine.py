"""
Wolf Trading Bot — Risk Engine
Hard rules. No exceptions. This runs under every strategy.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
import config

logger = logging.getLogger("wolf.risk")

@dataclass
class TradeRecord:
    timestamp: float
    strategy: str
    market_id: str
    side: str
    size: float
    entry_price: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"  # open | closed | cancelled

class RiskEngine:
    def __init__(self, starting_balance: float = 10000.0):  # default to $10K; always overridden by main.py
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.daily_start_balance = starting_balance
        self.open_positions: list[TradeRecord] = []
        self.closed_trades: list[TradeRecord] = []
        self.halted = False
        self.halt_reason = ""
        self.last_reset = self._today()
        self._check_daily_reset()

    def _today(self) -> str:
        from datetime import date
        return str(date.today())

    def _check_daily_reset(self):
        today = self._today()
        if self.last_reset != today:
            self.daily_start_balance = self.current_balance
            self.last_reset = today
            if self.halted and self.halt_reason == "daily_loss":
                self.halted = False
                self.halt_reason = ""
                logger.info("Daily loss halt lifted — new trading day")

    def can_trade(self, market_volume: float = None) -> tuple[bool, str]:
        """Check all risk gates before allowing a trade."""
        self._check_daily_reset()

        if self.halted:
            return False, f"Halted: {self.halt_reason}"

        max_pos = getattr(config, "MAX_OPEN_POSITIONS_PAPER", config.MAX_OPEN_POSITIONS) if config.PAPER_MODE else config.MAX_OPEN_POSITIONS
        if len(self.open_positions) >= max_pos:
            return False, f"Max open positions reached ({len(self.open_positions)}/{max_pos})"

        daily_pnl_pct = (self.current_balance - self.daily_start_balance) / self.daily_start_balance
        if daily_pnl_pct <= config.DAILY_LOSS_LIMIT:
            self.halted = True
            self.halt_reason = "daily_loss"
            logger.warning(f"Daily loss limit hit: {daily_pnl_pct:.1%}")
            return False, f"Daily loss limit hit: {daily_pnl_pct:.1%}"

        total_drawdown = (self.current_balance - self.starting_balance) / self.starting_balance
        if total_drawdown <= config.KILL_SWITCH_THRESHOLD:
            self.halted = True
            self.halt_reason = "kill_switch"
            logger.critical(f"KILL SWITCH TRIGGERED: drawdown {total_drawdown:.1%}")
            return False, f"Kill switch: drawdown {total_drawdown:.1%}"

        if market_volume is not None and market_volume < config.MIN_MARKET_VOLUME:
            return False, f"Market volume too low: ${market_volume:,.0f}"

        return True, "ok"

    def get_position_size(self, edge: float, confidence: float,
                          entry_price: float = 0.5) -> float:
        """
        Kelly Criterion position sizing. Conservative half-Kelly.
        Uses entry_price for correct binary market odds calculation.
        Binary payout: win (1 - entry_price), lose entry_price.
        b = (1 - entry_price) / entry_price
        """
        if confidence < config.MIN_CONFIDENCE:
            return 0.0

        entry_price = max(0.05, min(0.95, entry_price))  # clamp to sane range
        p = confidence
        q = 1.0 - p
        b = (1.0 - entry_price) / entry_price  # correct binary odds

        kelly_fraction = (b * p - q) / b
        half_kelly = kelly_fraction * 0.5  # half-Kelly for safety

        # Cap at MAX_POSITION_PCT
        fraction = min(half_kelly, config.MAX_POSITION_PCT)
        fraction = max(fraction, 0.0)

        size = self.current_balance * fraction

        # Hard caps — always enforced regardless of Kelly output
        if config.PAPER_MODE:
            size = min(size, config.MAX_POSITION_PAPER)  # Paper: cap at $200 for realistic small-account sim
        else:
            if size < config.MIN_POSITION_LIVE:
                return 0.0  # Below minimum — skip trade entirely
            size = min(size, config.MAX_POSITION_LIVE)

        return round(size, 2)

    def open_position(self, trade: TradeRecord):
        self.open_positions.append(trade)
        logger.info(f"Position opened: {trade.market_id} {trade.side} ${trade.size}")

    def close_position(self, market_id: str, exit_price: float) -> Optional[TradeRecord]:
        for i, pos in enumerate(self.open_positions):
            if pos.market_id == market_id and pos.status == "open":
                pos.exit_price = exit_price
                pos.status = "closed"
                # Binary market: win = exit at $1, lose = exit at $0
                if pos.side == "YES":
                    pos.pnl = pos.size * (exit_price - pos.entry_price)
                else:
                    pos.pnl = pos.size * (pos.entry_price - exit_price)
                self.current_balance += pos.pnl
                self.open_positions.pop(i)
                self.closed_trades.append(pos)
                logger.info(f"Position closed: {market_id} P&L ${pos.pnl:+.2f}")
                self._check_kill_switch()
                return pos
        return None

    def _check_kill_switch(self):
        total_drawdown = (self.current_balance - self.starting_balance) / self.starting_balance
        if total_drawdown <= config.KILL_SWITCH_THRESHOLD:
            self.halted = True
            self.halt_reason = "kill_switch"
            logger.critical(f"KILL SWITCH: drawdown {total_drawdown:.1%}")

    def update_balance(self, amount: float):
        self.current_balance += amount

    def get_stats(self) -> dict:
        self._check_daily_reset()
        closed = self.closed_trades
        wins = [t for t in closed if t.pnl and t.pnl > 0]
        losses = [t for t in closed if t.pnl and t.pnl <= 0]
        win_rate = len(wins) / len(closed) if closed else 0
        total_pnl = sum(t.pnl for t in closed if t.pnl)
        daily_pnl = self.current_balance - self.daily_start_balance
        drawdown = (self.current_balance - self.starting_balance) / self.starting_balance
        return {
            "balance": self.current_balance,
            "total_pnl": total_pnl,
            "daily_pnl": daily_pnl,
            "drawdown_pct": drawdown,
            "win_rate": win_rate,
            "total_trades": len(closed),
            "open_positions": len(self.open_positions),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }
