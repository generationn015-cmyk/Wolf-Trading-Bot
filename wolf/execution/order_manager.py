"""
Wolf Trading Bot — Order Manager
All trades route through here. PAPER_MODE=True → PaperTrader only.
PAPER_MODE=False → real Polymarket/Kalshi execution.
Every order logged. Dedup enforced at execution layer — no duplicate signals processed.
"""
import time
import logging
import config
from risk_engine import RiskEngine, TradeRecord
from paper_mode import PaperTrader
from journal.trade_logger import TradeLogger
from alerts.telegram_alerts import send_alert

logger = logging.getLogger("wolf.execution")

# Window in seconds within which the same (strategy, market_id, side) is deduplicated
EXEC_DEDUP_WINDOW = 300  # 5 minutes


class OrderManager:
    def __init__(self, risk_engine: RiskEngine, paper_trader: PaperTrader, trade_logger: TradeLogger):
        self.risk    = risk_engine
        self.paper   = paper_trader
        self.journal = trade_logger
        self._poly_client  = None
        self._kalshi_client = None
        # Dedup cache: (strategy, market_id, side) → last execution timestamp
        self._exec_cache: dict[tuple, float] = {}
        self._gate_alerted = False

    def _is_duplicate(self, strategy: str, market_id: str, side: str) -> bool:
        key = (strategy, market_id, side)
        last = self._exec_cache.get(key, 0.0)
        if time.time() - last < EXEC_DEDUP_WINDOW:
            return True
        self._exec_cache[key] = time.time()
        return False

    def _prune_cache(self):
        """Prune stale cache entries to prevent unbounded growth."""
        now = time.time()
        stale = [k for k, v in self._exec_cache.items() if now - v > EXEC_DEDUP_WINDOW * 2]
        for k in stale:
            del self._exec_cache[k]

    def execute_signal(self, signal: dict) -> dict:
        """Route a trading signal to paper or live execution with full dedup + risk gating."""
        venue      = signal.get("venue", "polymarket")
        market_id  = signal["market_id"]
        side       = signal["side"]
        confidence = signal["confidence"]
        entry_price = signal["entry_price"]
        strategy   = signal["strategy"]

        # ── Dedup gate ────────────────────────────────────────────────────────
        if self._is_duplicate(strategy, market_id, side):
            return {"status": "dedup_blocked", "reason": f"Already executed within {EXEC_DEDUP_WINDOW}s"}

        # ── Per-strategy slot cap ─────────────────────────────────────────────
        # No single strategy should monopolize all open positions
        strat_open = sum(
            1 for t in self.paper.open_trades
            if t.strategy == strategy
        ) if self.paper else 0
        # Per-strategy cap: half of total open positions max
        # Prevents any one strategy monopolizing all slots, but scales with MAX_OPEN_POSITIONS
        # Use paper-mode cap when in paper mode — live cap is much smaller and would silently block strategies
        _total_slots = config.MAX_OPEN_POSITIONS_PAPER if config.PAPER_MODE else config.MAX_OPEN_POSITIONS
        max_per = max(4, _total_slots * 3 // 8)  # 37.5% per strategy — prevents monopoly without starving others
        if strat_open >= max_per:
            return {"status": "blocked", "reason": f"Strategy slot cap: {strategy} already has {strat_open}/{max_per} open"}

        # ── Duration-based slot reservation ─────────────────────────────────────
        # Short plays (≤3d) always allowed (up to total cap)
        # Long plays (7-14d) hard-capped at 4 total open — keeps slots for shorter ops
        days_to_expiry = signal.get('days_to_expiry', 0)
        if days_to_expiry > 7:
            long_open = sum(1 for t in self.paper.open_trades
                           if getattr(t, 'days_to_expiry', 0) > 7) if self.paper else 0
            max_long = 4  # never let long plays consume more than 4 slots
            if long_open >= max_long:
                return {'status': 'blocked', 'reason': f'Long-play cap: {long_open}/{max_long} slots used for >7d trades'}

        # ── Risk gate ─────────────────────────────────────────────────────────
        volume = signal.get("volume", config.MIN_MARKET_VOLUME + 1)
        can_trade, reason = self.risk.can_trade(market_volume=volume)
        if not can_trade:
            logger.info(f"Trade blocked by risk engine: {reason}")
            return {"status": "blocked", "reason": reason}

        # ── Position sizing (Kelly) ───────────────────────────────────────────
        edge = signal.get("edge", 0.1)
        size = self.risk.get_position_size(edge=edge, confidence=confidence, entry_price=entry_price)
        if size <= 0:
            return {"status": "blocked",
                    "reason": f"Kelly size 0 (conf={confidence:.2f} below threshold)"}

        self._prune_cache()

        if config.PAPER_MODE:
            market_end = signal.get('market_end', 0.0)
            days_to_expiry = signal.get('days_to_expiry', 0.0)
            return self._execute_paper(signal, size, strategy, venue, market_id, side, entry_price, market_end, days_to_expiry)
        else:
            return self._execute_live(signal, size, strategy, venue, market_id, side, entry_price)

    def _execute_paper(self, signal, size, strategy, venue, market_id, side, entry_price, market_end=0.0, days_to_expiry=0.0) -> dict:
        trade = self.paper.place_trade(
            strategy=strategy, venue=venue, market_id=market_id,
            side=side, size=size, entry_price=entry_price,
            market_end=market_end, days_to_expiry=days_to_expiry,
        )
        inserted = self.journal.log_paper_trade({
            "timestamp":   trade.timestamp,
            "strategy":    strategy,
            "venue":       venue,
            "market_id":   market_id,
            "side":        side,
            "size":        size,
            "entry_price": entry_price,
            "confidence":  signal.get("confidence"),
            "edge":        signal.get("edge"),
            "reason":      signal.get("reason", ""),
        })
        if not inserted:
            if trade in self.paper.open_trades:
                self.paper.open_trades.remove(trade)
            return {"status": "dedup_blocked", "reason": "DB dedup"}

        # Sync risk engine open_positions so total cap is enforced correctly
        from risk_engine import TradeRecord
        import time as _time
        _tr = TradeRecord(timestamp=_time.time(), market_id=market_id, strategy=strategy,
                          side=side, size=size, entry_price=entry_price, status='open')
        self.risk.open_position(_tr)

        # Entry alert (live only — suppressed in paper mode inside alert)
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

        # One-time gate alert
        if not self._gate_alerted:
            gate_passed, _ = self.paper.has_passed_gate()
            if gate_passed:
                self._gate_alerted = True
                from alerts.telegram_alerts import alert_paper_gate_passed, alert_wr_threshold
                alert_paper_gate_passed(self.paper.get_stats())
                stats = self.paper.get_stats()
                alert_wr_threshold(stats["win_rate"], stats["total_trades"], stats["total_pnl"])

        return {"status": "paper_executed", "trade": trade, "size": size}

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
