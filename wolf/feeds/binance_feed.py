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
    # Binance blocks VPS/cloud IPs in some regions (HTTP 451).
    # Fall back to Binance US or use REST polling as backup.
    WS_URLS = [
        "wss://stream.binance.us:9443/ws/{symbol}@trade",   # Binance US (usually reachable from VPS)
        "wss://stream.binance.com:443/ws/{symbol}@trade",   # Binance global alt port
    ]
    REST_URL = "https://api.binance.us/api/v3/ticker/price"

    def __init__(self, symbol: str = "btcusdt"):
        self.symbol = symbol.lower()
        self.ws_url = self.WS_URLS[0].format(symbol=self.symbol)
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

    async def _rest_fallback(self):
        """Poll Binance US REST API as fallback when WS is blocked."""
        import requests
        sym = self.symbol.upper()
        try:
            r = requests.get(self.REST_URL, params={"symbol": sym}, timeout=5)
            if r.ok:
                self._price = float(r.json()["price"])
                self._last_update = time.time()
        except Exception as e:
            logger.debug(f"REST fallback error for {sym}: {e}")

    async def _connect_loop(self):
        backoff = 1
        url_idx = 0
        consecutive_failures = 0
        while self._running:
            try:
                url = self.WS_URLS[url_idx % len(self.WS_URLS)].format(symbol=self.symbol)
                async with websockets.connect(url, ping_interval=20) as ws:
                    backoff = 1
                    consecutive_failures = 0
                    logger.info(f"Binance WS connected: {self.symbol} via {url}")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        self._price = float(data["p"])
                        self._last_update = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                url_idx += 1  # Try next URL on next attempt
                if consecutive_failures >= len(self.WS_URLS) * 2:
                    # All WS endpoints failing — fall back to REST polling
                    logger.warning(f"Binance WS unavailable for {self.symbol} — using REST fallback")
                    while self._running:
                        await self._rest_fallback()
                        await asyncio.sleep(10)  # Poll every 10s
                    break
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
