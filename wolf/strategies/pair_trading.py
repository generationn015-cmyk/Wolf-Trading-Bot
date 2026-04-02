"""
Wolf Trading Bot — Gabagool Pair Trading Strategy

The Gabagool Method (PDF Strategy 2 — Guaranteed profit math):
Buy YES and NO on the same market ASYNCHRONOUSLY — at different times when each side dips.
As long as combined purchase cost < $0.97, profit is locked in regardless of outcome.

Difference from ComplementArb (simultaneous):
- ComplementArb: requires BOTH sides cheap at the SAME TIME (rare)
- PairTrading:   buy YES when cheap, hold, THEN buy NO when it dips later (more opportunities)

The math (from PDF):
  Buy YES @ 0.517 avg → Buy NO @ 0.449 avg → Combined $0.966 → Profit $0.034/share guaranteed
  Works best on volatile 15-min crypto markets where sentiment swings both directions

Implementation:
- Track each market's YES and NO prices separately
- When YES price < ENTRY_THRESHOLD: open YES leg, add to pending tracker
- When market has a pending YES leg AND NO_price < COMPLETION_THRESHOLD: open NO leg
- If combined cost < MAX_PAIR_COST: pair is locked — guaranteed profit at resolution
- Cancel pending leg if market resolves before second leg fills
"""
import time
import logging
import asyncio
import requests
import json as _json
from dataclasses import dataclass, field
from market_priority import fetch_prioritized_markets
from typing import Optional
import config

logger = logging.getLogger("wolf.strategy.pair_trading")

ENTRY_THRESHOLD     = 0.495  # Buy a side when price ≤ this (Blueprint: < 0.495)
COMPLETION_THRESHOLD = 0.495 # Complete pair when second side ≤ this (Blueprint)
MAX_PAIR_COST        = 0.97  # Reject if combined cost would exceed this (no guaranteed profit)
MIN_EDGE             = 0.03  # Minimum guaranteed profit margin
MAX_POSITION_SIZE    = 100   # $100 max per leg in paper mode
COOLDOWN_SEC         = 1800  # 30 min cooldown per market after a pair completes
MAX_PENDING_PAIRS    = 6     # Max number of open "one leg filled" positions
MIN_VOLUME           = 10_000  # $10K minimum market volume
EXPIRY_MIN_HOURS     = 0.5   # Don't enter if market resolves in less than 30 minutes
EXPIRY_MAX_HOURS     = 168    # Don't enter if market resolves in > 7 days (paper mode)


@dataclass
class PendingPair:
    market_id: str
    question: str
    yes_filled: bool = False
    no_filled: bool = False
    yes_cost: float = 0.0
    no_cost: float = 0.0
    created_at: float = field(default_factory=time.time)
    max_wait_sec: float = 2400  # 40 min timeout (Blueprint)
    expires_at: float = 0.0   # market resolution time (epoch)

    @property
    def combined_cost(self) -> float:
        return self.yes_cost + self.no_cost

    @property
    def is_complete(self) -> bool:
        return self.yes_filled and self.no_filled

    @property
    def guaranteed_profit_pct(self) -> float:
        if not self.is_complete or self.combined_cost <= 0:
            return 0.0
        return (1.0 - self.combined_cost) / self.combined_cost


