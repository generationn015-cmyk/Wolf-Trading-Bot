"""
Wolf Trading Bot — Cross-Platform Arbitrage (Polymarket ↔ Kalshi)

The edge: Same event, different platforms, different prices.
Example: "Fed rate cut in December?" 
  Polymarket: YES = 45¢
  Kalshi:     YES = 52¢
  Action: Buy YES on Polymarket (45¢), Sell/Buy NO on Kalshi (48¢)
  Total cost: 93¢ for a guaranteed $1.00 payout = 7.5% risk-free

Why this works:
- Polymarket = crypto-native, DeFi users, skews speculative
- Kalshi = US-regulated, institutional/finance users, skews conservative
- Same event → different user base → different pricing biases
- Spread: typically 3–10 cents on overlapping markets

Matching logic:
1. Fetch both market lists
2. Match markets by title similarity (fuzzy keyword match)
3. If YES_poly < YES_kalshi by > MIN_SPREAD → buy poly YES + kalshi NO
4. If YES_kalshi < YES_poly by > MIN_SPREAD → buy kalshi YES + poly NO

In paper mode: simulate both legs independently, both must resolve same way.
"""
import time
import logging
import asyncio
import requests
import json as _json
from difflib import SequenceMatcher
from typing import Optional
import config
from feeds.kalshi_feed import get_active_markets as kalshi_markets
from market_priority import fetch_prioritized_markets

logger = logging.getLogger("wolf.strategy.cross_platform_arb")

MIN_SPREAD   = 0.05    # minimum price gap to fire (5 cents)
MIN_EDGE     = 0.03    # net edge after both venues' fees
POLY_FEE     = 0.005   # ~0.5% Polymarket fee on some markets
KALSHI_FEE   = 0.01    # ~1% Kalshi fee
COOLDOWN_SEC = 900     # 15 min per pair


def _similarity(a: str, b: str) -> float:
    a = a.lower().strip()
    b = b.lower().strip()
    return SequenceMatcher(None, a, b).ratio()


def _keyword_overlap(a: str, b: str) -> float:
    """Fraction of significant keywords shared between two strings."""
    stop = {"the","a","an","in","on","at","by","for","to","of","will","is","be","has"}
    words_a = {w for w in a.lower().split() if len(w) > 3 and w not in stop}
    words_b = {w for w in b.lower().split() if len(w) > 3 and w not in stop}
    if not words_a or not words_b:
        return 0.0
    overlap = words_a & words_b
    return len(overlap) / max(len(words_a), len(words_b))


