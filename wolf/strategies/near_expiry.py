"""
Wolf Trading Bot — Near-Expiry High-Confidence Strategy

The edge: Verified $7,423 → $210,000 in 4 months (99.4% WR).
Buy the side priced at $0.95–$0.99 on markets resolving within 2 hours
when the outcome is near-certain.

Why it works:
- Markets resolving in <2h have almost no remaining uncertainty
- If YES = $0.97, the market is saying 97% chance YES wins
- Reality: when a market is this lopsided this close to expiry, 
  it's usually 99%+ certain — the 3¢ gap is just spread/liquidity
- Buy 100 contracts at $0.97, collect $1.00 = $3 profit per contract
- 75 trades/hour × $124 median size = $9,300/hour theoretical max

Two sub-strategies:
1. NEAR_CERTAIN: Buy $0.95–$0.99 side on markets resolving in <2h
2. COMPLEMENT_EXPIRY: Buy both sides when combined < $1.00 AND <2h expiry
   (guaranteed profit regardless of outcome)

Both Polymarket and Kalshi eligible.
"""
import time
import logging
import asyncio
import requests
import json as _json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional
import config
from feeds.kalshi_feed import get_active_markets as kalshi_markets

logger = logging.getLogger("wolf.strategy.near_expiry")

NEAR_CERTAIN_MIN  = 0.94   # minimum price to consider "near certain"
NEAR_CERTAIN_MAX  = 0.995  # above this, too little upside to bother
EXPIRY_WINDOW_SEC = 7200   # 2 hours
SHORT_WINDOW_SEC  = 1800   # 30 minutes — highest confidence
COOLDOWN_SEC      = 600    # 10 min per market
KALSHI_FEE        = 0.01


