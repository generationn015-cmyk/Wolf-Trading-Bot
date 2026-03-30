"""
Wolf Trading Bot — Latency Arbitrage Strategy
Monitors Binance price feed vs Polymarket implied probability.

Calibrated from verified $1.4M wallet analysis:
- Entry fires 9–16 SECONDS after a >0.11% BTC price move on Binance
- NOT before — the lag window is where the edge lives
- Signal-to-order must be < 40ms (in live mode via CLOB)
- The price gap between Binance and Polymarket closes in ~20 seconds

Why 9–16 seconds? 
- <9s: Polymarket market makers haven't updated yet — you might get filled
  at old price, but the MM bots snap it back immediately
- 9–16s: The lag window — large traders see the Binance move and start
  hitting Polymarket. Price is moving your way. Best fill, best momentum.
- >20s: Gap already closed. No edge.
"""
import time
import logging
import asyncio
import config
from feeds.binance_feed import btc_feed, eth_feed
from feeds.polymarket_feed import get_active_btc_markets, get_market_price, get_market_volume
from learning_engine import learning

logger = logging.getLogger("wolf.strategy.latency_arb")

# Calibrated from $1.4M wallet — do not change without backtesting
ENTRY_DELAY_MIN_SEC = 9    # minimum seconds to wait after move detected
ENTRY_DELAY_MAX_SEC = 16   # fire before this or the edge is gone
MIN_BTC_MOVE_PCT    = 0.0011  # 0.11% minimum move to trigger scan


class LatencyArb:
    def __init__(self):
        self._signals: list[dict] = []
        self._last_btc_price: float = 0.0
        self._last_scan: float = 0.0
        self._pending_signals: list[dict] = []  # signals waiting for entry delay
        self._move_detected_at: float = 0.0    # when we first saw the move

    async def scan(self) -> list[dict]:
        """
        Scan for latency arb. Implements the 9–16 second entry delay.
        Step 1: Detect >0.11% move → record timestamp, queue pending signal
        Step 2: On next scan 9–16s later → fire queued signals
        """
        signals = []
        now = time.time()

        # ── Fire any pending signals that are now in the 9–16s window ────────
        ready = []
        still_waiting = []
        for ps in self._pending_signals:
            age = now - ps["_move_detected_at"]
            if ENTRY_DELAY_MIN_SEC <= age <= ENTRY_DELAY_MAX_SEC:
                ready.append(ps)
            elif age < ENTRY_DELAY_MIN_SEC:
                still_waiting.append(ps)
            # else: >16s — drop it, edge is gone
        self._pending_signals = still_waiting
        if ready:
            logger.info(f"Latency arb: firing {len(ready)} delayed signals ({ENTRY_DELAY_MIN_SEC}–{ENTRY_DELAY_MAX_SEC}s window)")
        signals.extend(ready)

        btc_price = btc_feed.get_current_price()
        if btc_price == 0:
            return signals  # Feed not ready yet
        # In paper mode allow up to 30s staleness; live mode keeps 500ms
        max_age = 30000 if config.PAPER_MODE else 3000  # REST polling: 2s interval, 3s tolerance
        if not btc_feed.is_fresh(max_age_ms=max_age):
            return signals

        # ── Detect new moves and queue pending signals ────────────────────────
        if self._last_btc_price > 0:
            pct_move = abs(btc_price - self._last_btc_price) / self._last_btc_price
            if pct_move >= MIN_BTC_MOVE_PCT:
                logger.info(
                    f"BTC move detected: ${self._last_btc_price:,.0f} → ${btc_price:,.0f} "
                    f"({pct_move:.3%}) — queuing latency arb, fires in {ENTRY_DELAY_MIN_SEC}–{ENTRY_DELAY_MAX_SEC}s"
                )
                self._move_detected_at = now

        self._last_btc_price = btc_price

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
                    # Queue with move timestamp — fires in 9–16s
                    signal["_move_detected_at"] = self._move_detected_at or time.time()
                    self._pending_signals.append(signal)

            except Exception as e:
                logger.warning(f"Error scanning market {market}: {e}")

        self._last_scan = time.time()
        return signals  # Returns previously-queued signals that hit the 9–16s window

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

        real_higher = btc_price > threshold * (1 + MIN_BTC_MOVE_PCT)  # 0.11% above
        real_lower  = btc_price < threshold * (1 - MIN_BTC_MOVE_PCT)  # 0.11% below

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