class CrossPlatformArb:
    def __init__(self):
        self._poly_cache:   list[dict] = []
        self._kalshi_cache: list[dict] = []
        self._poly_ts:      float = 0.0
        self._kalshi_ts:    float = 0.0
        self._fired:        dict[str, float] = {}

    def _fetch_poly_markets(self) -> list[dict]:
        now = time.time()
        if now - self._poly_ts < 180 and self._poly_cache:
            return self._poly_cache
        try:
            markets = fetch_prioritized_markets(limit=200, max_days=2)
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
                vol = float(m.get("volumeNum", 0) or 0)
                if vol < 5000:
                    continue
                m["_yes_price"] = p0
                m["_no_price"]  = p1
                m["_title"]     = (m.get("question") or m.get("title") or "").lower()
                m["_id"]        = m.get("conditionId") or m.get("id", "")
                m["_volume"]    = vol
                filtered.append(m)

            self._poly_cache = filtered
            self._poly_ts = now
        except Exception as e:
            logger.warning(f"CrossArb poly fetch: {e}")
        return self._poly_cache

    def _match_markets(self) -> list[dict]:
        """Find Polymarket ↔ Kalshi market pairs covering the same event."""
        poly    = self._fetch_poly_markets()
        kalshi  = kalshi_markets(limit=100)
        matches = []

        for km in kalshi:
            k_title = km.get("_title", "").lower()
            if not k_title:
                continue
            best_score = 0.0
            best_pm    = None
            for pm in poly:
                p_title = pm.get("_title", "")
                score = max(
                    _similarity(k_title, p_title),
                    _keyword_overlap(k_title, p_title) * 1.2,  # keyword overlap weighted higher
                )
                if score > best_score:
                    best_score = score
                    best_pm = pm

            if best_pm and best_score >= 0.45:
                matches.append({
                    "kalshi":  km,
                    "poly":    best_pm,
                    "score":   best_score,
                    "k_title": k_title,
                    "p_title": best_pm.get("_title", ""),
                })

        logger.debug(f"CrossArb: {len(matches)} market pairs matched")
        return matches

    async def scan(self) -> list[dict]:
        signals = []
        now = time.time()

        try:
            pairs = self._match_markets()
        except Exception as e:
            logger.warning(f"CrossArb match error: {e}")
            return signals

        for pair in pairs:
            km = pair["kalshi"]
            pm = pair["poly"]

            k_yes = km.get("_yes_ask", km.get("_yes_price", 0))
            k_no  = km.get("_no_ask",  km.get("_no_price", 0))
            p_yes = pm.get("_yes_price", 0)
            p_no  = pm.get("_no_price", 0)

            if not all([k_yes, k_no, p_yes, p_no]):
                continue

            k_id = km.get("_ticker", "")
            p_id = pm.get("_id", "")
            pair_key = f"{p_id}|{k_id}"

            if now - self._fired.get(pair_key, 0) < COOLDOWN_SEC:
                continue

            # Case 1: Poly YES cheaper → buy Poly YES + Kalshi NO
            spread_a = k_yes - p_yes  # how much cheaper poly is
            net_cost_a = p_yes + k_no + POLY_FEE + KALSHI_FEE + 0.005
            edge_a = 1.0 - net_cost_a

            # Case 2: Kalshi YES cheaper → buy Kalshi YES + Poly NO
            spread_b = p_yes - k_yes
            net_cost_b = k_yes + p_no + POLY_FEE + KALSHI_FEE + 0.005
            edge_b = 1.0 - net_cost_b

            best_spread = max(spread_a, spread_b)
            best_edge   = max(edge_a, edge_b)

            if best_spread < MIN_SPREAD or best_edge < MIN_EDGE:
                continue

            self._fired[pair_key] = now
            confidence = min(0.97, 0.88 + best_edge * 3)

            if edge_a >= edge_b:
                # Buy Poly YES + Kalshi NO
                signals.append({
                    "strategy":    "cross_platform_arb",
                    "venue":       "polymarket",
                    "market_id":   p_id,
                    "side":        "YES",
                    "entry_price": p_yes,
                    "confidence":  confidence,
                    "edge":        edge_a / 2,
                    "volume":      pm.get("_volume", 0),
                    "timestamp":   now,
                    "arb_pair":    True,
                    "reason": (
                        f"CrossArb: Poly YES@{p_yes:.2f} + Kalshi NO@{k_no:.2f} "
                        f"= {net_cost_a:.2f} | edge={edge_a:.3f} | "
                        f"match={pair['score']:.2f}"
                    ),
                })
                signals.append({
                    "strategy":    "cross_platform_arb",
                    "venue":       "kalshi",
                    "market_id":   k_id,
                    "side":        "NO",
                    "entry_price": k_no,
                    "confidence":  confidence,
                    "edge":        edge_a / 2,
                    "volume":      km.get("_volume", 0),
                    "timestamp":   now,
                    "arb_pair":    True,
                    "reason":      f"CrossArb hedge: Kalshi NO@{k_no:.2f}",
                })
                logger.info(
                    f"⚡ CrossArb: [{pair['score']:.2f} match] "
                    f"Poly YES {p_yes:.2f} | Kalshi NO {k_no:.2f} | "
                    f"edge={edge_a:.3f}"
                )
            else:
                # Buy Kalshi YES + Poly NO
                signals.append({
                    "strategy":    "cross_platform_arb",
                    "venue":       "kalshi",
                    "market_id":   k_id,
                    "side":        "YES",
                    "entry_price": k_yes,
                    "confidence":  confidence,
                    "edge":        edge_b / 2,
                    "volume":      km.get("_volume", 0),
                    "timestamp":   now,
                    "arb_pair":    True,
                    "reason": (
                        f"CrossArb: Kalshi YES@{k_yes:.2f} + Poly NO@{p_no:.2f} "
                        f"= {net_cost_b:.2f} | edge={edge_b:.3f}"
                    ),
                })
                signals.append({
                    "strategy":    "cross_platform_arb",
                    "venue":       "polymarket",
                    "market_id":   p_id,
                    "side":        "NO",
                    "entry_price": p_no,
                    "confidence":  confidence,
                    "edge":        edge_b / 2,
                    "volume":      pm.get("_volume", 0),
                    "timestamp":   now,
                    "arb_pair":    True,
                    "reason":      f"CrossArb hedge: Poly NO@{p_no:.2f}",
                })
                logger.info(
                    f"⚡ CrossArb: [{pair['score']:.2f} match] "
                    f"Kalshi YES {k_yes:.2f} | Poly NO {p_no:.2f} | "
                    f"edge={edge_b:.3f}"
                )

            if len(signals) >= 4:
                break

        return signals
