"""
Lighter.xyz Feed Module — WebSocket + REST via lighter-sdk
"""
import logging
logger = logging.getLogger("lighter.feed")

class LighterFeed:
    def __init__(self):
        self.connected = False
        self.orderbooks = {}
        self.funding = {}

    async def connect(self):
        logger.info("Lighter feed connecting (stub)")
        self.connected = True

    async def get_orderbook(self, market: str) -> dict:
        return self.orderbooks.get(market, {"bids": [], "asks": []})

    async def get_funding_rate(self, market: str) -> dict:
        return self.funding.get(market, {"rate": 0.0, "next_settlement": 0})

    async def get_candles(self, market: str, resolution: str = "5m", limit: int = 200) -> list:
        return []

    async def place_order(self, market: str, side: str, size: float, price: float = None) -> dict:
        logger.info(f"ORDER: {side} {size} {market} @ {price or 'MARKET'}")
        return {"id": "stub", "status": "pending"}
