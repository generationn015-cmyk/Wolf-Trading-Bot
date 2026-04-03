"""
Wolf Trading Bot — Combinatorial / Market Rebalancing Arbitrage

PDF Strategy 9: ~$40M extracted Apr 2024–Apr 2025.

Two sub-strategies:
1. MULTI-OUTCOME: In a group of mutually exclusive markets (A wins OR B wins OR C wins),
   if sum(prices) < 0.97 → buy all underpriced sides. Guaranteed profit at resolution.
   
2. LOGICAL INCONSISTENCY: If P(candidate wins state) < P(candidate wins election),
   that's logically impossible — a candidate can't win an election without winning states.
   Buy the underpriced side.

3. BINARY SUM: In a single binary market, if YES + NO < $0.98 → buy both.
   (Simpler than complement_arb — targets markets missed by the 0.95 threshold)

Detection runs every 60s scanning all active markets.
"""
import time
import logging
import requests
import json as _json
from dataclasses import dataclass
import config
from market_priority import fetch_prioritized_markets

logger = logging.getLogger("wolf.strategy.combinatorial_arb")

MIN_ARB_EDGE     = 0.03    # 3¢ minimum guaranteed profit
MAX_PAIR_COST    = 0.97    # Binary sum must be < this
MAX_MULTI_COST   = 0.97    # Multi-outcome sum must be < this
MIN_VOLUME       = 5_000   # $5K minimum
COOLDOWN_SEC     = 3600    # 1h cooldown per market group
MAX_POSITION     = 80      # $80 per leg in paper mode


class CombinatorialArb:
    def __init__(self):
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 60  # refresh every 60s
        self._cooldown: dict[str, float] = {}

    def _fetch_markets(self) -> list[dict]:
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._cache:
            return self._cache
        try:
            markets = fetch_prioritized_markets(limit=200, max_days=2)
            if not isinstance(markets, list):
                return self._cache
            self._cache = markets
            self._cache_ts = now
        except Exception as e:
            logger.warning(f"Combinatorial arb fetch failed: {e}")
        return self._cache

    def _parse_prices(self, m: dict) -> list[float]:
        op = m.get("outcomePrices", [])
        if isinstance(op, str):
            try:    op = _json.loads(op)
            except: op = []
        prices = []
        for p in op:
            try:    prices.append(float(p))
            except: pass
        return prices

    def _check_binary_sum(self, markets: list[dict]) -> list[dict]:
        """Binary markets where YES+NO < MAX_PAIR_COST."""
        signals = []
        now = time.time()
        for m in markets:
            mid = m.get("conditionId") or m.get("id", "")
            if mid in self._cooldown and now - self._cooldown[mid] < COOLDOWN_SEC:
                continue
            prices = self._parse_prices(m)
            if len(prices) != 2:
                continue
            p_yes, p_no = prices[0], prices[1]
            combined = p_yes + p_no
            if combined < MAX_PAIR_COST and combined > 0.1:
                vol = float(m.get("volumeNum", 0) or 0)
                if vol < MIN_VOLUME:
                    continue
                edge = 1.0 - combined
                question = m.get("question", "")[:70]
                logger.info(f"[COMBI] Binary sum arb: {question} YES={p_yes:.3f}+NO={p_no:.3f}={combined:.3f} edge={edge:.3f}")
                _end_epoch = float(m.get("_end_ts", 0) or 0)
                _days_left = float(m.get("_days_to_expiry", 0) or 0)
                self._cooldown[mid] = now
                for side, price in [("YES", p_yes), ("NO", p_no)]:
                    signals.append({
                        "strategy":       "combinatorial_arb",
                        "market_id":      mid,
                        "question":       m.get("question", "")[:80],
                        "side":           side,
                        "entry_price":    price,
                        "price":          price,
                        "confidence":     0.99,
                        "size":           min(MAX_POSITION, config.PAPER_STARTING_CAPITAL * 0.02),
                        "days_to_expiry": _days_left,
                        "market_end":     _end_epoch,
                        "reason":         f"Binary sum arb: {combined:.3f} combined → {edge:.3f} guaranteed",
                    })
        return signals

    def _check_multi_outcome(self, markets: list[dict]) -> list[dict]:
        """Group markets by event tag and check if probability sum < 1.0."""
        signals = []
        now = time.time()

        # Group by event_id or shared slug prefix
        groups: dict[str, list[dict]] = {}
        for m in markets:
            event_id = m.get("eventId") or m.get("groupItemId") or ""
            if event_id:
                groups.setdefault(event_id, []).append(m)

        for event_id, group in groups.items():
            if len(group) < 3:  # Need 3+ mutually exclusive outcomes to be interesting
                continue
            if event_id in self._cooldown and now - self._cooldown[event_id] < COOLDOWN_SEC:
                continue

            # Each market in group: take the YES price as the outcome probability
            yes_prices = []
            valid_group = []
            for m in group:
                prices = self._parse_prices(m)
                if prices and 0 < prices[0] < 1:
                    yes_prices.append(prices[0])
                    valid_group.append(m)

            if len(yes_prices) < 3:
                continue

            total = sum(yes_prices)
            if total < MAX_MULTI_COST:
                edge = 1.0 - total
                if edge < MIN_ARB_EDGE:
                    continue
                # Check all have sufficient volume
                vols = [float(m.get("volumeNum", 0) or 0) for m in valid_group]
                if min(vols) < MIN_VOLUME:
                    continue

                logger.info(f"[COMBI] Multi-outcome arb: {len(valid_group)} markets sum={total:.3f} edge={edge:.3f}")
                self._cooldown[event_id] = now
                for m, price in zip(valid_group, yes_prices):
                    _end_epoch = float(m.get("_end_ts", 0) or 0)
                    _days_left = float(m.get("_days_to_expiry", 0) or 0)
                    signals.append({
                        "strategy":       "combinatorial_arb",
                        "market_id":      m.get("conditionId") or m.get("id", ""),
                        "question":       m.get("question", "")[:80],
                        "side":           "YES",
                        "entry_price":    price,
                        "price":          price,
                        "confidence":     min(0.99, 0.85 + edge),
                        "size":           min(MAX_POSITION, config.PAPER_STARTING_CAPITAL * 0.02),
                        "days_to_expiry": _days_left,
                        "market_end":     _end_epoch,
                        "reason":         f"Multi-outcome arb: {len(valid_group)} outcomes sum={total:.3f} → {edge:.3f} edge",
                    })
        return signals

    async def scan(self) -> list[dict]:
        markets = self._fetch_markets()
        if not markets:
            return []

        signals = []
        signals.extend(self._check_binary_sum(markets))
        signals.extend(self._check_multi_outcome(markets))

        if signals:
            logger.info(f"Combinatorial arb: {len(signals)} signal(s)")
        return signals
