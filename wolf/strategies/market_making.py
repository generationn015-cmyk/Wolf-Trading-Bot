"""
Wolf Trading Bot — Market Making Strategy
Posts both sides of the book on BTC/ETH + Fed/macro markets.
VPIN spike detection: go dark when informed money hunts.
Inventory rebalancing: auto-correct skew > 10 contracts.
"""
import time
import logging
import asyncio
from dataclasses import dataclass
import config
from feeds.polymarket_feed import get_orderbook, get_market_volume, get_market_price

logger = logging.getLogger("wolf.strategy.market_making")

@dataclass
class MMPosition:
    market_id: str
    inventory: float = 0.0   # positive = long YES, negative = long NO
    total_spread_captured: float = 0.0
    trades: int = 0

# Target markets for market making (extend as Wolf validates more)
TARGET_MARKETS = [
    # These are symbolic — actual IDs fetched from Polymarket API
    # Format: description → real market IDs populated at runtime
    {"tag": "BTC_15M", "keywords": ["BITCOIN", "BTC"], "min_duration_min": 10, "max_duration_min": 20},
    {"tag": "ETH_15M", "keywords": ["ETHEREUM", "ETH"], "min_duration_min": 10, "max_duration_min": 20},
    {"tag": "FED_RATE", "keywords": ["FED", "FEDERAL RESERVE", "RATE CUT", "FOMC"], "min_duration_min": 60, "max_duration_min": None},
    {"tag": "GOLD", "keywords": ["GOLD"], "min_duration_min": 60, "max_duration_min": None},
    {"tag": "OIL", "keywords": ["OIL", "CRUDE"], "min_duration_min": 60, "max_duration_min": None},
]

class MarketMaker:
    def __init__(self):
        self.positions: dict[str, MMPosition] = {}
        self._vpin_tracker: dict[str, list] = {}  # market_id -> recent volume samples
        self._spread_stats: dict[str, float] = {}  # market_id -> recent avg spread

    def _estimate_vpin(self, market_id: str, orderbook: dict) -> float:
        """
        Simplified VPIN estimation.
        Real VPIN requires trade-level data; this uses order book imbalance as proxy.
        Higher = more informed trading activity detected.
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return 0.0

        bid_vol = sum(s for _, s in bids[:5])
        ask_vol = sum(s for _, s in asks[:5])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0

        imbalance = abs(bid_vol - ask_vol) / total
        return imbalance

    async def scan(self) -> list[dict]:
        """Generate market making signals for qualifying markets."""
        signals = []

        # This would normally loop through discovered markets
        # For now returns structure — actual market IDs must come from polymarket_feed discovery
        for market_config in TARGET_MARKETS:
            # In production: fetch markets matching keywords, filter by volume/duration
            # Placeholder: shows the logic shape
            pass

        return signals

    def generate_mm_signal(self, market_id: str, orderbook: dict,
                           volume: float, venue: str = "polymarket") -> list[dict]:
        """
        Given a live orderbook, generate bid+ask orders for market making.
        Returns list of order signals (buy YES + buy NO simultaneously).
        """
        signals = []

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return signals

        if volume < config.MIN_MARKET_VOLUME:
            return signals

        best_bid = bids[0][0] if bids else 0.45
        best_ask = asks[0][0] if asks else 0.55
        spread = best_ask - best_bid

        if spread < 0.02:  # Too tight, not worth making
            return signals

        # VPIN check — if informed money detected, pause
        vpin = self._estimate_vpin(market_id, orderbook)
        if vpin > config.VPIN_SPIKE_THRESHOLD:
            logger.info(f"VPIN spike on {market_id}: {vpin:.3f} — pausing market making")
            return signals

        # Inventory check — rebalance if skewed
        pos = self.positions.get(market_id, MMPosition(market_id=market_id))
        if abs(pos.inventory) > 10:
            logger.info(f"Inventory skew on {market_id}: {pos.inventory} — rebalancing")
            # Adjust quotes to favor the side that reduces inventory
            if pos.inventory > 0:  # Long YES, post more ask (sell YES)
                best_ask = best_ask - 0.01
            else:  # Long NO, post more bid (buy YES)
                best_bid = best_bid + 0.01

        # Spread capture: place inside the spread by 0.01
        our_bid = best_bid + 0.01
        our_ask = best_ask - 0.01
        captured_spread = our_ask - our_bid

        if captured_spread <= 0:
            return signals

        confidence = min(0.70, 0.5 + captured_spread)  # Steady, not spectacular

        if confidence >= config.MIN_CONFIDENCE:
            # Buy YES (bid side)
            signals.append({
                "strategy": "market_making",
                "venue": venue,
                "market_id": market_id,
                "side": "YES",
                "order_type": "limit",
                "limit_price": our_bid,
                "edge": captured_spread / 2,
                "confidence": confidence,
                "entry_price": our_bid,
                "volume": volume,
                "vpin": vpin,
                "timestamp": time.time(),
                "reason": f"MM bid ${our_bid:.3f}, ask ${our_ask:.3f}, spread ${captured_spread:.3f}",
            })
            # Buy NO (ask side mirror)
            signals.append({
                "strategy": "market_making",
                "venue": venue,
                "market_id": market_id,
                "side": "NO",
                "order_type": "limit",
                "limit_price": 1.0 - our_ask,
                "edge": captured_spread / 2,
                "confidence": confidence,
                "entry_price": 1.0 - our_ask,
                "volume": volume,
                "vpin": vpin,
                "timestamp": time.time(),
                "reason": f"MM both sides — VPIN {vpin:.3f}",
            })

        return signals
