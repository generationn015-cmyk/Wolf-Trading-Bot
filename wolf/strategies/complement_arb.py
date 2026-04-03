"""
Wolf Trading Bot — High-Probability Bond Strategy (formerly Complement Arb)

The original "buy YES+NO for <$0.97 combined" approach is mathematically impossible
on Polymarket — YES+NO always sums to ~$1.00 by design. Strategy replaced.

NEW STRATEGY: HIGH-PROBABILITY BOND
When a market is priced at >= 0.92 on one side (near-certain outcome), back it.
The implied probability is already high, so residual risk is small. The edge
is in being right 95%+ of the time on markets the crowd has already priced highly.

Rules:
- Target: one side >= 0.92 (market says ~92%+ chance)
- Volume >= $20K (proven liquidity, market isn't thin/stale)
- Within 48h of resolution (captures the final convergence to 1.0)
- Don't enter above 0.995 — no juice left
- Max 4 concurrent positions, $10 per trade

Edge vs value_bet bond logic: this fires on near-certainty markets nearing expiry,
value_bet fires on full-certainty (>0.92 ALL durations). Separate cadence.
"""
import time
import logging
import asyncio
import json as _json
from datetime import datetime, timezone as _tz
from market_priority import fetch_prioritized_markets
import config
from learning_engine import learning

logger = logging.getLogger("wolf.strategy.complement_arb")

HIGH_PROB_MIN     = 0.92   # Minimum price for "near-certain" signal
HIGH_PROB_MAX     = 0.995  # Don't enter — no upside
MIN_VOLUME        = 20_000 # $20K volume required
MAX_POSITION_SIZE = 10     # $10 per trade
MAX_ACTIVE        = 4      # Max concurrent positions
COOLDOWN_SEC      = 1800   # 30 min cooldown per market


class ComplementArb:
    def __init__(self):
        self._active: dict[str, float] = {}   # market_id → last_fired ts
        self._market_cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 120

    def _fetch_markets(self) -> list[dict]:
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._market_cache:
            return self._market_cache
        try:
            markets = fetch_prioritized_markets(limit=200, max_days=2)
            if not isinstance(markets, list):
                return self._market_cache

            now_dt = datetime.now(_tz.utc)
            filtered = []
            for m in markets:
                op = m.get("outcomePrices", [])
                if isinstance(op, str):
                    try:    op = _json.loads(op)
                    except: op = []
                if not op or len(op) < 2:
                    continue
                try:
                    p_yes, p_no = float(op[0]), float(op[1])
                except (ValueError, TypeError):
                    continue

                vol = float(m.get("volumeNum", 0) or 0)
                if vol < MIN_VOLUME:
                    continue

                # Check if either side qualifies as high-probability
                high_side = None
                high_price = None
                if HIGH_PROB_MIN <= p_yes <= HIGH_PROB_MAX:
                    high_side = "YES"
                    high_price = p_yes
                elif HIGH_PROB_MIN <= p_no <= HIGH_PROB_MAX:
                    high_side = "NO"
                    high_price = p_no
                else:
                    continue

                # Expiry filter — only near-term
                end_raw = m.get("endDate") or m.get("endDateIso") or ""
                hours_left = 99.0
                end_epoch = 0.0
                if end_raw:
                    try:
                        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        if not end_dt.tzinfo:
                            end_dt = end_dt.replace(tzinfo=_tz.utc)
                        hours_left = (end_dt - now_dt).total_seconds() / 3600
                        end_epoch = end_dt.timestamp()
                    except Exception:
                        pass
                if hours_left < 0.5:
                    continue

                m["_yes_price"]  = p_yes
                m["_no_price"]   = p_no
                m["_volume"]     = vol
                m["_hours_left"] = hours_left
                m["_end_epoch"]  = end_epoch
                m["_high_side"]  = high_side
                m["_high_price"] = high_price
                filtered.append(m)

            # Sort by highest price (most certain) first
            filtered.sort(key=lambda x: x["_high_price"], reverse=True)
            self._market_cache = filtered
            self._cache_ts = now
        except Exception as e:
            logger.warning(f"High-prob bond market fetch: {e}")
        return self._market_cache

    async def scan(self) -> list[dict]:
        signals = []
        now = time.time()

        # Expire cooldowns
        stale = [mid for mid, ts in self._active.items() if now - ts > COOLDOWN_SEC]
        for mid in stale:
            del self._active[mid]

        active_count = len(self._active)
        if active_count >= MAX_ACTIVE:
            return signals

        markets = self._fetch_markets()
        floor = learning.get_confidence_floor("complement_arb")

        for m in markets:
            if active_count >= MAX_ACTIVE:
                break

            mid = m.get("conditionId") or m.get("id", "")
            if not mid or mid in self._active:
                continue

            side  = m["_high_side"]
            price = m["_high_price"]
            volume = m["_volume"]
            hours_left = m["_hours_left"]
            end_epoch = m["_end_epoch"]
            question = m.get("question", "")[:80]

            # Confidence scales with certainty — 0.92 price = 0.88 conf, 0.99 = 0.97 conf
            confidence = min(0.97, 0.85 + (price - HIGH_PROB_MIN) * 1.5)
            confidence = max(confidence, floor)

            if confidence < max(floor, config.MIN_CONFIDENCE):
                continue

            self._active[mid] = now
            active_count += 1

            slug = m.get("slug", "")
            if slug and mid:
                try:
                    from market_resolver import register_slug
                    register_slug(mid, slug)
                except Exception:
                    pass

            signals.append({
                "strategy":       "complement_arb",
                "venue":          "polymarket",
                "market_id":      mid,
                "slug":           slug,
                "side":           side,
                "entry_price":    price,
                "edge":           1.0 - price,
                "confidence":     confidence,
                "volume":         volume,
                "timestamp":      now,
                "days_to_expiry": hours_left / 24,
                "market_end":     end_epoch,
                "reason": (
                    f"High-prob bond {side}@{price:.3f} "
                    f"vol=${volume:,.0f} expires_in={hours_left:.1f}h"
                ),
            })
            logger.info(
                f"High-prob bond: {side}@{price:.3f} | {question[:50]} | "
                f"conf={confidence:.2f} vol=${volume:,.0f}"
            )

        return signals
