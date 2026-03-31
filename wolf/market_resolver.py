"""
Wolf Market Resolver — Real Outcome Polling
Replaces simulated win_prob resolution with actual Polymarket market results.

Resolution detection logic:
  - outcomePrices: ["1","0"] → YES won
  - outcomePrices: ["0","1"] → NO won
  - lastTradePrice >= 0.99   → YES won
  - lastTradePrice <= 0.01   → NO won
  - closed=True + prices settled → resolved
  - umaResolutionStatuses contains "settled" or "resolved" → confirmed
"""
import time
import logging
import requests
import json

logger = logging.getLogger("wolf.resolver")

# Cache resolved outcomes — market_id → (outcome, fetched_at)
_resolved_cache: dict[str, tuple[str, float]] = {}
# Cache live prices — market_id → (yes_price, no_price, fetched_at)
_price_cache: dict[str, tuple[float, float, float]] = {}

RESOLVE_CACHE_TTL = 3600   # 1h — resolved markets don't change
PRICE_CACHE_TTL   = 30     # 30s — live prices update frequently


def get_real_outcome(market_id: str) -> str | None:
    """
    Query Polymarket for the real resolution outcome of a market.
    Returns 'YES', 'NO', or None if market hasn't resolved yet.
    """
    now = time.time()

    # Return from cache if fresh
    if market_id in _resolved_cache:
        outcome, fetched_at = _resolved_cache[market_id]
        if now - fetched_at < RESOLVE_CACHE_TTL:
            return outcome

    try:
        # Use numeric id when available — conditionId can collide with old markets
        if market_id.startswith("0x"):
            params = {"conditionId": market_id}
        else:
            params = {"id": market_id}

        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params=params,
            timeout=8,
        )
        if not resp.ok:
            return None

        raw = resp.json()
        markets = raw if isinstance(raw, list) else [raw]

        for m in markets:
            # Validate we got the RIGHT market (conditionId can collide)
            m_cid = m.get("conditionId", "")
            m_id  = str(m.get("id", ""))
            if market_id.startswith("0x") and m_cid != market_id:
                continue  # Wrong market returned — skip
            if not market_id.startswith("0x") and m_id != market_id:
                continue  # Wrong market returned — skip

            outcome = _extract_outcome(m)
            if outcome:
                _resolved_cache[market_id] = (outcome, now)
                logger.info(
                    f"[RESOLVER] ✅ Real outcome: "
                    f"{market_id[:20]}… → {outcome} | {m.get('question','')[:50]}"
                )
                return outcome

    except Exception as e:
        logger.debug(f"Resolver fetch error {market_id[:20]}: {e}")

    return None  # Not resolved yet


def _extract_outcome(m: dict) -> str | None:
    """
    Extract YES/NO outcome from a market dict using multiple signals.
    Returns None if market is still live/unresolved.

    Polymarket resolution patterns:
    - Resolved YES: outcomePrices=["1","0"] OR lastTradePrice≈0.999
    - Resolved NO:  outcomePrices=["0","1"] OR lastTradePrice≈0.001-0.01
    - Unresolved/inconclusive: outcomePrices=["0","0"] or ["0.5","0.5"]
    - Still live:   closed=False
    """
    closed = m.get("closed", False)
    if not closed:
        return None  # Still live — wait

    # Signal 1: outcomePrices — only trust [1,0] or [0,1], not [0,0] or [0.5,0.5]
    op = m.get("outcomePrices", "[]")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            op = []

    if op and len(op) >= 2:
        try:
            yes_p = float(op[0])
            no_p  = float(op[1])
            # Only resolve if clearly settled to near-1.0 (not the 0/0 zeroed-out state)
            if yes_p >= 0.97 and no_p <= 0.03:
                return "YES"
            if no_p >= 0.97 and yes_p <= 0.03:
                return "NO"
            # ["0","0"] or ["0.5","0.5"] = not conclusive — fall through to lastTradePrice
        except (ValueError, TypeError):
            pass

    # Signal 2: lastTradePrice — the final price before resolution
    last = m.get("lastTradePrice")
    if last is not None:
        try:
            lp = float(last)
            if lp >= 0.97:
                return "YES"
            if lp <= 0.03:
                return "NO"
        except (ValueError, TypeError):
            pass

    # Signal 3: explicit winner field
    winner = m.get("winner")
    if winner:
        w = str(winner).strip().upper()
        if w in ("YES", "1", "TRUE"):
            return "YES"
        if w in ("NO", "0", "FALSE"):
            return "NO"

    return None  # Closed but outcome not yet deterministic (pending UMA dispute etc.)


# In-memory map of conditionId → numeric id (populated as markets are seen)
_cid_to_id: dict[str, str] = {}
_cid_to_slug: dict[str, str] = {}  # conditionId → slug


