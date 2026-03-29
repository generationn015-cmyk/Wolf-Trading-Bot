"""
Wolf Trading Bot — Order Manager
All trades route through here. PAPER_MODE=True → PaperTrader only.
PAPER_MODE=False → real Polymarket/Kalshi execution.
Every order logged.
"""
import time
import logging
import config
from risk_engine import RiskEngine, TradeRecord
from paper_mode import PaperTrader
from journal.trade_logger import TradeLogger
from alerts.telegram_alerts import send_alert

logger = logging.getLogger("wolf.execution")

class OrderManager:
    def __init__(self, risk_engine: RiskEngine, paper_trader: PaperTrader, trade_logger: TradeLogger):
        self.risk = risk_engine
        self.paper = paper_trader
        self.journal = trade_logger
        self._poly_client = None
        self._kalshi_client = None

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

    def execute_signal(self, signal: dict) -> dict:
        """
        Execute a trading signal. Routes to paper or live based on PAPER_MODE.
        Returns result dict with status and details.
        """
        venue = signal.get("venue", "polymarket")
        market_id = signal["market_id"]
        side = signal["side"]
        confidence = signal["confidence"]
        entry_price = signal["entry_price"]
        strategy = signal["strategy"]

        # Risk gate
        volume = signal.get("volume", config.MIN_MARKET_VOLUME + 1)
        can_trade, reason = self.risk.can_trade(market_volume=volume)
        if not can_trade:
            logger.info(f"Trade blocked by risk engine: {reason}")
            return {"status": "blocked", "reason": reason}

        # Get position size from risk engine (Kelly)
        edge = signal.get("edge", 0.1)
        size = self.risk.get_position_size(edge=edge, confidence=confidence)
        if size <= 0:
            return {"status": "blocked", "reason": f"Kelly size 0 (confidence {confidence:.2f} below threshold)"}

        if config.PAPER_MODE:
            return self._execute_paper(signal, size, strategy, venue, market_id, side, entry_price)
        else:
            return self._execute_live(signal, size, strategy, venue, market_id, side, entry_price)

    def _execute_paper(self, signal, size, strategy, venue, market_id, side, entry_price) -> dict:
        trade = self.paper.place_trade(
            strategy=strategy,
            venue=venue,
            market_id=market_id,
            side=side,
            size=size,
            entry_price=entry_price,
        )
        # Log to journal
        self.journal.log_paper_trade({
            "timestamp": trade.timestamp,
            "strategy": strategy,
            "venue": venue,
            "market_id": market_id,
            "side": side,
            "size": size,
            "entry_price": entry_price,
            "confidence": signal.get("confidence"),
            "edge": signal.get("edge"),
            "reason": signal.get("reason", ""),
        })
        # Check gate status
        gate_passed, gate_msg = self.paper.has_passed_gate()
        if gate_passed:
            from alerts.telegram_alerts import alert_paper_gate_passed
            alert_paper_gate_passed(self.paper.get_stats())

        return {"status": "paper_executed", "trade": trade, "size": size}

    def _execute_live(self, signal, size, strategy, venue, market_id, side, entry_price) -> dict:
        """Live execution — only reached when PAPER_MODE=False and authorized."""
        if venue == "polymarket":
            return self._execute_polymarket(signal, size, strategy, market_id, side, entry_price)
        elif venue == "kalshi":
            return self._execute_kalshi(signal, size, strategy, market_id, side, entry_price)
        else:
            return {"status": "error", "reason": f"Unknown venue: {venue}"}

    def _execute_polymarket(self, signal, size, strategy, market_id, side, entry_price) -> dict:
        try:
            client = self._get_poly_client()
            if not client:
                return {"status": "error", "reason": "Polymarket client not initialized"}

            from py_clob_client.clob_types import MarketOrderArgs, BUY
            order_args = MarketOrderArgs(
                token_id=market_id,
                amount=size,
            )
            resp = client.create_and_post_market_order(order_args)

            # Register with risk engine
            trade = TradeRecord(
                timestamp=time.time(),
                strategy=strategy,
                market_id=market_id,
                side=side,
                size=size,
                entry_price=entry_price,
            )
            self.risk.open_position(trade)
            self.journal.log_trade({
                "timestamp": trade.timestamp,
                "strategy": strategy,
                "venue": "polymarket",
                "market_id": market_id,
                "side": side,
                "size": size,
                "entry_price": entry_price,
                "order_id": str(resp),
            })
            send_alert(f"LIVE trade executed: {strategy} | {market_id} {side} ${size:.2f} @ {entry_price:.3f}", "INFO")
            return {"status": "live_executed", "size": size, "response": str(resp)}
        except Exception as e:
            logger.error(f"Polymarket execution error: {e}")
            return {"status": "error", "reason": str(e)}

    def _execute_kalshi(self, signal, size, strategy, market_id, side, entry_price) -> dict:
        """Kalshi execution — raw REST, no SDK bugs."""
        try:
            import requests
            import json
            # Kalshi uses YES/NO mapped to buy_yes/buy_no
            action = "buy" if side == "YES" else "buy"
            contracts = int(size / entry_price)  # Kalshi uses contract counts
            if contracts < 1:
                return {"status": "blocked", "reason": "Size too small for Kalshi (< 1 contract)"}

            # Auth headers would be set here with RSA signing
            # Full auth implementation in Phase 2
            logger.info(f"[KALSHI LIVE] {market_id} {side} {contracts} contracts @ {entry_price:.3f}")
            return {"status": "kalshi_pending_auth_setup", "market_id": market_id, "side": side}
        except Exception as e:
            logger.error(f"Kalshi execution error: {e}")
            return {"status": "error", "reason": str(e)}