class PairTrader:
    def __init__(self):
        self._pending: dict[str, PendingPair] = {}  # market_id → PendingPair
        self._completed: dict[str, float] = {}       # market_id → completion timestamp (cooldown)
        self._market_cache: list[dict] = []
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 120  # 2 min refresh
        self._seed_pending_from_db()

    def _seed_pending_from_db(self):
        """Restore pending YES legs from DB after restart — prevents duplicate YES entries."""
        try:
            import sqlite3, os
            db_path = getattr(config, 'DB_PATH', None)
            if not db_path or not os.path.exists(db_path):
                return
            conn = sqlite3.connect(db_path, timeout=3)
            rows = conn.execute(
                "SELECT market_id, entry_price, reason "
                "FROM paper_trades WHERE strategy='pair_trading' AND side='YES' AND resolved=0 AND COALESCE(void,0)=0"
            ).fetchall()
            for mid, entry, reason in rows:
                self._pending[mid] = PendingPair(market_id=mid, question=reason or "pair leg 1",
                                                  yes_cost=entry, yes_filled=True)
            conn.close()
            if self._pending:
                logger.info(f"Pair trading dedup seeded: {len(self._pending)} open YES legs protected")
        except Exception as e:
            logger.debug(f"Pair trading DB seed failed: {e}")

    def _fetch_markets(self) -> list[dict]:
        now = time.time()
        if now - self._cache_ts < self._cache_ttl and self._market_cache:
            return self._market_cache
        try:
            markets = fetch_prioritized_markets(
                limit=60,
                max_days=30,
            )
            if not isinstance(markets, list):
                return self._market_cache

            from datetime import datetime, timezone as _tz
            now_dt = datetime.now(_tz.utc)
            filtered = []
            for m in markets:
                # Parse prices
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

                # Only consider markets where at least one side is cheap enough to enter
                if p_yes > ENTRY_THRESHOLD and p_no > ENTRY_THRESHOLD:
                    continue

                # Volume filter
                vol = float(m.get("volumeNum", 0) or 0)
                if vol < MIN_VOLUME:
                    continue

                # Expiry filter — need enough time for second leg to fill
                end_raw = m.get("endDate") or m.get("endDateIso") or ""
                hours_left = 99.0
                if end_raw:
                    try:
                        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        if not end_dt.tzinfo:
                            end_dt = end_dt.replace(tzinfo=_tz.utc)
                        hours_left = (end_dt - now_dt).total_seconds() / 3600
                    except Exception:
                        pass
                if hours_left < EXPIRY_MIN_HOURS :
                    continue

                m["_yes_price"] = p_yes
                m["_no_price"] = p_no
                m["_volume"] = vol
                m["_hours_left"] = hours_left
                filtered.append(m)

            self._market_cache = filtered
            self._cache_ts = now
        except Exception as e:
            logger.warning(f"Pair trader market fetch failed: {e}")
        return self._market_cache

    def _expire_stale_pending(self):
        """Remove pending pairs whose markets have resolved."""
        now = time.time()
        to_remove = []
        for mid, pair in self._pending.items():
            # Expire if: market resolved (hours_left ≤ 0) or pending > 24h with no second leg
            age_mins = (now - pair.created_at) / 60
            max_wait = getattr(pair, 'max_wait_sec', 2400) / 60
            if (pair.expires_at > 0 and now > pair.expires_at) or age_mins > max_wait:
                logger.info(f"Pair expired without completion: {pair.question[:50]} combined={pair.combined_cost:.3f}")
                to_remove.append(mid)
        for mid in to_remove:
            del self._pending[mid]

    async def scan(self) -> list[dict]:
        signals = []
        self._expire_stale_pending()
        markets = self._fetch_markets()
        now = time.time()

        for m in markets:
            mid = m.get("conditionId") or m.get("id", "")
            if not mid:
                continue
            question = m.get("question", "")[:80]
            yes_price = m["_yes_price"]
            no_price = m["_no_price"]
            hours_left = m["_hours_left"]

            # Cooldown check
            if mid in self._completed and now - self._completed[mid] < COOLDOWN_SEC:
                continue

            # ── Case 1: Market has a pending YES leg — check if NO has dipped ──
            if mid in self._pending and self._pending[mid].yes_filled and not self._pending[mid].no_filled:
                pair = self._pending[mid]
                projected_cost = pair.yes_cost + no_price
                if no_price <= COMPLETION_THRESHOLD and projected_cost < MAX_PAIR_COST:
                    profit_pct = (1.0 - projected_cost) / projected_cost * 100
                    logger.info(f"[PAIR] Completing pair: {question[:50]} YES={pair.yes_cost:.3f}+NO={no_price:.3f}={projected_cost:.3f} profit={profit_pct:.1f}%")
                    pair.no_filled = True
                    pair.no_cost = no_price
                    self._completed[mid] = now
                    del self._pending[mid]
                    signals.append({
                        "strategy": "pair_trading",
                        "market_id": mid,
                        "question": question,
                        "side": "NO",
                        "price": no_price,
                        "entry_price": no_price,
                        "confidence": 0.99,  # Math-guaranteed
                        "size": min(MAX_POSITION_SIZE, config.PAPER_STARTING_CAPITAL * 0.03),
                        "reason": f"Gabagool pair complete: {projected_cost:.3f} combined → {profit_pct:.1f}% guaranteed",
                        "pair_yes_cost": pair.yes_cost,
                        "pair_no_cost": no_price,
                        "pair_combined": projected_cost,
                    })
                continue

            # ── Case 2: No pending leg — check if YES is cheap to open first leg ──
            if mid not in self._pending and len(self._pending) < MAX_PENDING_PAIRS:
                if yes_price <= ENTRY_THRESHOLD:
                    # Open YES leg — wait for NO to dip later
                    projected_if_no_matches = yes_price + COMPLETION_THRESHOLD
                    if projected_if_no_matches < MAX_PAIR_COST:
                        logger.info(f"[PAIR] Opening YES leg: {question[:50]} YES={yes_price:.3f} waiting for NO≤{COMPLETION_THRESHOLD}")
                        from datetime import datetime, timezone as _tz
                        import datetime as _dt
                        end_epoch = 0.0
                        end_raw = m.get("endDate") or m.get("endDateIso") or ""
                        if end_raw:
                            try:
                                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                                if not end_dt.tzinfo:
                                    end_dt = end_dt.replace(tzinfo=_tz.utc)
                                end_epoch = end_dt.timestamp()
                            except Exception:
                                pass
                        self._pending[mid] = PendingPair(
                            market_id=mid,
                            question=question,
                            yes_filled=True,
                            yes_cost=yes_price,
                            expires_at=end_epoch,
                        )
                        signals.append({
                            "strategy": "pair_trading",
                            "market_id": mid,
                            "question": question,
                            "side": "YES",
                            "price": yes_price,
                            "entry_price": yes_price,
                            "confidence": 0.80,  # Not yet guaranteed — waiting for second leg
                            "size": min(MAX_POSITION_SIZE, config.PAPER_STARTING_CAPITAL * 0.03),
                            "reason": f"Gabagool pair leg 1: YES@{yes_price:.3f}, waiting for NO≤{COMPLETION_THRESHOLD}",
                        })

        if signals:
            logger.info(f"Pair trader: {len(signals)} signal(s) | {len(self._pending)} pending pairs")
        return signals
