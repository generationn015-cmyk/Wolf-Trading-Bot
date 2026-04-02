"""
Wolf Trading Bot — Binance WebSocket / REST Feed
Real-time BTC/ETH price feed. No API key needed — public stream.
Auto-reconnects on disconnect.

Upgrades over v1:
  - get_volatility_30m(): Returns 30-minute rolling price std-dev as a
    fraction of current price. Used by RiskEngine for vol-adjusted Kelly.
  - Price history ring buffer (last 360 prices at 5s each = 30 minutes)
    stored in memory — zero extra API calls, no disk I/O.
"""
import asyncio
import json
import time
import math
import os
import logging
from collections import deque
import websockets
import config

logger = logging.getLogger("wolf.feeds.binance")

# Flag file indicating REST-only mode is active on this host
_REST_ONLY_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".binance_rest_only")

# Try to import httpx for truly async REST fallback
try:
    import httpx as _httpx
    _USE_HTTPX = True
except ImportError:
    import requests as _requests  # type: ignore
    _USE_HTTPX = False


class BinanceFeed:
    # Binance blocks VPS/cloud IPs in some regions (HTTP 451).
    # Fall back to Binance US or use REST polling as backup.
    WS_URLS = [
        "wss://stream.binance.us:9443/ws/{symbol}@trade",   # Binance US
        "wss://stream.binance.com:443/ws/{symbol}@trade",   # Binance global alt port
    ]
    REST_URL = "https://api.binance.us/api/v3/ticker/price"

    # 30-minute rolling window at one sample per ~5s = 360 samples max
    _VOLATILITY_WINDOW = 360

    def __init__(self, symbol: str = "btcusdt"):
        self.symbol        = symbol.lower()
        self._price:       float = 0.0
        self._last_update: float = 0.0
        self._running      = False
        self._task         = None

        # Ring buffer of (timestamp, price) for volatility calculation
        # maxlen = 360 samples ≈ 30 minutes at 5s poll interval
        self._price_history: deque[tuple[float, float]] = deque(maxlen=self._VOLATILITY_WINDOW)

    # ── Control ───────────────────────────────────────────────────────────────

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())
        logger.info(f"Binance feed started: {self.symbol}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    # ── Price access ──────────────────────────────────────────────────────────

    def get_current_price(self) -> float:
        return self._price

    def get_price(self) -> float:
        return self._price

    def get_price_age_ms(self) -> float:
        if self._last_update == 0:
            return float("inf")
        return (time.time() - self._last_update) * 1000

    def is_fresh(self, max_age_ms: float = 15000) -> bool:
        """Returns True if price data is within max_age_ms milliseconds old."""
        return self.get_price_age_ms() < max_age_ms

    # ── Volatility (NEW) ──────────────────────────────────────────────────────

    def get_volatility_30m(self) -> float | None:
        """
        Returns the 30-minute rolling price standard deviation as a fraction
        of the current price (i.e. coefficient of variation).

        Examples:
            0.005 = 0.5% std-dev = calm market
            0.015 = 1.5% std-dev = elevated volatility
            0.025 = 2.5% std-dev = very choppy

        Returns None if fewer than 30 samples exist (< ~2.5 minutes of data).
        Used by RiskEngine.get_position_size() to dampen Kelly in choppy markets.
        """
        if len(self._price_history) < 30:
            return None

        prices = [p for _, p in self._price_history]
        n = len(prices)
        mean = sum(prices) / n
        if mean <= 0:
            return None

        variance = sum((x - mean) ** 2 for x in prices) / max(n - 1, 1)
        std_dev = math.sqrt(variance)

        # Coefficient of variation — scale-independent
        return round(std_dev / mean, 5)

    def _record_price(self, price: float):
        """Store price tick in history ring buffer."""
        self._price = price
        self._last_update = time.time()
        self._price_history.append((self._last_update, price))

    # ── REST fallback ─────────────────────────────────────────────────────────

    async def _rest_fallback(self):
        """Poll Binance US REST API — truly async via httpx or thread executor."""
        sym = self.symbol.upper()
        try:
            if _USE_HTTPX:
                async with _httpx.AsyncClient(timeout=4.0) as client:
                    r = await client.get(self.REST_URL, params={"symbol": sym})
                    if r.status_code == 200:
                        price = float(r.json()["price"])
                        self._record_price(price)
            else:
                loop = asyncio.get_event_loop()
                r = await loop.run_in_executor(
                    None,
                    lambda: _requests.get(self.REST_URL, params={"symbol": sym}, timeout=4),
                )
                if r.ok:
                    price = float(r.json()["price"])
                    self._record_price(price)
        except Exception as e:
            logger.debug(f"REST fallback error for {sym}: {e}")

    # ── Connection loop ───────────────────────────────────────────────────────

    async def _connect_loop(self):
        """
        Try WS first. If WS is blocked (VPS IP — code 1000 with 0 messages),
        fall through to REST polling at 5s interval.
        REST at 5s gives 30-min volatility history with 360 samples.
        """
        backoff = 1
        url_idx = 0
        ws_empty_strikes = 0

        # Use cached REST-only flag if set from a previous run
        if os.path.exists(_REST_ONLY_FLAG):
            logger.debug(f"Binance REST mode active for {self.symbol} (cached)")
            while self._running:
                await self._rest_fallback()
                await asyncio.sleep(5)
            return

        # Fast-path: probe REST first
        await self._rest_fallback()
        if self._price > 0:
            logger.info(f"Binance feed active: {self.symbol} via REST")
            try:
                open(_REST_ONLY_FLAG, "w").write("rest_only")
            except Exception:
                pass
            while self._running:
                await self._rest_fallback()
                await asyncio.sleep(5)
            return

        # Try WebSocket
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
                        self._record_price(float(data["p"]))

                if msgs_received == 0:
                    ws_empty_strikes += 1
                    url_idx += 1
                    if ws_empty_strikes >= len(self.WS_URLS):
                        logger.debug(f"Binance WS blocked for {self.symbol} — switching to REST")
                        try:
                            open(_REST_ONLY_FLAG, "w").write("rest_only")
                        except Exception:
                            pass
                        while self._running:
                            await self._rest_fallback()
                            await asyncio.sleep(5)
                        return
                    await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                url_idx += 1
                logger.warning(f"Binance WS error ({self.symbol}): {e} — retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


# Singleton feeds — same interface as v1
btc_feed = BinanceFeed("btcusdt")
eth_feed = BinanceFeed("ethusdt")