def _preload_slugs_from_db() -> None:
    """Load conditionId→slug mappings from slug_cache table + open paper_trades.
    Survives restarts — slug_cache table is the durable source of truth."""
    try:
        import sqlite3 as _sqlite3
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        import config as _cfg
        _conn = _sqlite3.connect(_cfg.DB_PATH)
        # Ensure slug_cache table exists
        _conn.execute("""CREATE TABLE IF NOT EXISTS slug_cache (
            market_id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            updated_at REAL NOT NULL
        )""")
        _conn.commit()
        # Load from persistent slug_cache first
        _rows = _conn.execute(
            "SELECT market_id, slug FROM slug_cache"
        ).fetchall()
        for _cid, _slug in _rows:
            if _cid and _slug:
                _cid_to_slug[_cid] = _slug
        # Also load from open positions (backfill any missing)
        _open_rows = _conn.execute(
            "SELECT market_id, slug FROM paper_trades"
            " WHERE resolved=0 AND slug IS NOT NULL AND slug != ''"
        ).fetchall()
        _conn.close()
        _extra = 0
        for _cid, _slug in _open_rows:
            if _cid and _slug and _cid not in _cid_to_slug:
                _cid_to_slug[_cid] = _slug
                _extra += 1
        total = len(_rows) + _extra
        if total:
            logger.debug(f"Preloaded {total} slug mappings from DB ({len(_rows)} persistent + {_extra} from open trades)")
    except Exception as _e:
        logger.debug(f"Slug preload failed: {_e}")


_preload_slugs_from_db()

def _register_market(m: dict):
    """Cache the conditionId → numeric id and slug mappings for future lookups."""
    cid = m.get("conditionId", "")
    mid = str(m.get("id", ""))
    slug = m.get("slug", "")
    if cid and mid:
        _cid_to_id[cid] = mid
    if cid and slug:
        _cid_to_slug[cid] = slug

def register_slug(cid: str, slug: str) -> None:
    """Externally register a conditionId → slug mapping.
    Persists to slug_cache table so it survives process restarts."""
    if not cid or not slug:
        return
    _cid_to_slug[cid] = slug
    try:
        import sqlite3 as _sqlite3, time as _time
        import config as _cfg
        _conn = _sqlite3.connect(_cfg.DB_PATH, timeout=3)
        _conn.execute(
            "INSERT OR REPLACE INTO slug_cache (market_id, slug, updated_at) VALUES (?,?,?)",
            (cid, slug, _time.time())
        )
        _conn.commit()
        _conn.close()
    except Exception as _e:
        logger.debug(f"[resolver] register_slug DB persist failed: {_e}")


def get_current_price(market_id: str) -> tuple[float, float] | None:
    """
    Get current YES/NO mid-prices for a live market.
    Returns (yes_price, no_price) or None on failure.

    NOTE: gamma-api ?conditionId= filter is broken — returns arbitrary markets.
    Fix: if we have a numeric id cached, use that. Otherwise fetch by numeric id
    from the active market list and match client-side.
    """
    now = time.time()

    if market_id in _price_cache:
        yes_p, no_p, fetched_at = _price_cache[market_id]
        if now - fetched_at < PRICE_CACHE_TTL:
            return yes_p, no_p

    def _extract_prices(m: dict) -> tuple[float, float] | None:
        op = m.get("outcomePrices", "[]")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except Exception:
                return None
        if op and len(op) >= 2:
            try:
                yes_p, no_p = float(op[0]), float(op[1])
                if yes_p + no_p < 0.01:
                    return None  # Zeroed-out / closed market
                return yes_p, no_p
            except (ValueError, TypeError):
                pass
        return None

    try:
        # Strategy 1: use cached numeric id for direct lookup
        numeric_id = _cid_to_id.get(market_id) if market_id.startswith("0x") else market_id

        if numeric_id:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"id": numeric_id},
                timeout=6,
            )
            if resp.ok:
                raw = resp.json()
                markets = raw if isinstance(raw, list) else [raw]
                for m in markets:
                    if str(m.get("id", "")) == numeric_id:
                        prices = _extract_prices(m)
                        if prices:
                            _price_cache[market_id] = (*prices, now)
                            _register_market(m)
                            return prices

        # Strategy 2: fetch active market batch and match conditionId client-side
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": True, "closed": False, "limit": 100},
            timeout=8,
        )
        if resp.ok:
            for m in resp.json():
                _register_market(m)  # Build up our cid→id cache
                m_cid = m.get("conditionId", "")
                m_id  = str(m.get("id", ""))
                matched = (market_id.startswith("0x") and m_cid == market_id) or                           (not market_id.startswith("0x") and m_id == market_id)
                if matched:
                    prices = _extract_prices(m)
                    if prices:
                        _price_cache[market_id] = (*prices, now)
                        return prices

    except Exception as e:
        logger.debug(f"get_current_price error {market_id[:20]}: {e}")

    # Strategy 3: slug-based lookup (for copy_trading markets where conditionId filter is broken)
    slug = _cid_to_slug.get(market_id, "")
    if slug:
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                timeout=6,
            )
            if resp.ok:
                for m in (resp.json() if isinstance(resp.json(), list) else [resp.json()]):
                    if m.get("conditionId", "").lower() == market_id.lower() or m.get("slug", "") == slug:
                        prices = _extract_prices(m)
                        if prices:
                            _price_cache[market_id] = (*prices, now)
                            _register_market(m)
                            return prices
        except Exception as e:
            logger.debug(f"slug lookup error {slug}: {e}")

    return None


def batch_check_outcomes(market_ids: list[str]) -> dict[str, str | None]:
    """
    Check outcomes for multiple markets efficiently.
    Returns dict of market_id → outcome (or None if unresolved).
    """
    results = {}
    # Return cached first
    now = time.time()
    uncached = []
    for mid in market_ids:
        if mid in _resolved_cache:
            outcome, ts = _resolved_cache[mid]
            if now - ts < RESOLVE_CACHE_TTL:
                results[mid] = outcome
                continue
        uncached.append(mid)

    # For uncached, check individually (gamma API doesn't support bulk conditionId lookup)
    for mid in uncached:
        results[mid] = get_real_outcome(mid)

    return results
