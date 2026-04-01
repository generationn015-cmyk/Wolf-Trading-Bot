"""
Lighter Risk Manager — Central risk gate
All orders must pass through this before execution.
"""
import time
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger("lighter.risk")

class RiskManager:
    def __init__(self):
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.peak_equity = config.PAPER_STARTING_CAPITAL
        self.consecutive_losses = 0
        self.circuit_breaker_until = 0.0
        self.open_positions = {}

    def can_trade(self, proposed_order: dict) -> tuple:
        now = time.time()
        if now < self.circuit_breaker_until:
            return False, "Circuit breaker active"
        account_value = proposed_order.get("account_value", config.PAPER_STARTING_CAPITAL)
        if self.daily_pnl < -(account_value * config.DAILY_LOSS_LIMIT_PCT):
            return False, "Daily loss limit reached"
        if account_value < self.peak_equity * 0.85:
            return False, "Max drawdown exceeded"
        if len(self.open_positions) >= config.MAX_OPEN_POSITIONS:
            return False, f"Max positions ({config.MAX_OPEN_POSITIONS}) reached"
        return True, ""

    def record_result(self, pnl: float):
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= 5:
                self.circuit_breaker_until = time.time() + 3600
                logger.warning(f"CIRCUIT BREAKER: {self.consecutive_losses} losses. Pausing 1h.")
        else:
            self.consecutive_losses = 0
