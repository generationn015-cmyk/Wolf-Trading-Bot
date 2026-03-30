"""
Wolf Trading Bot — Market Making Strategy
Posts both sides of the book on active Polymarket markets.

Key rules:
- ONE position per market at a time (no re-entry until prior trade resolved)
- 10-minute cooldown per market after any trade fires
- Max 3 active MM markets simultaneously
- VPIN spike check — go dark when informed money moves
- Sim resolution: YES+NO paired — one wins, one loses (net = spread capture)
"""
import json
import time
import logging
import asyncio
import requests
from dataclasses import dataclass, field
import json as _mm_json
import config

def _get_clob_spread_value(clob_token_id: str) -> float:
    """Returns the CLOB spread for a token. No auth needed."""
    try:
        import requests as _rq
        r = _rq.get("https://clob.polymarket.com/spread",
            params={"token_id": clob_token_id}, timeout=5)
        return float(r.json().get("spread", 0)) if r.ok else 0.0
    except Exception:
        return 0.0

from learning_engine import learning

logger = logging.getLogger("wolf.strategy.market_making")

COOLDOWN_SEC = 600       # 10 min between re-entry on same market
MAX_ACTIVE   = 3         # max markets we hold simultaneously


@dataclass
class MMSlot:
    market_id: str
    last_fired: float = 0.0      # timestamp of last signal fired
    active_yes: bool  = False    # we have an open YES position
    active_no:  bool  = False    # we have an open NO position


