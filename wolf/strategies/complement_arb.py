"""
Wolf Trading Bot — Binary Complement Arbitrage

The edge: On any binary YES/NO market, exactly one side pays $1.00.
If YES_ask + NO_ask < $1.00, buying both sides locks guaranteed profit.
Cost: (YES_ask + NO_ask) × size
Payout: $1.00 × size (one side always wins)
Net: ($1.00 - combined_cost) × size — risk-free

Real example (verified wallet $300→$400K+):
- Market: "BTC > $X in 15 min?"
- YES ask: 0.47  |  NO ask: 0.47  |  Total: 0.94
- Buy both → guaranteed $0.06 per share
- 400 trades/day × 100 shares × $0.06 = $2,400/day

Polymarket added fees on 15-min crypto markets at ~50/50 prices.
Fee is highest at 50¢, lowest at extremes.
Current fee structure: max 1.56% at 50¢, near-zero at extremes.
We target: combined_price ≤ 0.95 to clear fees comfortably.

Strategy scope:
1. All active binary markets (not just 15-min)
2. Scan for YES+NO ask sum < 0.95
3. Also check near-expiry markets (within 2 hours) for near-$1 resolution
"""
import time
import logging
import asyncio
import requests
import json as _json
from dataclasses import dataclass, field
from typing import Optional
import config
from feeds.polymarket_feed import get_orderbook
import config
POLYMARKET_GAMMA_URL = config.POLYMARKET_GAMMA_URL

logger = logging.getLogger("wolf.strategy.complement_arb")

MAX_COMBINED_COST = 0.95   # Must be below this to clear fees and be profitable
MIN_EDGE          = 0.03   # Minimum net edge (3¢ per share minimum)
MAX_POSITION_SIZE = 200    # Max $200 per arb (scales with balance)
COOLDOWN_SEC      = 300    # 5 min cooldown per market
MIN_VOLUME        = 5_000  # Need at least $5K volume for fill confidence


@dataclass
class ArbOpportunity:
    market_id: str
    yes_ask: float
    no_ask: float
    combined_cost: float
    edge: float           # 1.0 - combined_cost
    volume: float
    question: str
    near_expiry: bool = False


class ComplementArb:
    def __init__(self):
        self._market_cache:    list[dict] = []
        self._market_cache_ts: float = 0.0
        self._market_ttl:      float = 120   # refresh every 2 min
        self._fired:           dict[str, float] = {}  # market_id → last fired ts
        self._scan_count:      int = 0

    def _fetch_markets(self) -> list[dict]:
        now = time.time()
        if now - self._market_cache_ts < self._market_ttl and self._market_cache:
            return self._market_cache
        try:
            resp = requests.get(
                f"{POLYMARKET_GAMMA_URL}/markets",
                params={"active": True, "limit": 100, "closed": False},
                timeout=10,
            )
            if not resp.ok:
                return self._market_cache
            markets = resp.json()
            if not isinstance(markets, list):
                return self._market_cache

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

                vol = float(m.get("volumeNum", 0) or 0)
                if vol < MIN_VOLUME:
                    continue

                combined = p0 + p1
                if combined > MAX_COMBINED_COST + 0.10:
                    # Way too expensive — skip early
                    continue

                m["_yes_price"]   = p0
                m["_no_price"]    = p1
                m["_combined"]    = combined
                m["_volume"]      = vol
                filtered.append(m)

            self._market_cache = filtered
            self._market_cache_ts = now
        except Exception as e:
            logger.warning(f"Complement arb market fetch: {e}")
        return self._market_cache

    def _check_near_expiry(self, market: dict) -> bool:
        """Return True if market resolves within 2 hours."""
        end_date = market.get("endDate") or market.get("endDateIso") or ""
        if not end_date:
            return False
        try:
            from datetime import datetime, timezone
            # endDate format: "2026-03-29T23:00:00Z"
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
            return 0 < remaining < 7200  # within 2 hours
        except Exception:
            return False

    def _find_arb(self, market: dict) -> Optional[ArbOpportunity]:
        """Check if a market has a complement arb opportunity."""
        yes_ask = market["_yes_price"]
        no_ask  = market["_no_price"]

        # In a real CLOB we'd check the actual ask side of the orderbook.
        # Here the outcomePrices represent the best available prices.
        # Add a 0.5¢ buffer for slippage.
        combined = yes_ask + no_ask + 0.005  # small slippage buffer

        if combined > MAX_COMBINED_COST:
            return None

        edge = 1.0 - combined
        if edge < MIN_EDGE:
            return None

        near_expiry = self._check_near_expiry(market)
        return ArbOpportunity(
            market_id    = market.get("conditionId") or market.get("id", ""),
            yes_ask      = yes_ask,
            no_ask       = no_ask,
            combined_cost = combined,
            edge         = edge,
            volume       = market["_volume"],
            question     = market.get("question", "")[:80],
            near_expiry  = near_expiry,
        )

    async def scan(self) -> list[dict]:
        """Scan all active markets for complement arb opportunities."""
        signals = []
        now = time.time()
        self._scan_count += 1

        # Scan every scan cycle — this is the highest-priority strategy
        markets = self._fetch_markets()
        opportunities: list[ArbOpportunity] = []

        for market in markets:
            arb = self._find_arb(market)
            if not arb or not arb.market_id:
                continue
            if now - self._fired.get(arb.market_id, 0) < COOLDOWN_SEC:
                continue
            opportunities.append(arb)

        # Sort by edge descending — take the best ones first
        opportunities.sort(key=lambda x: x.edge, reverse=True)

        for arb in opportunities[:3]:  # Max 3 per scan
            confidence = min(0.97, 0.90 + arb.edge * 2)  # High confidence — it's arb
            if arb.near_expiry:
                confidence = min(0.99, confidence + 0.02)

            self._fired[arb.market_id] = now

            reason = (
                f"Complement arb: YES {arb.yes_ask:.3f} + NO {arb.no_ask:.3f} "
                f"= {arb.combined_cost:.3f} | edge={arb.edge:.3f} | "
                f"{'NEAR EXPIRY ' if arb.near_expiry else ''}"
                f"{arb.question}"
            )

            # YES leg
            signals.append({
                "strategy":    "complement_arb",
                "venue":       "polymarket",
                "market_id":   arb.market_id,
                "side":        "YES",
                "entry_price": arb.yes_ask,
                "confidence":  confidence,
                "edge":        arb.edge / 2,
                "volume":      arb.volume,
                "timestamp":   now,
                "arb_pair":    True,
                "reason":      reason,
            })
            # NO leg (hedge — one always wins)
            signals.append({
                "strategy":    "complement_arb",
                "venue":       "polymarket",
                "market_id":   arb.market_id,
                "side":        "NO",
                "entry_price": arb.no_ask,
                "confidence":  confidence,
                "edge":        arb.edge / 2,
                "volume":      arb.volume,
                "timestamp":   now,
                "arb_pair":    True,
                "reason":      f"Complement hedge: {reason}",
            })
            logger.info(
                f"💰 Complement arb: {arb.question[:40]}… "
                f"edge={arb.edge:.3f} combined={arb.combined_cost:.3f}"
                + (" [NEAR EXPIRY]" if arb.near_expiry else "")
            )

        if opportunities and self._scan_count % 20 == 0:
            logger.info(
                f"Complement arb scan: {len(markets)} markets checked | "
                f"{len(opportunities)} opportunities | "
                f"best edge: {opportunities[0].edge:.3f}" if opportunities else "none"
            )

        return signals
