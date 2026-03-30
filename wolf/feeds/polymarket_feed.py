"""
Wolf Trading Bot — Polymarket Feed
Wraps the py-clob-client SDK for market data and wallet intelligence.
"""
import logging
import time
import requests
import config

logger = logging.getLogger("wolf.feeds.polymarket")

# Lazy import — only needed when credentials are set
_client = None

def get_client():
    global _client
    if _client is None:
        if not config.POLYMARKET_PRIVATE_KEY:
            logger.info("Polymarket CLOB client not initialized — running in public read-only mode. "
                        "All leaderboard/market/price/activity data uses free public REST APIs. "
                        "Credentials only needed for live order execution.")
            return None
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            from py_clob_client.constants import POLYGON
            creds = ApiCreds(
                api_key=config.POLYMARKET_API_KEY,
                api_secret=config.POLYMARKET_API_SECRET,
                api_passphrase=config.POLYMARKET_API_PASSPHRASE,
            )
            _client = ClobClient(
                config.POLYMARKET_CLOB_URL,
                key=config.POLYMARKET_PRIVATE_KEY,
                chain_id=POLYGON,
                creds=creds,
            )
        except Exception as e:
            logger.error(f"Failed to init Polymarket client: {e}")
            return None
    return _client

def get_market_price(market_id: str) -> tuple[float, float]:
    """Returns (best_yes_price, best_no_price). Returns (0.5, 0.5) on error."""
    try:
        client = get_client()
        if not client:
            return 0.5, 0.5
        book = client.get_order_book(market_id)
        best_yes = float(book.bids[0].price) if book.bids else 0.5
        best_no = 1.0 - best_yes
        return best_yes, best_no
    except Exception as e:
        logger.warning(f"get_market_price error {market_id}: {e}")
        return 0.5, 0.5

def get_market_volume(market_id: str) -> float:
    """Returns 24h volume in USD."""
    try:
        resp = requests.get(
            f"{config.POLYMARKET_GAMMA_URL}/markets",
            params={"clob_token_ids": market_id},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            if data:
                return float(data[0].get("volumeNum", 0))
    except Exception as e:
        logger.warning(f"get_market_volume error: {e}")
    return 0.0

def get_orderbook(market_id: str) -> dict:
    """Returns orderbook dict with bids/asks."""
    try:
        client = get_client()
        if not client:
            return {}
        book = client.get_order_book(market_id)
        return {
            "bids": [(float(b.price), float(b.size)) for b in book.bids],
            "asks": [(float(a.price), float(a.size)) for a in book.asks],
        }
    except Exception as e:
        logger.warning(f"get_orderbook error {market_id}: {e}")
        return {}

POLYMARKET_DATA_URL = "https://data-api.polymarket.com"

def get_top_wallets(limit: int = 20) -> list[dict]:
    """Returns top Polymarket wallets by P&L from leaderboard (v1 endpoint)."""
    try:
        resp = requests.get(
            f"{POLYMARKET_DATA_URL}/v1/leaderboard",
            params={"limit": min(limit, 50), "orderBy": "PNL", "window": "all"},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            # Normalize to expected field names used by CopyTrader
            normalized = []
            for entry in (data if isinstance(data, list) else []):
                normalized.append({
                    "proxy_wallet": entry.get("proxyWallet", ""),
                    "wallet": entry.get("proxyWallet", ""),
                    "profit": entry.get("pnl", 0),
                    "percentPositive": 0,   # not in leaderboard; enriched via activity scan
                    "tradesCount": 0,
                    "avgPositionSize": 0,
                    "maxPositionSize": 0,
                    "activeDays": 0,
                    "marketsTraded": 0,
                    "userName": entry.get("userName", ""),
                    "vol": entry.get("vol", 0),
                })
            return normalized
    except Exception as e:
        logger.warning(f"get_top_wallets error: {e}")
    return []

def get_wallet_activity(wallet_address: str, limit: int = 20) -> list[dict]:
    """Returns recent trade activity for a wallet (data-api v1)."""
    try:
        resp = requests.get(
            f"{POLYMARKET_DATA_URL}/activity",
            params={"user": wallet_address, "limit": limit},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"get_wallet_activity error: {e}")
    return []

def get_wallet_positions(wallet_address: str, limit: int = 20) -> list[dict]:
    """Returns open positions for a wallet (data-api v1)."""
    try:
        resp = requests.get(
            f"{POLYMARKET_DATA_URL}/positions",
            params={"user": wallet_address, "limit": limit},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            # Normalize to format expected by CopyTrader.scan()
            normalized = []
            for p in (data if isinstance(data, list) else []):
                normalized.append({
                    "id": p.get("asset", ""),
                    "market": p.get("conditionId", ""),
                    "side": "YES",   # positions don't specify side directly; default YES
                    "size": float(p.get("size", 0)),
                    "price": float(p.get("avgPrice", 0.5)),
                    "timestamp": 0,  # no timestamp on positions endpoint
                })
            return normalized
    except Exception as e:
        logger.warning(f"get_wallet_positions error: {e}")
    return []

def get_active_btc_markets() -> list[dict]:
    """Returns active BTC/ETH 15-min prediction markets."""
    try:
        resp = requests.get(
            f"{config.POLYMARKET_GAMMA_URL}/markets",
            params={"tag": "crypto", "active": True, "limit": 50},
            timeout=10
        )
        if resp.ok:
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            # Filter for short-duration BTC/ETH markets
            return [m for m in markets if any(
                kw in m.get("question", "").upper()
                for kw in ["BTC", "BITCOIN", "ETH", "ETHEREUM"]
            )]
    except Exception as e:
        logger.warning(f"get_active_btc_markets error: {e}")
    return []