class MarketMaker:
    def __init__(self):
        self._slots:      dict[str, MMSlot] = {}
        self._restore_slots_from_db()  # Prevent re-entry on restart
        self._market_cache: list[dict] = []
        self._cache_ts:   float = 0.0
        self._cache_ttl:  float = 180    # refresh market list every 3 min

    # ── Market list ───────────────────────────────────────────────────────────

    def _fetch_active_markets(self) -> list[dict]:
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._market_cache:
            return self._market_cache
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": True, "limit": 50, "closed": False},
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
                    try:    op = json.loads(op)
                    except: op = []
                if not op or len(op) < 2:
                    continue
                try:
                    p0, p1 = float(op[0]), float(op[1])
                except (ValueError, TypeError):
                    continue
                # Only trade markets with meaningful liquidity on BOTH sides
                if not (0.05 < p0 < 0.95 and 0.05 < p1 < 0.95):
                    continue
                vol = float(m.get("volumeNum", 0) or 0)
                if vol < config.MIN_MARKET_VOLUME:
                    continue
                # Duration filter: paper mode — prefer markets resolving within 7 days
                import config as _mmcfg
                if _mmcfg.PAPER_MODE:
                    from datetime import datetime, timezone as _tz
                    _end_raw = m.get("endDate") or m.get("endDateIso") or ""
                    if _end_raw:
                        try:
                            _end_dt = datetime.fromisoformat(_end_raw.replace("Z", "+00:00"))
                            if not _end_dt.tzinfo: _end_dt = _end_dt.replace(tzinfo=_tz.utc)
                            _days = (_end_dt - datetime.now(_tz.utc)).total_seconds() / 86400
                            if _days > 7:
                                continue
                        except Exception:
                            pass

                m["_yes_price"] = p0
                m["_no_price"]  = p1
                m["_volume"]    = vol
                # Pre-fetch CLOB spread for accurate market making
                clob_ids = m.get("clobTokenIds", "")
                if isinstance(clob_ids, str) and clob_ids.startswith("["):
                    try:
                        ids = _mm_json.loads(clob_ids)
                        if ids:
                            m["_clob_spread"] = _get_clob_spread_value(ids[0])
                    except Exception:
                        m["_clob_spread"] = 0.0
                filtered.append(m)

            self._market_cache = filtered[:20]
            self._cache_ts = now
            logger.info(f"Market maker loaded {len(self._market_cache)} markets")
        except Exception as e:
            logger.warning(f"MM market fetch failed: {e}")
        return self._market_cache

    # ── VPIN ─────────────────────────────────────────────────────────────────

    def _vpin(self, bid_size: float, ask_size: float) -> float:
        total = bid_size + ask_size
        return abs(bid_size - ask_size) / total if total > 0 else 0.0

    # ── Main scan ─────────────────────────────────────────────────────────────

    def _restore_slots_from_db(self):
        """Load open MM positions from DB into slots to prevent re-entry after restart."""
        try:
            import sqlite3
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import config
            conn = sqlite3.connect(config.DB_PATH)
            rows = conn.execute(
                "SELECT market_id, side FROM paper_trades "
                "WHERE resolved=0 AND simulated=0 AND strategy='market_making'"
            ).fetchall()
            conn.close()
            for market_id, side in rows:
                slot = self._slots.setdefault(market_id, MMSlot(market_id=market_id))
                if side == "YES":
                    slot.active_yes = True
                else:
                    slot.active_no = True
        except Exception as e:
            pass  # Non-fatal — worst case we re-enter briefly

    async def scan(self) -> list[dict]:
        signals = []
        markets = self._fetch_active_markets()
        now = time.time()

        # How many markets do we already have active positions on?
        active_count = sum(
            1 for s in self._slots.values()
            if s.active_yes or s.active_no
        )

        for market in markets:
            if len(signals) > 0:
                break  # One new market per scan cycle — prevents flooding

            market_id = market.get("conditionId") or market.get("id", "")
            if not market_id:
                continue

            slot = self._slots.setdefault(market_id, MMSlot(market_id=market_id))

            # Skip if still cooling down
            if now - slot.last_fired < COOLDOWN_SEC:
                continue

            # Skip if already have a position here or at capacity
            if slot.active_yes or slot.active_no:
                continue
            if active_count >= MAX_ACTIVE:
                continue

            yes_price = market["_yes_price"]
            no_price  = market["_no_price"]
            volume    = market["_volume"]

            # Spread we'd capture posting both sides tight
            # Natural spread on Polymarket is typically 2–8 cents
            # Use real CLOB spread if available; fallback to mid-price spread
            clob_spread = market.get("_clob_spread", 0.0)
            natural_spread = clob_spread if clob_spread > 0 else abs(yes_price - (1.0 - no_price))
            if natural_spread < 0.03:  # Require 3% spread minimum (raised from 2%)
                continue  # Too tight — no edge

            # Synthetic VPIN from price imbalance
            vpin = self._vpin(yes_price * 1000, no_price * 1000)
            if vpin > config.VPIN_SPIKE_THRESHOLD:
                logger.debug(f"MM VPIN spike {market_id[:16]}: {vpin:.3f} — skipping")
                continue

            # Our posted prices sit inside the spread
            our_bid = round(yes_price - 0.005, 3)   # slightly below current YES
            our_ask = round(yes_price + 0.005, 3)   # slightly above current YES
            captured = our_ask - our_bid             # = 0.01 baseline

            # Confidence based on spread quality + volume
            vol_bonus = min(0.05, volume / 1_000_000 * 0.05)
            confidence = min(0.78, 0.60 + natural_spread * 3 + vol_bonus)

            if confidence < 0.60:
                continue

            # Mark as active before appending so active_count stays correct
            slot.last_fired = now
            slot.active_yes = True
            slot.active_no  = True
            active_count   += 1

            signals.append({
                "strategy":    "market_making",
                "venue":       "polymarket",
                "market_id":   market_id,
                "side":        "YES",
                "order_type":  "limit",
                "limit_price": our_bid,
                "entry_price": our_bid,
                "edge":        captured / 2,
                "confidence":  confidence,
                "volume":      volume,
                "vpin":        vpin,
                "timestamp":   now,
                "reason":      (
                    f"MM {market_id[:16]}… spread={natural_spread:.3f} "
                    f"vol=${volume:,.0f} VPIN={vpin:.3f}"
                ),
            })
            signals.append({
                "strategy":    "market_making",
                "venue":       "polymarket",
                "market_id":   market_id,
                "side":        "NO",
                "order_type":  "limit",
                "limit_price": round(1.0 - our_ask, 3),
                "entry_price": round(1.0 - our_ask, 3),
                "edge":        captured / 2,
                "confidence":  confidence,
                "volume":      volume,
                "vpin":        vpin,
                "timestamp":   now,
                "reason":      f"MM both sides — paired hedge",
            })

        return signals

    def on_trade_resolved(self, market_id: str):
        """Call when a MM trade resolves so the slot can re-open."""
        if market_id in self._slots:
            self._slots[market_id].active_yes = False
            self._slots[market_id].active_no  = False
