"""
Wolf Market Prioritization Engine

Sorts markets by expiry urgency:
  Priority 0: Resolves TODAY (< 24h)
  Priority 1: Resolves TOMORROW (24-48h)
  Priority 2: Resolves THIS WEEK (2-7 days)
  Priority 3: Resolves THIS MONTH (7-30 days)
  Priority 4: Resolves LATER (30+ days)

All strategies use this to ensure Wolf always looks at
shortest-duration markets first, without changing strategy logic.
"""

import time
import requests
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("wolf.market_priority")

# ── Expiry Priority Tiers ───────────────────────────────────────────────
PRIORITY_TODAY    = 0   # < 24h  — HIGHEST URGENCY
PRIORITY_TOMORROW = 1   # 24-48h
PRIORITY_WEEK     = 2   # 2-7 days
PRIORITY_MONTH    = 3   # 7-30 days
PRIORITY_LATER    = 4   # 30+ days — LOWEST URGENCY

# ── Cache (keyed by max_days to prevent cross-contamination) ───────────
_market_caches: dict[float, list[dict]] = {}
_cache_timestamps: dict[float, float] = {}
CACHE_TTL = 30  # seconds — refresh frequently for freshness


def _parse_expiry(end_raw: str) -> Optional[float]:
    """Parse expiry string to timestamp. Returns None if unparseable."""
    if not end_raw:
        return None
    try:
        dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _days_to_expiry(end_ts: Optional[float]) -> float:
    """Calculate days from now to expiry. Returns 999 if no expiry."""
    if end_ts is None:
        return 999.0
    return max(0.0, (end_ts - time.time()) / 86400)


def _expiry_priority(days: float) -> int:
    """Map days-to-expiry to priority tier."""
    if days <= 1:
        return PRIORITY_TODAY
    elif days <= 2:
        return PRIORITY_TOMORROW
    elif days <= 7:
        return PRIORITY_WEEK
    elif days <= 30:
        return PRIORITY_MONTH
    else:
        return PRIORITY_LATER


def _priority_label(tier: int) -> str:
    """Human-readable priority label."""
    return {
        PRIORITY_TODAY: "🔴 TODAY",
        PRIORITY_TOMORROW: "🟠 TOMORROW",
        PRIORITY_WEEK: "🟡 THIS WEEK",
        PRIORITY_MONTH: "🟢 THIS MONTH",
        PRIORITY_LATER: "⚪ LATER",
    }.get(tier, "❓ UNKNOWN")


def fetch_prioritized_markets(
    limit: int = 200,
    min_liquidity: float = 0,
    min_volume: float = 0,
    max_days: float = 365,
    require_two_sided: bool = True,
    custom_params: Optional[dict] = None,
) -> list[dict]:
    """
    Fetch active markets from Gamma API, sorted by expiry urgency.

    Markets closest to resolution appear FIRST.
    Each market gets _days_to_expiry and _expiry_priority fields added.

    Args:
        limit: Max markets to fetch from API
        min_liquidity: Minimum liquidity filter
        min_volume: Minimum volume filter
        max_days: Skip markets beyond this many days
        require_two_sided: Only include markets with prices on both YES and NO
        custom_params: Additional Gamma API parameters to merge
    """
    global _market_caches, _cache_timestamps

    cache_key = max_days
    now = time.time()
    if cache_key in _market_caches and now - _cache_timestamps.get(cache_key, 0) < CACHE_TTL:
        return _market_caches.get(cache_key, [])

    # ── Fetch with pagination to find short-term markets ────────────────
    raw_list = []
    seen_ids = set()

    for offset in [0, 500, 1000, 1500, 2000]:
        params = {"limit": 500, "offset": offset, "active": True, "closed": False}
        if custom_params:
            params.update(custom_params)
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params=params,
                timeout=15,
            )
            if not resp.ok:
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for m in data:
                mid = m.get("id") or m.get("conditionId")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    raw_list.append(m)
            if len(data) < 500:
                break
        except Exception:
            break

    raw = raw_list

    # ── Enrich each market with expiry data ─────────────────────────
    enriched = []
    for m in raw:
        # Parse prices
        op = m.get("outcomePrices", [])
        if isinstance(op, str):
            try:
                import json
                op = json.loads(op)
            except:
                op = []
        if not op or len(op) < 2:
            continue
        try:
            p0, p1 = float(op[0]), float(op[1])
        except (ValueError, TypeError):
            continue

        # Parse expiry
        end_raw = m.get("endDate") or m.get("endDateIso") or ""
        end_ts = _parse_expiry(end_raw)
        days = _days_to_expiry(end_ts)
        priority = _expiry_priority(days)

        # Skip expired markets (endDate already passed)
        if end_ts and end_ts < time.time():
            continue

        # Max days filter — skip markets with no expiry (d=999)
        if days > max_days:
            continue

        # Two-sided filter — always require prices in valid range
        if require_two_sided and not (0.03 < p0 < 0.97 and 0.03 < p1 < 0.97):
            continue

        # Volume filter
        vol = float(m.get("volumeNum", 0) or 0)
        if vol < min_volume:
            continue

        # Enrich
        m["_days_to_expiry"] = days
        m["_expiry_priority"] = priority
        m["_expiry_label"] = _priority_label(priority)
        m["_end_ts"] = end_ts
        m["_yes_price"] = p0
        m["_no_price"] = p1
        m["_volume"] = vol
        m["_spread"] = abs(p0 - (1.0 - p1))

        enriched.append(m)

    # ── Sort by expiry urgency (nearest first), then by volume ──────
    enriched.sort(key=lambda x: (
        x["_expiry_priority"],       # Today first
        x["_days_to_expiry"],        # Within tier, shortest first
            -x["_volume"],               # Tiebreak: highest volume
        ))

    _market_caches[cache_key] = enriched
    _cache_timestamps[cache_key] = now

    # Log distribution
    from collections import Counter
    dist = Counter(m["_expiry_priority"] for m in enriched)
    logger.info(
        f"Market priority: "
        f"🔴TODAY={dist.get(0,0)} "
        f"🟠TOMORROW={dist.get(1,0)} "
        f"🟡WEEK={dist.get(2,0)} "
        f"🟢MONTH={dist.get(3,0)} "
        f"⚪LATER={dist.get(4,0)} "
        f"| Total={len(enriched)}"
    )

    return enriched


def get_expiry_summary(markets: list[dict]) -> str:
    """Get a human-readable summary of market expiry distribution."""
    from collections import Counter
    dist = Counter(m.get("_expiry_priority", 4) for m in markets)
    return (
    f"🔴 TODAY={dist.get(0,0)} "
    f"🟠 TOMORROW={dist.get(1,0)} "
    f"🟡 WEEK={dist.get(2,0)} "
    f"🟢 MONTH={dist.get(3,0)} "
    f"⚪ LATER={dist.get(4,0)}"
    )
