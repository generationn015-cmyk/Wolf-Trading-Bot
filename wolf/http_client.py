"""
Wolf Trading Bot — Async HTTP Client
Shared aiohttp session that replaces all blocking requests.get() calls
inside async strategy scan() methods.

The problem with requests inside async def:
    requests.get() blocks the entire Python event loop — ALL coroutines
    freeze (including other strategy scans and feed handlers) until the
    HTTP response arrives. On a slow Polymarket API call (2-3s), this
    means latency_arb, complement_arb, etc. are all stalled.

aiohttp.ClientSession.get() is truly non-blocking: it yields control to
the event loop while waiting for the response, so all other coroutines
keep running concurrently.

Usage:
    from http_client import http_get, http_get_json

    # Drop-in replacement for requests.get():
    data = await http_get_json("https://gamma-api.polymarket.com/markets",
                               params={"active": True, "limit": 100})
"""
import asyncio
import logging
import aiohttp
from typing import Any

logger = logging.getLogger("wolf.http")

# ── Session singleton (all lazy — no event loop needed at import time) ────────
_session: aiohttp.ClientSession | None = None
_session_lock: asyncio.Lock | None = None
_connector: aiohttp.TCPConnector | None = None


def _get_connector() -> aiohttp.TCPConnector:
    """Lazily create the shared TCP connector (requires running event loop)."""
    global _connector
    if _connector is None or _connector.closed:
        _connector = aiohttp.TCPConnector(
            limit=30,
            limit_per_host=10,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
    return _connector


async def get_session() -> aiohttp.ClientSession:
    """Return shared aiohttp session, creating it on first call (async-safe)."""
    global _session, _session_lock
    if _session is None or _session.closed:
        if _session_lock is None:
            _session_lock = asyncio.Lock()
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(
                        total=12,
                        connect=5,
                        sock_read=8,
                    ),
                    connector=_get_connector(),
                    headers={
                        "User-Agent": "WolfTradingBot/1.0",
                        "Accept": "application/json",
                    },
                )
                logger.debug("aiohttp session created")
    return _session


async def http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout_sec: float | None = None,
) -> aiohttp.ClientResponse | None:
    """
    GET request. Returns the response object on success, None on error.
    Caller must check response.status before reading body.
    """
    try:
        session = await get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_sec) if timeout_sec else None
        resp = await session.get(url, params=params, headers=headers, timeout=timeout)
        return resp
    except asyncio.TimeoutError:
        logger.debug(f"HTTP timeout: {url}")
        return None
    except aiohttp.ClientError as e:
        logger.debug(f"HTTP error {url}: {e}")
        return None
    except Exception as e:
        logger.warning(f"HTTP unexpected error {url}: {e}")
        return None


async def http_get_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout_sec: float | None = None,
    fallback: Any = None,
) -> Any:
    """
    GET + parse JSON. Returns parsed body on success, fallback on any error.
    This is the primary replacement for requests.get(...).json().
    """
    resp = await http_get(url, params=params, headers=headers, timeout_sec=timeout_sec)
    if resp is None:
        return fallback
    try:
        if resp.status != 200:
            logger.debug(f"HTTP {resp.status}: {url}")
            resp.release()
            return fallback
        data = await resp.json(content_type=None)  # Accept text/plain too (Polymarket quirk)
        return data
    except Exception as e:
        logger.debug(f"JSON parse error {url}: {e}")
        return fallback
    finally:
        # Ensure connection is returned to pool even if caller raises
        try:
            resp.release()
        except Exception:
            pass


async def http_post_json(
    url: str,
    json_body: dict,
    headers: dict | None = None,
    timeout_sec: float = 10.0,
) -> Any:
    """POST JSON body, return parsed response or None."""
    try:
        session = await get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with session.post(url, json=json_body, headers=headers, timeout=timeout) as resp:
            if resp.status not in (200, 201):
                logger.debug(f"HTTP POST {resp.status}: {url}")
                return None
            return await resp.json(content_type=None)
    except Exception as e:
        logger.debug(f"HTTP POST error {url}: {e}")
        return None


async def close():
    """Close the session and connector on shutdown."""
    global _session, _connector
    if _session and not _session.closed:
        await _session.close()
        _session = None
        logger.debug("aiohttp session closed")
    if _connector and not _connector.closed:
        await _connector.close()
        _connector = None
