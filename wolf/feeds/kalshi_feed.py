"""
Wolf Trading Bot — Kalshi Feed (Phase 1.5 Adapter)
Thin custom client using raw REST calls, avoiding SDK instability.
Supports market discovery for:
- Fed rate decisions
- economic indicators (CPI, jobs)
- sports markets
- future copy-trading expansion
"""
import time
import json
import base64
import logging
from pathlib import Path
from typing import Optional
import requests
import config

logger = logging.getLogger("wolf.feeds.kalshi")

class KalshiFeed:
    def __init__(self):
        self.base_url = config.KALSHI_BASE_URL.rstrip("/")
        self.api_key_id = config.KALSHI_API_KEY_ID
        self.private_key_path = config.KALSHI_PRIVATE_KEY_PATH
        self.session = requests.Session()

    def _is_configured(self) -> bool:
        return bool(self.api_key_id and self.private_key_path and Path(self.private_key_path).exists())

    def get_exchange_status(self) -> dict:
        try:
            r = self.session.get(f"{self.base_url}/exchange/status", timeout=10)
            return r.json() if r.ok else {"error": r.text}
        except Exception as e:
            return {"error": str(e)}

    def get_events(self, series_ticker: Optional[str] = None, limit: int = 100) -> list[dict]:
        try:
            params = {"limit": limit}
            if series_ticker:
                params["series_ticker"] = series_ticker
            r = self.session.get(f"{self.base_url}/events", params=params, timeout=10)
            if r.ok:
                data = r.json()
                return data.get("events", data) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning(f"Kalshi get_events error: {e}")
        return []

    def get_markets(self, event_ticker: Optional[str] = None, limit: int = 100) -> list[dict]:
        try:
            params = {"limit": limit}
            if event_ticker:
                params["event_ticker"] = event_ticker
            r = self.session.get(f"{self.base_url}/markets", params=params, timeout=10)
            if r.ok:
                data = r.json()
                return data.get("markets", data) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning(f"Kalshi get_markets error: {e}")
        return []

    def get_fed_markets(self) -> list[dict]:
        markets = self.get_markets(limit=200)
        return [m for m in markets if any(kw in (m.get("title", "") + " " + m.get("subtitle", "")).upper()
                                          for kw in ["FED", "RATE", "FOMC", "CUT", "HIKE"])]

    def get_economic_markets(self) -> list[dict]:
        markets = self.get_markets(limit=200)
        return [m for m in markets if any(kw in (m.get("title", "") + " " + m.get("subtitle", "")).upper()
                                          for kw in ["CPI", "INFLATION", "JOBS", "NFP", "UNEMPLOYMENT", "GDP"])]

    def get_sports_markets(self) -> list[dict]:
        markets = self.get_markets(limit=300)
        return [m for m in markets if any(kw in (m.get("title", "") + " " + m.get("subtitle", "")).upper()
                                          for kw in ["NBA", "NFL", "MLB", "NHL", "SOCCER", "TENNIS", "GOLF"])]

    def get_orderbook(self, ticker: str) -> dict:
        try:
            r = self.session.get(f"{self.base_url}/markets/{ticker}/orderbook", timeout=10)
            return r.json() if r.ok else {}
        except Exception as e:
            logger.warning(f"Kalshi orderbook error {ticker}: {e}")
            return {}

    def get_market_details(self, ticker: str) -> dict:
        try:
            r = self.session.get(f"{self.base_url}/markets/{ticker}", timeout=10)
            return r.json() if r.ok else {}
        except Exception as e:
            logger.warning(f"Kalshi market details error {ticker}: {e}")
            return {}

kalshi_feed = KalshiFeed()
