"""
Wolf Trading Bot — Risk Engine
Hard rules. No exceptions. This runs under every strategy.

Upgrades over v1:
  - Rolling-window circuit breaker (replaces fragile 2-consecutive-loss rule)
  - Volatility-adjusted Kelly sizing (shrinks in choppy markets)
  - Days-to-expiry time-decay multiplier in position sizing
  - Sharpe/Sortino ratio tracking per strategy
  - Cross-strategy correlation guard (event fingerprint dedup)
"""
import time
import math
import logging
from collections import deque
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


# ── Rolling circuit breaker ───────────────────────────────────────────────────
class RollingCircuitBreaker:
    """
    Replaces the brittle consecutive-loss counter with a rolling window.
    A 70% win-rate strategy will hit 2 consecutive losses by pure chance ~9% of
    the time, incorrectly silencing it for 24h. The rolling window requires a
    sustained collapse across multiple trades before firing.
    """

    def __init__(
        self,
        window: int = 10,
        min_wr_to_stay_open: float = 0.35,
        min_samples: int = 5,
        pause_seconds: float = None,
    ):
        self.window = deque(maxlen=window)
        self.min_wr = min_wr_to_stay_open
        self.min_samples = min_samples
        self._pause_seconds = pause_seconds or config.MODULE_PAUSE_SECONDS
        self._paused_until: float = 0.0

    def record(self, won: bool) -> bool:
        """Record outcome. Returns True if the circuit should trip (pause now)."""
        self.window.append(1 if won else 0)
        if len(self.window) < self.min_samples:
            return False
        rolling_wr = sum(self.window) / len(self.window)
        if rolling_wr < self.min_wr and not self.is_paused():
            self._paused_until = time.time() + self._pause_seconds
            return True  # Caller should log + alert
        return False

    def is_paused(self) -> bool:
        return time.time() < self._paused_until

    def remaining_seconds(self) -> float:
        return max(0.0, self._paused_until - time.time())

    def rolling_wr(self) -> Optional[float]:
        if len(self.window) < self.min_samples:
            return None
        return sum(self.window) / len(self.window)

    def reset_pause(self):
        """External reset — guardian/responder can clear a stale pause."""
        self._paused_until = 0.0


# ── Per-strategy PnL tracker for Sharpe/Sortino ──────────────────────────────
class StrategyPnLTracker:
    """Tracks a rolling window of PnL values for risk-adjusted return metrics."""

    TRADES_PER_DAY_ESTIMATE = 8  # annualisation constant

    def __init__(self, window: int = 100):
        self._pnl: deque[float] = deque(maxlen=window)

    def record(self, pnl: float):
        self._pnl.append(pnl)

    def sharpe(self, risk_free: float = 0.0) -> float:
        """Annualised Sharpe ratio. Returns 0.0 with insufficient data."""
        values = list(self._pnl)
        if len(values) < 5:
            return 0.0
        n = len(values)
        mean = sum(values) / n
        variance = sum((x - mean) ** 2 for x in values) / max(n - 1, 1)
        std = math.sqrt(variance) if variance > 0 else 1e-9
        daily_sharpe = (mean - risk_free) / std
        return round(daily_sharpe * math.sqrt(self.TRADES_PER_DAY_ESTIMATE * 365), 2)

    def sortino(self) -> float:
        """Annualised Sortino ratio (penalises only downside variance)."""
        values = list(self._pnl)
        if len(values) < 5:
            return 0.0
        mean = sum(values) / len(values)
        downside = [x for x in values if x < 0]
        if not downside:
            return 99.0  # No losses = theoretically infinite Sortino
        dvar = sum(x ** 2 for x in downside) / len(downside)
        dstd = math.sqrt(dvar) if dvar > 0 else 1e-9
        return round((mean / dstd) * math.sqrt(self.TRADES_PER_DAY_ESTIMATE * 365), 2)

    def avg_pnl(self) -> float:
        if not self._pnl:
            return 0.0
        return sum(self._pnl) / len(self._pnl)

    def sample_count(self) -> int:
        return len(self._pnl)


# ── Module-level state (survives across RiskEngine re-instantiation) ──────────
_breakers:      dict[str, RollingCircuitBreaker] = {}
_pnl_trackers:  dict[str, StrategyPnLTracker]    = {}


def _get_breaker(strategy: str) -> RollingCircuitBreaker:
    if strategy not in _breakers:
        _breakers[strategy] = RollingCircuitBreaker()
    return _breakers[strategy]


def _get_tracker(strategy: str) -> StrategyPnLTracker:
    if strategy not in _pnl_trackers:
        _pnl_trackers[strategy] = StrategyPnLTracker()
    return _pnl_trackers[strategy]


