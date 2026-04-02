"""
Wolf Trading Bot — Order Manager
All trades route through here. PAPER_MODE=True → PaperTrader only.
PAPER_MODE=False → real Polymarket/Kalshi execution.

Upgrades over v1:
  - BoundedCache replaces unbounded dict (no memory leak on long runs)
  - Cross-strategy correlation guard (event fingerprint dedup)
  - days_to_expiry passed through to get_position_size() for time-decay
  - volatility_30m passed through from Binance feed for vol-adjusted Kelly
"""
import time
import logging
from collections import OrderedDict
import config
from risk_engine import RiskEngine, TradeRecord
from paper_mode import PaperTrader
from journal.trade_logger import TradeLogger
from alerts.telegram_alerts import send_alert

logger = logging.getLogger("wolf.execution")

# Window in seconds within which the same (strategy, market_id, side) is deduplicated
EXEC_DEDUP_WINDOW = 300  # 5 minutes

# Max correlated positions sharing the same event fingerprint across all strategies
MAX_CORRELATED_POSITIONS = 2


# ── Bounded LRU dedup cache ───────────────────────────────────────────────────

class BoundedCache:
    """
    Fixed-capacity ordered dict used as an LRU dedup cache.
    Replaces the plain dict that could grow unboundedly over long runs.

    When capacity is reached, the oldest entry (LRU) is evicted automatically.
    Expired entries are lazily evicted on lookup.
    """

    def __init__(self, maxsize: int = 2000, ttl: float = EXEC_DEDUP_WINDOW):
        self._d: OrderedDict[tuple, float] = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl

    def is_recent(self, key: tuple) -> bool:
        """Returns True if key was set within TTL (i.e., this is a duplicate)."""
        ts = self._d.get(key)
        if ts is None:
            return False
        if time.time() - ts < self.ttl:
            self._d.move_to_end(key)  # Refresh recency on hit
            return True
        # Expired — lazy evict
        del self._d[key]
        return False

    def set(self, key: tuple):
        """Mark key as recently executed."""
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = time.time()
        # Evict oldest if over capacity
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)

    def seed(self, key: tuple, ts: float = None):
        """Seed with a specific timestamp (used on startup from DB)."""
        self._d[key] = ts or time.time()
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)


