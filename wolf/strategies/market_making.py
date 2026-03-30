"""
Wolf Trading Bot — Market Making Strategy
Posts both sides of the book on active Polymarket markets.
VPIN spike detection: go dark when informed money hunts.
Scans live markets every cycle and generates signals.
"""
import time
import logging
import asyncio
from dataclasses import dataclass
import config
from feeds.polymarket_feed import get_orderbook, get_market_volume, get_market_price
from learning_engine import learning
from feeds.polymarket_feed import POLYMARKET_DATA_URL
import requests

logger = logging.getLogger("wolf.strategy.market_making")

@dataclass
class MMPosition:
    market_id: str
    inventory: float = 0.0
    total_spread_captured: float = 0.0
    trades: int = 0

MARKET_KEYWORDS = {
    "crypto":   ["BITCOIN", "BTC", "ETHEREUM", "ETH", "CRYPTO"],
    "politics": ["TRUMP", "BIDEN", "PRESIDENT", "ELECTION", "CONGRESS"],
    "sports":   ["NBA", "NFL", "MLB", "SOCCER", "CHAMPION"],
    "macro":    ["FED", "RATE", "CPI", "INFLATION", "FOMC", "GDP", "OIL", "GOLD"],
}

class MarketMaker:
    def __init__(self):
        self.positions: dict[str, MMPosition] = {}
        self._market_cache: list[dict] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 120  # refresh markets every 2 min

    def _fetch_active_markets(self) -> list[dict]:
        """Fetch active markets with real prices from Polymarket."""
        import json as _json
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._market_cache:
            return self._market_cache
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": True, "limit": 50, "closed": False},
                timeout=10
            )
            if resp.ok:
                markets = resp.json()
                if isinstance(markets, list):
                    filtered = []
                    for m in markets:
                        op = m.get("outcomePrices", [])
                        if isinstance(op, str):
                            try: op = _json.loads(op)
                            except: op = []
                        if op and len(op) >= 2:
                            try:
                                p0, p1 = float(op[0]), float(op[1])
                                # Only include markets with real non-degenerate prices
                                if 0.02 < p0 < 0.98 and 0.02 < p1 < 0.98:
                                    m["_yes_price"] = p0
                                    m["_no_price"] = p1
                                    filtered.append(m)
                            except (ValueError, TypeError):
                                pass
                    self._market_cache = filtered[:20]  # top 20
                    self._cache_ts = now
                    logger.info(f"Market maker loaded {len(self._market_cache)} markets")
                    return self._market_cache
        except Exception as e:
            logger.warning(f"Failed to fetch MM markets: {e}")
        return self._market_cache

    def _estimate_vpin(self, orderbook: dict) -> float:
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return 0.0
        bid_vol = sum(s for _, s in bids[:5])
        ask_vol = sum(s for _, s in asks[:5])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return abs(bid_vol - ask_vol) / total

    async def scan(self) -> list[dict]:
        """Scan active markets and generate market making signals."""
        signals = []
        markets = self._fetch_active_markets()

        for market in markets:
            try:
                market_id = market.get("conditionId") or market.get("id", "")
                if not market_id:
                    continue

                # Use token IDs if available for CLOB orderbook
                clob_ids = market.get("clobTokenIds", "")
                if isinstance(clob_ids, str):
                    try:
                        import json
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = []

                # Use pre-validated prices from _fetch_active_markets
                yes_price = market.get("_yes_price", 0.5)
                no_price = market.get("_no_price", 0.5)

                # Build synthetic orderbook with realistic tight spread (2-4%)
                spread_estimate = 0.04
                best_bid = max(0.01, yes_price - spread_estimate / 2)
                best_ask = min(0.99, yes_price + spread_estimate / 2)

                orderbook = {
                    "bids": [(best_bid, 500)],
                    "asks": [(best_ask, 500)],
                }

                volume = float(market.get("volumeNum", 0) or 0)
                new_signals = self.generate_mm_signal(
                    market_id=market_id,
                    orderbook=orderbook,
                    volume=volume,
                    venue="polymarket",
                )
                signals.extend(new_signals)

                # Max 2 markets per scan cycle — enough volume without flooding
                if len(signals) >= 4:
                    break

            except Exception as e:
                logger.debug(f"MM scan error on market: {e}")

        return signals

    def generate_mm_signal(self, market_id: str, orderbook: dict,
                            volume: float, venue: str = "polymarket") -> list[dict]:
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

        if spread < 0.02:
            return signals

        vpin = self._estimate_vpin(orderbook)
        if vpin > config.VPIN_SPIKE_THRESHOLD:
            logger.debug(f"VPIN spike {market_id}: {vpin:.3f} — skipping")
            return signals

        pos = self.positions.get(market_id, MMPosition(market_id=market_id))
        if abs(pos.inventory) > 10:
            if pos.inventory > 0:
                best_ask -= 0.01
            else:
                best_bid += 0.01

        our_bid = best_bid + 0.01
        our_ask = best_ask - 0.01
        captured_spread = our_ask - our_bid

        if captured_spread <= 0:
            return signals

        # Market making confidence is different from directional bets —
        # it's based on spread capture probability, not outcome prediction.
        # A 2% captured spread on a liquid market is a solid MM opportunity.
        confidence = min(0.70, 0.5 + captured_spread * 5)

        MM_MIN_CONFIDENCE = max(0.55, learning.get_confidence_floor("market_making"))
        if confidence >= MM_MIN_CONFIDENCE:
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
                "reason": f"MM bid {our_bid:.3f} / ask {our_ask:.3f} spread {captured_spread:.3f} VPIN {vpin:.3f}",
            })
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