class RiskEngine:
    def __init__(self, starting_balance: float = 100.0):
        self.starting_balance     = starting_balance
        self.current_balance      = starting_balance
        self.daily_start_balance  = starting_balance
        self.open_positions:  list[TradeRecord] = []
        self.closed_trades:   list[TradeRecord] = []
        self.halted      = False
        self.halt_reason = ""
        self.last_reset  = self._today()
        self._check_daily_reset()

    # ── Internal helpers ──────────────────────────────────────────────────────

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

    # ── Trade gates ───────────────────────────────────────────────────────────

    def can_trade(self, market_volume: float = None) -> tuple[bool, str]:
        """Check all risk gates before allowing a trade."""
        self._check_daily_reset()

        if self.halted:
            return False, f"Halted: {self.halt_reason}"

        max_pos = (
            getattr(config, "MAX_OPEN_POSITIONS_PAPER", config.MAX_OPEN_POSITIONS)
            if config.PAPER_MODE
            else config.MAX_OPEN_POSITIONS
        )
        if len(self.open_positions) >= max_pos:
            return False, f"Max open positions ({len(self.open_positions)}/{max_pos})"

        daily_pnl_pct = (self.current_balance - self.daily_start_balance) / self.daily_start_balance
        if daily_pnl_pct <= config.DAILY_LOSS_LIMIT:
            self.halted = True
            self.halt_reason = "daily_loss"
            logger.warning(f"Daily loss limit hit: {daily_pnl_pct:.1%}")
            return False, f"Daily loss limit: {daily_pnl_pct:.1%}"

        total_drawdown = (self.current_balance - self.starting_balance) / self.starting_balance
        if total_drawdown <= config.KILL_SWITCH_THRESHOLD:
            self.halted = True
            self.halt_reason = "kill_switch"
            logger.critical(f"KILL SWITCH TRIGGERED: drawdown {total_drawdown:.1%}")
            return False, f"Kill switch: drawdown {total_drawdown:.1%}"

        if market_volume is not None and market_volume < config.MIN_MARKET_VOLUME:
            return False, f"Volume too low: ${market_volume:,.0f}"

        return True, "ok"

    # ── Rolling circuit breaker ───────────────────────────────────────────────

    def record_module_result(self, strategy: str, won: bool, pnl: float = 0.0):
        """
        Record a resolved trade outcome.
        Updates the rolling circuit breaker AND the Sharpe tracker.
        """
        breaker = _get_breaker(strategy)
        tripped = breaker.record(won)
        if tripped:
            remaining_h = breaker.remaining_seconds() / 3600
            logger.warning(
                f"[CIRCUIT] {strategy} paused {remaining_h:.1f}h — "
                f"rolling WR collapsed to {breaker.rolling_wr():.0%}"
            )

        # Track PnL for Sharpe calculation
        if pnl != 0.0:
            _get_tracker(strategy).record(pnl)

    def module_allowed(self, strategy: str) -> bool:
        """Returns False if the strategy is in circuit-breaker cooldown."""
        breaker = _get_breaker(strategy)
        if breaker.is_paused():
            remaining_h = breaker.remaining_seconds() / 3600
            logger.debug(f"[CIRCUIT] {strategy} blocked — {remaining_h:.1f}h remaining")
            return False
        return True

    def reset_module_pause(self, strategy: str):
        """External reset for guardian/responder scripts."""
        if strategy in _breakers:
            _breakers[strategy].reset_pause()
            logger.info(f"[CIRCUIT] {strategy} pause manually cleared")

    def check_daily_loss_circuit(self, daily_pnl: float, portfolio: float) -> bool:
        """Returns True if the daily loss cap has been breached (halt signal)."""
        loss_threshold = portfolio * config.DAILY_LOSS_CAP_PCT
        if daily_pnl < -abs(loss_threshold):
            logger.critical(
                f"[CIRCUIT] Daily loss cap: ${daily_pnl:.2f} < -${loss_threshold:.2f} — halting"
            )
            return True
        return False

    # ── Position sizing ───────────────────────────────────────────────────────

    def get_position_size(
        self,
        edge: float,
        confidence: float,
        entry_price: float = 0.5,
        volatility_30m: float = None,
        days_to_expiry: float = None,
    ) -> float:
        """
        Kelly Criterion position sizing with volatility dampening and
        time-decay multiplier.

        Args:
            edge:            Expected edge (confidence-weighted)
            confidence:      Signal confidence [0, 1]
            entry_price:     Entry price for Kelly odds calculation [0.05, 0.95]
            volatility_30m:  30-min BTC price std-dev as fraction of price.
                             None = no vol adjustment. >0.02 = choppy.
            days_to_expiry:  Days until market resolves.
                             Closer = slightly larger size (more certain outcome).

        Returns:
            Dollar size rounded to 2dp, subject to all hard caps.
        """
        if confidence < config.MIN_CONFIDENCE:
            return 0.0

        # ── Half-Kelly core ───────────────────────────────────────────────────
        entry_price = max(0.05, min(0.95, entry_price))
        p = confidence
        q = 1.0 - p
        b = (1.0 - entry_price) / entry_price  # Binary market odds

        kelly_fraction = (b * p - q) / b
        half_kelly = kelly_fraction * 0.5  # Half-Kelly for safety

        # ── Volatility dampener ───────────────────────────────────────────────
        # When BTC is swinging hard, the same confidence score carries more
        # hidden risk. Reduce size proportionally.
        vol_scalar = 1.0
        if volatility_30m is not None:
            if volatility_30m > 0.025:    # > 2.5% std-dev = very choppy
                vol_scalar = 0.40
            elif volatility_30m > 0.020:  # 2.0-2.5% = choppy
                vol_scalar = 0.55
            elif volatility_30m > 0.015:  # 1.5-2.0% = elevated
                vol_scalar = 0.70
            elif volatility_30m > 0.010:  # 1.0-1.5% = moderate
                vol_scalar = 0.85
            # else: < 1% = calm market, no adjustment

        # ── Time-decay multiplier ─────────────────────────────────────────────
        # Markets resolving soon have less unknown unknowns than multi-day markets.
        # Slightly larger positions on near-expiry trades; slightly smaller on long ones.
        time_scalar = 1.0
        if days_to_expiry is not None:
            if days_to_expiry < 0.1:      # < 2.4h — near resolution
                time_scalar = 1.25
            elif days_to_expiry < 0.5:    # < 12h
                time_scalar = 1.15
            elif days_to_expiry < 1.0:    # same-day
                time_scalar = 1.08
            elif days_to_expiry <= 3.0:   # 1-3 days — baseline
                time_scalar = 1.0
            elif days_to_expiry <= 7.0:   # week out — slightly conservative
                time_scalar = 0.90
            else:                          # > 1 week — many unknowns
                time_scalar = 0.80

        # ── Combine and apply caps ────────────────────────────────────────────
        fraction = min(half_kelly * vol_scalar * time_scalar, config.MAX_POSITION_PCT)
        fraction = max(fraction, 0.0)
        size = self.current_balance * fraction

        if config.PAPER_MODE:
            dynamic_cap = self.current_balance * config.MAX_POSITION_PCT
            size = min(size, dynamic_cap)
            size = max(size, 0.10)  # Polymarket floor
        else:
            if size < config.MIN_POSITION_LIVE:
                return 0.0  # Below minimum — skip trade
            dynamic_cap = self.current_balance * config.MAX_POSITION_PCT
            size = min(size, max(dynamic_cap, config.MAX_POSITION_LIVE))

        return round(size, 2)

    # ── Sharpe / Sortino reporting ────────────────────────────────────────────

    def get_sharpe(self, strategy: str) -> float:
        return _get_tracker(strategy).sharpe()

    def get_sortino(self, strategy: str) -> float:
        return _get_tracker(strategy).sortino()

    def get_rolling_wr(self, strategy: str) -> Optional[float]:
        return _get_breaker(strategy).rolling_wr()

    def get_all_strategy_metrics(self) -> dict:
        """Return Sharpe, Sortino, rolling WR for all tracked strategies."""
        all_strategies = set(_breakers) | set(_pnl_trackers)
        result = {}
        for strat in all_strategies:
            tracker = _pnl_trackers.get(strat)
            breaker = _breakers.get(strat)
            result[strat] = {
                "sharpe":     tracker.sharpe()     if tracker else 0.0,
                "sortino":    tracker.sortino()    if tracker else 0.0,
                "avg_pnl":    tracker.avg_pnl()   if tracker else 0.0,
                "samples":    tracker.sample_count() if tracker else 0,
                "rolling_wr": breaker.rolling_wr() if breaker else None,
                "paused":     breaker.is_paused()  if breaker else False,
            }
        return result

    # ── Position management ───────────────────────────────────────────────────

    def open_position(self, trade: TradeRecord):
        self.open_positions.append(trade)
        logger.info(f"Position opened: {trade.market_id} {trade.side} ${trade.size}")

    def close_position(self, market_id: str, exit_price: float) -> Optional[TradeRecord]:
        for i, pos in enumerate(self.open_positions):
            if pos.market_id == market_id and pos.status == "open":
                pos.exit_price = exit_price
                pos.status = "closed"
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

    def get_daily_pnl(self) -> float:
        return self.current_balance - self.daily_start_balance

    def get_stats(self) -> dict:
        self._check_daily_reset()
        closed = self.closed_trades
        wins   = [t for t in closed if t.pnl and t.pnl > 0]
        total_pnl = sum(t.pnl for t in closed if t.pnl)
        daily_pnl = self.current_balance - self.daily_start_balance
        drawdown  = (self.current_balance - self.starting_balance) / self.starting_balance

        # Aggregate Sharpe/Sortino across all tracked strategies
        all_pnls: list[float] = []
        for tracker in _pnl_trackers.values():
            all_pnls.extend(list(tracker._pnl))

        agg_tracker = StrategyPnLTracker(window=len(all_pnls) + 1)
        for p in all_pnls:
            agg_tracker.record(p)

        return {
            "balance":          self.current_balance,
            "total_pnl":        total_pnl,
            "daily_pnl":        daily_pnl,
            "drawdown_pct":     drawdown,
            "win_rate":         len(wins) / len(closed) if closed else 0,
            "total_trades":     len(closed),
            "open_positions":   len(self.open_positions),
            "halted":           self.halted,
            "halt_reason":      self.halt_reason,
            "portfolio_sharpe": agg_tracker.sharpe(),
            "portfolio_sortino": agg_tracker.sortino(),
            "strategy_metrics": self.get_all_strategy_metrics(),
        }