class OrderManager:
    def __init__(
        self,
        risk_engine: RiskEngine,
        paper_trader: PaperTrader,
        trade_logger: TradeLogger,
    ):
        self.risk    = risk_engine
        self.paper   = paper_trader
        self.journal = trade_logger
        self._poly_client   = None
        self._kalshi_client = None

        # Bounded LRU dedup cache — never grows past 2000 entries
        self._exec_cache = BoundedCache(maxsize=2000, ttl=EXEC_DEDUP_WINDOW)
        self._gate_alerted = False

        # Seed dedup from DB so restarts don't re-enter open positions
        self._seed_dedup_from_db(trade_logger)

    # ── Dedup ─────────────────────────────────────────────────────────────────

    def _is_duplicate(self, strategy: str, market_id: str, side: str) -> bool:
        key = (strategy, market_id, side)
        if self._exec_cache.is_recent(key):
            return True
        self._exec_cache.set(key)
        return False

    def _seed_dedup_from_db(self, journal: TradeLogger):
        """
        On startup mark all currently-open positions as recently executed.
        Prevents re-entry burst when Wolf restarts with existing open positions.
        """
        try:
            import sqlite3
            with sqlite3.connect(journal.db_path) as conn:
                rows = conn.execute(
                    "SELECT strategy, market_id, side FROM paper_trades "
                    "WHERE resolved=0 AND simulated=0"
                ).fetchall()
            now = time.time()
            for strategy, market_id, side in rows:
                self._exec_cache.seed((strategy, market_id, side), now)
            if rows:
                logger.info(f"Dedup cache seeded: {len(rows)} open positions protected")
        except Exception as e:
            logger.debug(f"Dedup seed skipped: {e}")

    # ── Correlation guard ─────────────────────────────────────────────────────

    @staticmethod
    def _event_fingerprint(market_id: str, reason: str) -> str:
        """
        Coarse event key derived from the signal reason text.
        First 40 chars of the lowercase reason approximates the underlying event.
        Falls back to first 20 chars of market_id if reason is empty.
        """
        if reason:
            return reason[:40].lower().strip()
        return market_id[:20].lower()

    def _count_correlated_positions(self, fingerprint: str) -> int:
        """Count open positions sharing this event fingerprint across all strategies."""
        if not self.paper or not self.paper.open_trades:
            return 0
        count = 0
        for t in self.paper.open_trades:
            t_reason = getattr(t, "reason", "") or ""
            t_fp = self._event_fingerprint(t.market_id, t_reason)
            if t_fp == fingerprint:
                count += 1
        return count

    # ── Main execution entry point ────────────────────────────────────────────

    def execute_signal(self, signal: dict) -> dict:
        """
        Route a trading signal to paper or live execution with full dedup + risk gating.
        Returns a status dict: {"status": str, ...}
        """
        venue        = signal.get("venue", "polymarket")
        market_id    = signal["market_id"]
        side         = signal["side"]
        confidence   = signal["confidence"]
        entry_price  = signal["entry_price"]
        strategy     = signal["strategy"]
        reason       = signal.get("reason", "")
        days_to_exp  = signal.get("days_to_expiry")
        market_end   = signal.get("market_end", 0.0) or 0.0

        # ── Dedup gate ────────────────────────────────────────────────────────
        if self._is_duplicate(strategy, market_id, side):
            return {"status": "dedup_blocked", "reason": f"Executed within {EXEC_DEDUP_WINDOW}s"}

        # ── Market expiry guard ───────────────────────────────────────────────
        if market_end > 0 and market_end < time.time():
            logger.warning(f"[GUARD] Blocked expired market {market_id[:20]}")
            return {"status": "blocked", "reason": "market_already_expired"}

        # ── Correlation guard ─────────────────────────────────────────────────
        # Prevents Wolf from loading up on the same underlying event via multiple
        # strategies simultaneously (e.g. BTC YES via value_bet + copy_trading).
        fingerprint = self._event_fingerprint(market_id, reason)
        corr_count  = self._count_correlated_positions(fingerprint)
        if corr_count >= MAX_CORRELATED_POSITIONS:
            logger.debug(
                f"[CORR] Blocked {strategy}/{side} — {corr_count} correlated positions "
                f"already open on '{fingerprint[:30]}'"
            )
            return {
                "status": "corr_blocked",
                "reason": f"{corr_count} correlated positions on same event",
            }

        # ── Per-strategy slot cap ─────────────────────────────────────────────
        strat_open = (
            sum(1 for t in self.paper.open_trades if t.strategy == strategy)
            if self.paper else 0
        )
        total_slots = (
            config.MAX_OPEN_POSITIONS_PAPER if config.PAPER_MODE else config.MAX_OPEN_POSITIONS
        )
        max_per_strategy = max(6, total_slots * 3 // 4)  # 75% of total per strategy
        if strat_open >= max_per_strategy:
            return {
                "status": "blocked",
                "reason": f"Strategy slot cap: {strategy} has {strat_open}/{max_per_strategy}",
            }

        # ── Duration-based long-play cap ──────────────────────────────────────
        if days_to_exp is not None and days_to_exp > 7:
            long_open = (
                sum(1 for t in self.paper.open_trades if getattr(t, "days_to_expiry", 0) > 7)
                if self.paper else 0
            )
            if long_open >= 4:
                return {
                    "status": "blocked",
                    "reason": f"Long-play cap: {long_open}/4 slots used for >7d trades",
                }

        # ── Risk gate ─────────────────────────────────────────────────────────
        volume = signal.get("volume", config.MIN_MARKET_VOLUME + 1)
        can_trade, reason_str = self.risk.can_trade(market_volume=volume)
        if not can_trade:
            logger.info(f"Trade blocked by risk engine: {reason_str}")
            return {"status": "blocked", "reason": reason_str}

        # ── Position sizing (Kelly + vol + time-decay) ────────────────────────
        # Pull current BTC volatility for vol-adjusted sizing
        vol_30m = None
        try:
            from feeds.binance_feed import btc_feed
            vol_30m = btc_feed.get_volatility_30m()
        except Exception:
            pass

        size = self.risk.get_position_size(
            edge=signal.get("edge", 0.1),
            confidence=confidence,
            entry_price=entry_price,
            volatility_30m=vol_30m,
            days_to_expiry=days_to_exp,
        )
        if size <= 0:
            return {
                "status": "blocked",
                "reason": f"Kelly size 0 (conf={confidence:.2f} below threshold)",
            }

        # ── Route to paper or live ────────────────────────────────────────────
        if config.PAPER_MODE:
            return self._execute_paper(
                signal, size, strategy, venue, market_id, side,
                entry_price, market_end, days_to_exp or 0.0
            )
        else:
            return self._execute_live(signal, size, strategy, venue, market_id, side, entry_price)

    # ── Paper execution ───────────────────────────────────────────────────────

    def _execute_paper(
        self, signal, size, strategy, venue, market_id, side,
        entry_price, market_end, days_to_expiry
    ) -> dict:
        trade = self.paper.place_trade(
            strategy=strategy, venue=venue, market_id=market_id,
            side=side, size=size, entry_price=entry_price,
            market_end=market_end, days_to_expiry=days_to_expiry,
        )
        inserted = self.journal.log_paper_trade({
            "timestamp":      trade.timestamp,
            "strategy":       strategy,
            "venue":          venue,
            "market_id":      market_id,
            "side":           side,
            "size":           size,
            "entry_price":    entry_price,
            "confidence":     signal.get("confidence"),
            "edge":           signal.get("edge"),
            "reason":         signal.get("reason", ""),
            "market_end":     market_end,
            "days_to_expiry": days_to_expiry,
            "sub_strategy":   signal.get("sub_strategy"),
            "tp_price":       signal.get("tp_price"),
            "sl_price":       signal.get("sl_price"),
        })
        if not inserted:
            if trade in self.paper.open_trades:
                self.paper.open_trades.remove(trade)
            return {"status": "dedup_blocked", "reason": "DB dedup"}

        # Sync risk engine
        _tr = TradeRecord(
            timestamp=time.time(), market_id=market_id, strategy=strategy,
            side=side, size=size, entry_price=entry_price, status="open",
        )
        self.risk.open_position(_tr)

        # Entry alert
        from alerts.telegram_alerts import alert_trade_entry
        alert_trade_entry(
            strategy=strategy,
            market=signal.get("reason", trade.market_id)[:80],
            side=side,
            size=size,
            entry_price=entry_price,
            confidence=signal.get("confidence", 0),
            paper=config.PAPER_MODE,
        )

        # Gate check (one-time)
        if not self._gate_alerted:
            gate_passed, _ = self.paper.has_passed_gate()
            if gate_passed:
                self._gate_alerted = True
                from alerts.telegram_alerts import alert_paper_gate_passed, alert_wr_threshold
                alert_paper_gate_passed(self.paper.get_stats())
                stats = self.paper.get_stats()
                alert_wr_threshold(stats["win_rate"], stats["total_trades"], stats["total_pnl"])

        return {"status": "paper_executed", "trade": trade, "size": size}

    # ── Live execution ────────────────────────────────────────────────────────

    def _execute_live(self, signal, size, strategy, venue, market_id, side, entry_price) -> dict:
        if venue == "polymarket":
            return self._execute_polymarket(signal, size, strategy, market_id, side, entry_price)
        elif venue == "kalshi":
            return self._execute_kalshi(signal, size, strategy, market_id, side, entry_price)
        return {"status": "error", "reason": f"Unknown venue: {venue}"}

    def _get_poly_client(self):
        if self._poly_client is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                from py_clob_client.constants import POLYGON
                creds = ApiCreds(
                    api_key=config.POLYMARKET_API_KEY,
                    api_secret=config.POLYMARKET_API_SECRET,
                    api_passphrase=config.POLYMARKET_API_PASSPHRASE,
                )
                self._poly_client = ClobClient(
                    config.POLYMARKET_CLOB_URL,
                    key=config.POLYMARKET_PRIVATE_KEY,
                    chain_id=POLYGON,
                    creds=creds,
                )
            except Exception as e:
                logger.error(f"Failed to init Polymarket client: {e}")
        return self._poly_client

    def _execute_polymarket(self, signal, size, strategy, market_id, side, entry_price) -> dict:
        try:
            client = self._get_poly_client()
            if not client:
                return {"status": "error", "reason": "Polymarket client not initialized"}
            from py_clob_client.clob_types import MarketOrderArgs
            order_args = MarketOrderArgs(token_id=market_id, amount=size)
            resp = client.create_and_post_market_order(order_args)
            trade = TradeRecord(
                timestamp=time.time(), strategy=strategy,
                market_id=market_id, side=side, size=size, entry_price=entry_price,
            )
            self.risk.open_position(trade)
            self.journal.log_trade({
                "timestamp": trade.timestamp, "strategy": strategy,
                "venue": "polymarket", "market_id": market_id,
                "side": side, "size": size, "entry_price": entry_price,
                "order_id": str(resp),
            })
            send_alert(
                f"⚡ LIVE: {strategy} | {market_id[:20]}… {side} "
                f"${size:.2f} @ {entry_price:.3f}", "INFO"
            )
            return {"status": "live_executed", "size": size, "response": str(resp)}
        except Exception as e:
            logger.error(f"Polymarket execution error: {e}")
            return {"status": "error", "reason": str(e)}

    def _execute_kalshi(self, signal, size, strategy, market_id, side, entry_price) -> dict:
        try:
            contracts = int(size / entry_price)
            if contracts < 1:
                return {"status": "blocked", "reason": "Size too small for Kalshi (< 1 contract)"}
            logger.info(f"[KALSHI LIVE] {market_id} {side} {contracts} contracts @ {entry_price:.3f}")
            return {"status": "kalshi_pending_auth_setup", "market_id": market_id, "side": side}
        except Exception as e:
            logger.error(f"Kalshi execution error: {e}")
            return {"status": "error", "reason": str(e)}
