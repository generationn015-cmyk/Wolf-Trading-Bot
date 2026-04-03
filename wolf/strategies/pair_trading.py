"""
Wolf Trading Bot — Underdog Fade Strategy (formerly Pair Trading)

The original "Gabagool" pair trading approach (buy YES+NO simultaneously for <$0.97 combined)
is MATHEMATICALLY IMPOSSIBLE on Polymarket — YES+NO always sums to ~$1.00 by design.

New strategy: UNDERDOG FADE
- Find markets where one side is cheap (0.05–0.30) but the market is still live
- These are often mispriced underdogs near resolution with trapped liquidity
- Fade the overpriced side: when YES > 0.85 and NO < 0.15, buy the cheap NO
- Works on: sports, binary outcomes, crypto price targets close to resolution
- Edge: market makers price these wrong as resolution approaches; late movers get squeezed

Rules:
- Only enter when price is in 0.05–0.30 range (underdog with real chance)
- Minimum volume $10K (enough liquidity to exit)
- Only within 48h of resolution (near-term signal validity window)
- Max 6 concurrent positions, $15 per trade
"""
import time
import logging
import asyncio
import requests
import json as _json
from datetime import datetime, timezone as _tz
from dataclasses import dataclass, field
from market_priority import fetch_prioritized_markets
from typing import Optional
import config
from learning_engine import learning

logger = logging.getLogger("wolf.strategy.pair_trading")

# Strategy parameters
ENABLED = False   # DISABLED: 100% void rate, all ghost markets — fix pending
UNDERDOG_MAX_PRICE  = 0.30   # Buy the cheap side when it's <= this
UNDERDOG_MIN_PRICE  = 0.05   # Skip near-zero (already resolved in spirit)
MIN_VOLUME          = 10_000 # $10K minimum market volume
MAX_POSITION_SIZE   = 15     # $15 per trade
MAX_ACTIVE          = 6      # Max concurrent positions
COOLDOWN_SEC        = 1800   # 30 min between re-entries per market
MIN_HOURS_LEFT      = 0.5    # Skip markets resolving in < 30 min
MAX_HOURS_LEFT      = 48     # Only trade near-term markets


class UnderdogFader:
    def __init__(self):
        self._active: dict[str, float] = {}   # market_id → last_fired ts
        self._market_cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 120
        self._seed_active_from_db()

    def _seed_active_from_db(self):
        """Load open positions from DB to prevent re-entry after restart."""
        try:
            import sqlite3, os
            if not os.path.exists(config.DB_PATH):
                return
            conn = sqlite3.connect(config.DB_PATH, timeout=3)
            rows = conn.execute(
                "SELECT DISTINCT market_id FROM paper_trades "
                "WHERE strategy='pair_trading' AND resolved=0 AND COALESCE(void,0)=0"
            ).fetchall()
            conn.close()
            for (mid,) in rows:
                self._active[mid] = time.time()
            if self._active:
                logger.info(f"Pair trading dedup seeded: {len(self._active)} open positions protected")
        except Exception as e:
            logger.debug(f"Pair trading DB seed failed: {e}")

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

                # Need at least one underdog side in tradeable range
                underdog_side = None
                underdog_price = None
                if UNDERDOG_MIN_PRICE <= p_no <= UNDERDOG_MAX_PRICE:
                    underdog_side = "NO"
                    underdog_price = p_no
                elif UNDERDOG_MIN_PRICE <= p_yes <= UNDERDOG_MAX_PRICE:
                    underdog_side = "YES"
                    underdog_price = p_yes

                if underdog_side is None:
                    continue

                vol = float(m.get("volumeNum", 0) or 0)
                if vol < MIN_VOLUME:
                    continue

                # Expiry filter
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
                if hours_left < MIN_HOURS_LEFT or hours_left > MAX_HOURS_LEFT:
                    continue

                m["_yes_price"] = p_yes
                m["_no_price"] = p_no
                m["_volume"] = vol
                m["_hours_left"] = hours_left
                m["_end_epoch"] = end_epoch
                m["_underdog_side"] = underdog_side
                m["_underdog_price"] = underdog_price
                filtered.append(m)

            self._market_cache = filtered
            self._cache_ts = now
        except Exception as e:
            logger.warning(f"Pair trader market fetch failed: {e}")
        return self._market_cache

    async def scan(self) -> list[dict]:
        if not ENABLED:
            return []
        signals = []
        markets = self._fetch_markets()
        now = time.time()

        # Expire cooldowns
        stale = [mid for mid, ts in self._active.items() if now - ts > COOLDOWN_SEC]
        for mid in stale:
            del self._active[mid]

        active_count = len(self._active)

        for m in markets:
            if active_count >= MAX_ACTIVE:
                break

            mid = m.get("conditionId") or m.get("id", "")
            if not mid or mid in self._active:
                continue

            side = m["_underdog_side"]
            price = m["_underdog_price"]
            volume = m["_volume"]
            hours_left = m["_hours_left"]
            end_epoch = m["_end_epoch"]
            question = m.get("question", "")[:80]

            # Learning engine checks
            if learning.is_bad_price(price):
                continue
            floor = learning.get_confidence_floor("pair_trading")

            # Confidence: higher when price is lower (more edge) and volume is high
            # At 0.05 price with 1.0 resolution = 19x. At 0.30 = 2.3x.
            edge = 1.0 - price
            confidence = min(0.82, 0.62 + (UNDERDOG_MAX_PRICE - price) * 0.8 + min(0.05, volume / 500_000 * 0.05))
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
                "strategy":       "pair_trading",
                "venue":          "polymarket",
                "market_id":      mid,
                "slug":           slug,
                "question":       question,
                "side":           side,
                "entry_price":    price,
                "edge":           edge,
                "confidence":     confidence,
                "volume":         volume,
                "size":           MAX_POSITION_SIZE,
                "timestamp":      now,
                "days_to_expiry": hours_left / 24,
                "market_end":     end_epoch,
                "reason":         (
                    f"Underdog {side}@{price:.3f} vol=${volume:,.0f} "
                    f"expires_in={hours_left:.1f}h"
                ),
            })

        if signals:
            logger.info(f"Underdog fader: {len(signals)} signal(s) | {active_count} active")
        return signals

    def on_trade_resolved(self, market_id: str):
        """Free up slot when trade resolves."""
        self._active.pop(market_id, None)


# Keep class alias so main.py import still works
PairTrader = UnderdogFader
