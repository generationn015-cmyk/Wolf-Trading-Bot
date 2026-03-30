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
        """Poll Binance US REST API — non-blocking async."""
        sym = self.symbol.upper()
        url = f"{self.REST_URL}?symbol={sym}"
        try:
            if _USE_HTTPX:
                # Truly non-blocking — doesn't stall the event loop
                async with _httpx.AsyncClient(timeout=4.0) as client:
                    r = await client.get(self.REST_URL, params={"symbol": sym})
                    if r.status_code == 200:
                        self._price = float(r.json()["price"])
                        self._last_update = time.time()
            else:
                # Fallback: run blocking requests.get in thread pool
                loop = asyncio.get_event_loop()
                r = await loop.run_in_executor(
                    None,
                    lambda: requests.get(self.REST_URL, params={"symbol": sym}, timeout=4)
                )
                if r.ok:
                    self._price = float(r.json()["price"])
                    self._last_update = time.time()
        except Exception as e:
            logger.debug(f"REST fallback error for {sym}: {e}")

    async def _connect_loop(self):
        """
        Try WS first. If WS connects but immediately closes (VPS IP block — code 1000
        with 0 messages) fall through to REST polling. REST at 2s interval gives
        plenty of freshness for 5-min and 15-min TA candles.
        """
        backoff = 1
        url_idx = 0
        ws_empty_strikes = 0  # Count of WS connections that yielded 0 messages

        # Check persisted REST-only flag — if set, skip WS probe entirely (known VPS block)
        if os.path.exists(_REST_ONLY_FLAG):
            logger.debug(f"Binance REST mode active for {self.symbol} (cached)")
            while self._running:
                await self._rest_fallback()
                await asyncio.sleep(2)
            return

        # Fast-path: probe REST first — if it works on this host, prefer it
        await self._rest_fallback()
        if self._price > 0:
            logger.info(f"Binance feed active: {self.symbol} via REST")
            # Persist REST-only mode so future restarts skip WS probe immediately
            try:
                open(_REST_ONLY_FLAG, "w").write("rest_only")
            except Exception:
                pass
            while self._running:
                await self._rest_fallback()
                await asyncio.sleep(2)
            return

        while self._running:
            try:
                url = self.WS_URLS[url_idx % len(self.WS_URLS)].format(symbol=self.symbol)
                msgs_received = 0
                async with websockets.connect(url, ping_interval=20, open_timeout=8) as ws:
                    backoff = 1
                    logger.info(f"Binance WS connected: {self.symbol} via {url}")
                    async for msg in ws:
                        if not self._running:
                            break
                        msgs_received += 1
                        ws_empty_strikes = 0
                        data = json.loads(msg)
                        self._price = float(data["p"])
                        self._last_update = time.time()

                if msgs_received == 0:
                    # Connected but closed immediately — IP likely blocked
                    ws_empty_strikes += 1
                    url_idx += 1
                    if ws_empty_strikes >= len(self.WS_URLS):
                        logger.debug(f"Binance WS blocked for {self.symbol} — REST mode confirmed")
                        while self._running:
                            await self._rest_fallback()
                            await asyncio.sleep(2)
                        return
                    await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                url_idx += 1
                logger.warning(f"Binance WS error ({self.symbol}): {e} — retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def get_current_price(self) -> float:
        return self._price

    def get_price(self) -> float:
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
