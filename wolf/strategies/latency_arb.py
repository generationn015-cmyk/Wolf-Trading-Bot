"""
Wolf Trading Bot — Latency Arbitrage Strategy
Monitors Binance price feed vs Polymarket implied probability.
Fires signal when divergence > LATENCY_ARB_THRESHOLD (0.3%).
Core strategy: the edge that built $313 → $2.38M.
"""
import time
import logging
import asyncio
import config
from feeds.binance_feed import btc_feed, eth_feed
from feeds.polymarket_feed import get_active_btc_markets, get_market_price, get_market_volume
from learning_engine import learning

logger = logging.getLogger("wolf.strategy.latency_arb")

class LatencyArb:
    def __init__(self):
        self._signals: list[dict] = []
        self._last_btc_price: float = 0.0
        self._last_scan: float = 0.0

    async def scan(self) -> list[dict]:
        """Scan for latency arb opportunities. Returns list of signals."""
        signals = []

        btc_price = btc_feed.get_current_price()
        if btc_price == 0:
            return signals  # Feed not ready yet
        # In paper mode allow up to 30s staleness; live mode keeps 500ms
        max_age = 30000 if config.PAPER_MODE else 500
        if not btc_feed.is_fresh(max_age_ms=max_age):
            return signals

        markets = get_active_btc_markets()
        for market in markets:
            try:
                market_id = market.get("conditionId") or market.get("id", "")
                if not market_id:
                    continue

                volume = get_market_volume(market_id)
                if volume < config.MIN_MARKET_VOLUME:
                    continue

                yes_price, no_price = get_market_price(market_id)
                question = market.get("question", "")

                # Determine direction from question context
                signal = self._compute_signal(
                    question=question,
                    btc_price=btc_price,
                    yes_price=yes_price,
                    market_id=market_id,
                    volume=volume,
                )
                if signal:
                    signals.append(signal)

            except Exception as e:
                logger.warning(f"Error scanning market {market}: {e}")

        self._last_scan = time.time()
        return signals

    def _compute_signal(self, question: str, btc_price: float,
                        yes_price: float, market_id: str, volume: float) -> dict | None:
        """
        Detect if Polymarket price lags real price.
        For 'will BTC be higher than X?' markets:
        - If BTC is clearly above threshold but YES is underpriced → buy YES
        - If BTC is clearly below threshold but NO is underpriced → buy NO
        """
        q_upper = question.upper()
        threshold = self._extract_threshold(q_upper)
        if threshold is None:
            return None

        real_higher = btc_price > threshold * 1.003  # 0.3% above
        real_lower = btc_price < threshold * 0.997   # 0.3% below

        if real_higher and yes_price < (1.0 - config.LATENCY_ARB_THRESHOLD):
            edge = (1.0 - config.LATENCY_ARB_THRESHOLD) - yes_price
            confidence = min(0.95, 0.5 + edge * 5)
            if confidence >= max(config.MIN_CONFIDENCE, learning.get_confidence_floor("latency_arb")):
                return {
                    "strategy": "latency_arb",
                    "venue": "polymarket",
                    "market_id": market_id,
                    "side": "YES",
                    "edge": edge,
                    "confidence": confidence,
                    "entry_price": yes_price,
                    "volume": volume,
                    "timestamp": time.time(),
                    "reason": f"BTC ${btc_price:,.0f} > threshold ${threshold:,.0f}, YES underpriced at {yes_price:.3f}",
                }

        if real_lower and no_price < (1.0 - config.LATENCY_ARB_THRESHOLD):
            edge = (1.0 - config.LATENCY_ARB_THRESHOLD) - no_price
            confidence = min(0.95, 0.5 + edge * 5)
            if confidence >= max(config.MIN_CONFIDENCE, learning.get_confidence_floor("latency_arb")):
                return {
                    "strategy": "latency_arb",
                    "venue": "polymarket",
                    "market_id": market_id,
                    "side": "NO",
                    "edge": edge,
                    "confidence": confidence,
                    "entry_price": no_price,
                    "volume": volume,
                    "timestamp": time.time(),
                    "reason": f"BTC ${btc_price:,.0f} < threshold ${threshold:,.0f}, NO underpriced at {no_price:.3f}",
                }

        return None

    def _extract_threshold(self, question: str) -> float | None:
        """Extract price threshold from question text."""
        import re
        # Match patterns like "$95,000" or "95000" or "95k"
        patterns = [
            r'\$?([\d,]+)k\b',   # 95k
            r'\$?([\d,]+)',       # $95,000 or 95000
        ]
        for pattern in patterns:
            matches = re.findall(pattern, question)
            for m in matches:
                try:
                    val = float(m.replace(",", ""))
                    if pattern.endswith(r'k\b'):
                        val *= 1000
                    # Sanity check: BTC typically between $10k and $500k
                    if 10000 < val < 500000:
                        return val
                except ValueError:
                    continue
        return None
