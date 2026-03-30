"""
Wolf Trading Bot — Kalshi Market Feed
Kalshi is a US-regulated prediction market (CFTC-licensed).
REST API — no SDK required. Uses RSA key signing for authenticated endpoints.

Public endpoints (no auth): market list, orderbook, prices
Authenticated endpoints: place orders, get balance, portfolio

Key differences from Polymarket:
- Contracts denominated in USD cents (not USDC on Polygon)
- Markets have "Yes" / "No" sides (same binary structure)
- Resolution via Kalshi's own oracle (not UMA)
- Fees: ~1% per trade (baked into spread)
- API base: https://trading.kalshi.com/trade-api/v2
"""
import os
import time
import logging
import requests
from typing import Optional
import config

logger = logging.getLogger("wolf.feeds.kalshi")

KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://trading.kalshi.com/trade-api/v2")
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

# Use demo until Jefe provides live credentials
_base_url = KALSHI_DEMO_BASE if os.getenv("KALSHI_DEMO", "true").lower() == "true" else KALSHI_API_BASE

_session = requests.Session()
_session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
})

_auth_token: Optional[str] = None
_token_expiry: float = 0.0


def _get_auth_headers() -> dict:
    """Return auth headers if credentials are configured."""
    global _auth_token, _token_expiry
    key = config.KALSHI_API_KEY
    secret = config.KALSHI_API_SECRET
    if not key or not secret:
        return {}
    # Token still valid
    if _auth_token and time.time() < _token_expiry - 60:
        return {"Authorization": f"Bearer {_auth_token}"}
    # Login to get token
    try:
        resp = _session.post(
            f"{_base_url}/login",
            json={"email": key, "password": secret},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            _auth_token = data.get("token", "")
            _token_expiry = time.time() + 3600  # 1h
            logger.info("Kalshi: authenticated")
            return {"Authorization": f"Bearer {_auth_token}"}
    except Exception as e:
        logger.warning(f"Kalshi auth failed: {e}")
    return {}


def get_active_markets(limit: int = 50, category: str = None) -> list[dict]:
    """
    Fetch active Kalshi markets.
    Returns list of market dicts with normalized price fields.
    """
    try:
        params = {
            "limit": min(limit, 100),
            "status": "open",
        }
        if category:
            params["category"] = category

        resp = _session.get(
            f"{_base_url}/markets",
            params=params,
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"Kalshi markets: HTTP {resp.status_code}")
            return []

        data = resp.json()
        markets = data.get("markets", [])
        normalized = []
        for m in markets:
            try:
                # Kalshi prices in cents (0–100), normalize to 0.0–1.0
                yes_bid = float(m.get("yes_bid", 0) or 0) / 100
                yes_ask = float(m.get("yes_ask", 0) or 0) / 100
                no_bid  = float(m.get("no_bid", 0) or 0) / 100
                no_ask  = float(m.get("no_ask", 0) or 0) / 100

                if yes_ask <= 0 or no_ask <= 0:
                    continue

                yes_price = (yes_bid + yes_ask) / 2
                no_price  = (no_bid + no_ask) / 2

                m["_yes_price"]  = yes_price
                m["_no_price"]   = no_price
                m["_yes_ask"]    = yes_ask
                m["_no_ask"]     = no_ask
                m["_combined"]   = yes_ask + no_ask
                m["_volume"]     = float(m.get("volume", 0) or 0)
                m["_ticker"]     = m.get("ticker", "")
                m["_title"]      = m.get("title", "")
                m["_close_time"] = m.get("close_time", "")
                normalized.append(m)
            except (ValueError, TypeError):
                continue

        logger.info(f"Kalshi: {len(normalized)} active markets")
        return normalized

    except Exception as e:
        logger.warning(f"Kalshi market fetch failed: {e}")
        return []


def get_market_orderbook(ticker: str) -> dict:
    """Fetch orderbook for a Kalshi market by ticker."""
    try:
        resp = _session.get(
            f"{_base_url}/markets/{ticker}/orderbook",
            timeout=8,
        )
        if resp.ok:
            return resp.json().get("orderbook", {})
    except Exception as e:
        logger.debug(f"Kalshi orderbook {ticker}: {e}")
    return {}


def get_balance() -> float:
    """Get Kalshi account balance in USD."""
    headers = _get_auth_headers()
    if not headers:
        return 0.0
    try:
        resp = _session.get(
            f"{_base_url}/portfolio/balance",
            headers=headers,
            timeout=8,
        )
        if resp.ok:
            data = resp.json()
            return float(data.get("balance", 0) or 0) / 100  # cents → dollars
    except Exception as e:
        logger.warning(f"Kalshi balance: {e}")
    return 0.0


def place_order(ticker: str, side: str, count: int,
                order_type: str = "market",
                limit_price_cents: int = None) -> dict:
    """
    Place a Kalshi order.
    side: 'yes' or 'no'
    count: number of contracts
    limit_price_cents: required for limit orders (0-100)
    """
    headers = _get_auth_headers()
    if not headers:
        return {"error": "Not authenticated"}
    try:
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": side.lower(),
            "count": count,
            "type": order_type,
        }
        if order_type == "limit" and limit_price_cents is not None:
            payload["yes_price"] = limit_price_cents if side.lower() == "yes" else (100 - limit_price_cents)

        resp = _session.post(
            f"{_base_url}/portfolio/orders",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if resp.ok:
            return resp.json()
        else:
            logger.error(f"Kalshi order failed: {resp.status_code} {resp.text[:200]}")
            return {"error": resp.text}
    except Exception as e:
        logger.error(f"Kalshi order error: {e}")
        return {"error": str(e)}


def is_configured() -> bool:
    """Returns True if Kalshi credentials are set."""
    return bool(config.KALSHI_API_KEY and config.KALSHI_API_SECRET)
