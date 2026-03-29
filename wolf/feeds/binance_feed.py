"""
Wolf Trading Bot — Binance WebSocket Feed
Real-time BTC/ETH price feed. No API key needed — public stream.
Auto-reconnects on disconnect.
"""
import asyncio
import json
import time
import logging
import websockets
import config

logger = logging.getLogger("wolf.feeds.binance")

class BinanceFeed:
    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.lower()
        self.ws_url = f"wss://stream.binance.com:9443/ws/{self.symbol}@trade"
        self._price: float = 0.0
        self._last_update: float = 0.0
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        logger.info(f"Binance feed started: {self.symbol}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _connect_loop(self):
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    backoff = 1
                    logger.info(f"Binance WS connected: {self.symbol}")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        self._price = float(data["p"])
                        self._last_update = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance WS error: {e} — reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def get_current_price(self) -> float:
        return self._price

    def get_price_age_ms(self) -> float:
        if self._last_update == 0:
            return float("inf")
        return (time.time() - self._last_update) * 1000

    def is_fresh(self, max_age_ms: float = 500) -> bool:
        return self.get_price_age_ms() < max_age_ms

# Singleton feeds
btc_feed = BinanceFeed("btcusdt")
eth_feed = BinanceFeed("ethusdt")