def _parse_expiry(date_str: str) -> Optional[float]:
    """Parse ISO date string to Unix timestamp."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _seconds_to_expiry(date_str: str) -> Optional[float]:
    expiry = _parse_expiry(date_str)
    if expiry is None:
        return None
    remaining = expiry - time.time()
    return remaining if remaining > 0 else None


@dataclass
class NearExpiryOpportunity:
    venue: str
    market_id: str
    side: str
    price: float
    seconds_remaining: float
    near_certain: bool
    complement: bool   # True if YES+NO combined < $1
    combined_cost: float = 1.0


class NearExpiryStrategy:
    def __init__(self):
        self._fired:       dict[str, float] = {}
        self._poly_cache:  list[dict] = []
        self._poly_ts:     float = 0.0

    def _fetch_poly_markets(self) -> list[dict]:
        now = time.time()
        if now - self._poly_ts < 120 and self._poly_cache:
            return self._poly_cache
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": True, "limit": 100, "closed": False},
                timeout=10,
            )
            if not resp.ok:
                return self._poly_cache
            markets = resp.json()
            if not isinstance(markets, list):
                return self._poly_cache

            filtered = []
            for m in markets:
                op = m.get("outcomePrices", [])
                if isinstance(op, str):
                    try:    op = _json.loads(op)
                    except: op = []
                if not op or len(op) < 2:
                    continue
                try:
                    p0, p1 = float(op[0]), float(op[1])
                except (ValueError, TypeError):
                    continue

                end_date = m.get("endDate") or m.get("endDateIso") or ""
                secs = _seconds_to_expiry(end_date)
                if secs is None or secs > EXPIRY_WINDOW_SEC:
                    continue  # Not near expiry

                vol = float(m.get("volumeNum", 0) or 0)
                if vol < 2000:
                    continue

                m["_yes_price"] = p0
                m["_no_price"]  = p1
                m["_combined"]  = p0 + p1
                m["_volume"]    = vol
                m["_secs_remaining"] = secs
                filtered.append(m)

            self._poly_cache = filtered
            self._poly_ts = now
        except Exception as e:
            logger.warning(f"NearExpiry poly fetch: {e}")
        return self._poly_cache

    async def scan(self) -> list[dict]:
        signals = []
        now = time.time()
        opportunities: list[NearExpiryOpportunity] = []

        # ── Polymarket ────────────────────────────────────────────────────────
        for m in self._fetch_poly_markets():
            market_id = m.get("conditionId") or m.get("id", "")
            if not market_id or now - self._fired.get(market_id, 0) < COOLDOWN_SEC:
                continue

            yes_p = m["_yes_price"]
            no_p  = m["_no_price"]
            secs  = m["_secs_remaining"]

            # Near-certain single side
            for side, price in [("YES", yes_p), ("NO", no_p)]:
                if NEAR_CERTAIN_MIN <= price <= NEAR_CERTAIN_MAX:
                    opportunities.append(NearExpiryOpportunity(
                        venue="polymarket", market_id=market_id,
                        side=side, price=price,
                        seconds_remaining=secs,
                        near_certain=True, complement=False,
                    ))

            # Complement arb on near-expiry
            combined = yes_p + no_p + 0.005  # slippage
            if combined < 0.97 and secs < SHORT_WINDOW_SEC:
                opportunities.append(NearExpiryOpportunity(
                    venue="polymarket", market_id=market_id,
                    side="YES", price=yes_p,
                    seconds_remaining=secs,
                    near_certain=False, complement=True,
                    combined_cost=combined,
                ))

        # ── Kalshi ────────────────────────────────────────────────────────────
        import config as _cfg
        try:
            if not _cfg.KALSHI_ENABLED:
                raise StopIteration
            kalshi_mkts = kalshi_markets(limit=50)
            for m in kalshi_mkts:
                ticker = m.get("_ticker", "")
                if not ticker or now - self._fired.get(ticker, 0) < COOLDOWN_SEC:
                    continue

                secs = _seconds_to_expiry(m.get("_close_time", ""))
                if secs is None or secs > EXPIRY_WINDOW_SEC:
                    continue

                yes_ask = m.get("_yes_ask", 0)
                no_ask  = m.get("_no_ask", 0)

                for side, price in [("YES", yes_ask), ("NO", no_ask)]:
                    effective = price + KALSHI_FEE
                    if NEAR_CERTAIN_MIN <= price <= NEAR_CERTAIN_MAX:
                        opportunities.append(NearExpiryOpportunity(
                            venue="kalshi", market_id=ticker,
                            side=side, price=price,
                            seconds_remaining=secs,
                            near_certain=True, complement=False,
                        ))

                combined = yes_ask + no_ask + KALSHI_FEE * 2 + 0.005
                if combined < 0.96 and secs < SHORT_WINDOW_SEC:
                    opportunities.append(NearExpiryOpportunity(
                        venue="kalshi", market_id=ticker,
                        side="YES", price=yes_ask,
                        seconds_remaining=secs,
                        near_certain=False, complement=True,
                        combined_cost=combined,
                    ))
        except Exception as e:
            logger.debug(f"NearExpiry Kalshi: {e}")

        # Sort: complement arb > near-certain, shortest expiry first
        opportunities.sort(key=lambda x: (not x.complement, x.seconds_remaining))

        for opp in opportunities[:4]:
            if now - self._fired.get(opp.market_id, 0) < COOLDOWN_SEC:
                continue

            self._fired[opp.market_id] = now

            if opp.near_certain:
                # Single side — high confidence buy
                edge = max(0, 1.0 - opp.price - (KALSHI_FEE if opp.venue == "kalshi" else 0.005))
                confidence = min(0.99, 0.90 + (opp.price - NEAR_CERTAIN_MIN) * 2)
                # Shorter window = higher confidence
                if opp.seconds_remaining < SHORT_WINDOW_SEC:
                    confidence = min(0.995, confidence + 0.02)
                signals.append({
                    "strategy":    "near_expiry",
                    "venue":       opp.venue,
                    "market_id":   opp.market_id,
                    "side":        opp.side,
                    "entry_price": opp.price,
                    "confidence":  confidence,
                    "edge":        edge,
                    "volume":      0,
                    "timestamp":   now,
                    "reason": (
                        f"NearExpiry [{opp.venue}] {opp.side}@{opp.price:.3f} "
                        f"{opp.seconds_remaining/60:.0f}min remaining"
                    ),
                })
                logger.info(
                    f"💎 NearExpiry [{opp.venue}]: {opp.market_id[:20]}… "
                    f"{opp.side}@{opp.price:.3f} | {opp.seconds_remaining/60:.0f}min left"
                )
            else:
                # Complement arb — both sides
                edge = (1.0 - opp.combined_cost) / 2
                confidence = min(0.99, 0.95 + (0.97 - opp.combined_cost) * 5)
                for side, price in [
                    ("YES", opp.price),
                    ("NO",  1.0 - opp.combined_cost - opp.price),
                ]:
                    signals.append({
                        "strategy":    "near_expiry",
                        "venue":       opp.venue,
                        "market_id":   opp.market_id,
                        "side":        side,
                        "entry_price": price,
                        "confidence":  confidence,
                        "edge":        edge,
                        "volume":      0,
                        "timestamp":   now,
                        "reason": (
                            f"NearExpiry complement [{opp.venue}] "
                            f"combined={opp.combined_cost:.3f} "
                            f"{opp.seconds_remaining/60:.0f}min left"
                        ),
                    })
                logger.info(
                    f"💎 NearExpiry complement [{opp.venue}]: {opp.market_id[:20]}… "
                    f"edge={1-opp.combined_cost:.3f} | {opp.seconds_remaining/60:.0f}min left"
                )

        return signals
