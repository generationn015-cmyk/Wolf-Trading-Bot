"""
Wolf Trading Bot — Value Bet Strategy

Finds markets where the outcome is highly probable based on price.
Key insight: Always enters from the LOW PRICE side (≤ 0.50) for
positive Kelly sizing. High-conviction markets near resolution.

Logic:
- YES price < 0.15 → outcome is very likely NO → BUY NO (price ~0.85-0.91)
- YES price > 0.85 → outcome is very likely YES → BUY YES (price ~0.85-0.91)

Wait — for Kelly to work we need ENTRY price < 0.50.
So: rephrase all trades as the underdog entry:
  - YES=0.09 → BUY YES (entry=0.09, payout=~$1, confident it resolves YES)
    NO — this is the low-confidence side.

Actually the insight is:
  YES=0.09 means NO=0.91 and the market says NO is 91% likely.
  If WE agree NO is ~91% likely, BUY NO at 0.91 → but Kelly hates that.
  
  The ONLY way to get positive Kelly is if we believe probability > market price.
  At YES=0.09 → if we believe true prob of YES is <9% → buy NO at 0.91 is wrong entry.
  → Instead: buy YES at 0.09 if we believe true prob of YES is >9%.

REAL STRATEGY:
Markets priced 0.05–0.20 often have real residual probability.
Sports/news markets where crowd has over-corrected.
Buy the underdog at 0.10–0.20 with tight sizing.

Also: mid-range markets (0.35–0.65) with clear momentum signals.
"""
import os
import time
import logging
import json as _json
import requests
import config
from datetime import datetime, timezone, timedelta
from learning_engine import learning

logger = logging.getLogger("wolf.strategy.value_bet")

# Target: buy the underpriced side — entry price must be low enough for Kelly
MAX_ENTRY_PRICE   = 0.30   # Only buy at ≤ 0.30 → Kelly works
MIN_ENTRY_PRICE   = 0.03   # Too cheap = no liquidity
MIN_VOLUME        = 3_000  # $3K minimum for fills
POLY_FEE          = 0.01   # 1% taker fee
MIN_EDGE          = 0.04   # 4 cents net edge required
COOLDOWN          = 300    # 5 min per market — allow re-entry as prices move
MIN_CONFIDENCE    = 0.70   # override config — be more selective here


class ValueBetStrategy:
    def __init__(self):
        self._fired: dict[str, float] = {}
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0

    def _get_markets(self) -> list[dict]:
        now = time.time()
        if now - self._cache_ts < 90 and self._cache:
            return self._cache
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": True, "limit": 200, "closed": False},
                timeout=10,
            )
            if not resp.ok:
                return self._cache
            markets = resp.json()
            if not isinstance(markets, list):
                return self._cache

            filtered = []
            now_dt = datetime.now(timezone.utc)
            for m in markets:
                op = m.get("outcomePrices", [])
                if isinstance(op, str):
                    try: op = _json.loads(op)
                    except: op = []
                if not op or len(op) < 2:
                    continue
                try:
                    p0, p1 = float(op[0]), float(op[1])
                except:
                    continue

                vol = float(m.get("volumeNum", 0) or 0)
                if vol < MIN_VOLUME:
                    continue

                # Score market duration — shorter = higher priority, but don't block long ones
                end_raw = m.get("endDate") or m.get("endDateIso") or ""
                days_out = 999
                if end_raw:
                    try:
                        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        if not end_dt.tzinfo:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        days_out = max(0, (end_dt - now_dt).days)
                    except Exception:
                        pass
                m["_days_to_expiry"] = days_out

                # Paper mode: prefer short-duration markets to get real resolutions quickly
                # Skip very long-term markets during paper test (no data feedback for months)
                import config as _cfg
                max_days = 30 if _cfg.PAPER_MODE else 365
                if days_out > max_days and days_out != 999:
                    continue

                m["_yes_price"] = p0
                m["_no_price"]  = p1
                m["_volume"]    = vol
                m["_id"]        = m.get("conditionId") or m.get("id", "")
                filtered.append(m)

            self._cache = filtered
            self._cache_ts = now
        except Exception as e:
            logger.warning(f"ValueBet market fetch: {e}")
        return self._cache

    def _score_market(self, yes: float, no: float, vol: float) -> tuple:
        """
        Returns (side, entry_price, confidence, reason) or (None,None,None,None).
        
        Only returns signals where entry_price < 0.30 for positive Kelly.
        Looks for:
        1. YES price is very low (0.03-0.20) but market is active → underdog YES bet
        2. NO price is very low (0.03-0.20) but market is active → underdog NO bet
        """
        # Case 1: YES is the underdog — price 0.03-0.25
        # This means the market says ~75-97% chance of NO.
        # We bet YES only if the underdog has better real odds than the market shows.
        # Signal: large volume at low YES price = active market, not abandoned
        if MIN_ENTRY_PRICE <= yes <= 0.25 and vol >= 5_000:
            # The market has significant volume and prices YES very low
            # Contrarian bet: Yes has residual value the crowd ignores
            # Real edge: yes at 0.10 on a $300k market = high liquidity = real signal
            confidence = 0.70 + min(0.12, (vol / 500_000) * 0.12)
            edge = (1.0 - yes) * confidence - yes * (1 - confidence) - POLY_FEE
            if edge >= MIN_EDGE and confidence >= MIN_CONFIDENCE:
                return "YES", yes, round(confidence, 3), f"Underdog YES@{yes:.3f} vol=${vol:,.0f}"

        # Case 2: NO is the underdog — YES price is very high (0.75-0.97)
        # NO price = 1 - YES = 0.03-0.25
        elif yes >= 0.75 and no <= 0.25 and vol >= 5_000:
            confidence = 0.70 + min(0.12, (vol / 500_000) * 0.12)
            edge = (1.0 - no) * confidence - no * (1 - confidence) - POLY_FEE
            if edge >= MIN_EDGE and confidence >= MIN_CONFIDENCE:
                return "NO", no, round(confidence, 3), f"Underdog NO@{no:.3f} (YES={yes:.3f}) vol=${vol:,.0f}"

        # Case 3: Mid-range YES lean (0.28-0.42) — buy YES as mild underdog
        elif 0.28 <= yes <= 0.42 and vol >= 10_000:
            confidence = 0.70 + min(0.08, (vol / 1_000_000) * 0.08)
            edge = (1.0 - yes) * confidence - yes * (1 - confidence) - POLY_FEE
            if edge >= MIN_EDGE and confidence >= MIN_CONFIDENCE:
                return "YES", yes, round(confidence, 3), f"Value YES@{yes:.3f} mid-range vol=${vol:,.0f}"

        # Case 4: Mid-range NO lean (YES 0.58-0.72) — buy NO as mild underdog
        elif 0.58 <= yes <= 0.72 and vol >= 10_000:
            no_price = round(1.0 - yes, 3)
            confidence = 0.70 + min(0.08, (vol / 1_000_000) * 0.08)
            edge = (1.0 - no_price) * confidence - no_price * (1 - confidence) - POLY_FEE
            if edge >= MIN_EDGE and confidence >= MIN_CONFIDENCE:
                return "NO", no_price, round(confidence, 3), f"Value NO@{no_price:.3f} mid-range vol=${vol:,.0f}"

        return None, None, None, None

    async def scan(self) -> list[dict]:
        signals = []
        now = time.time()
        markets = self._get_markets()
        
        # Track event families already signaled this cycle — one signal per underlying event
        # Prevents: holding Harvey YES@5yr + YES@10yr + YES@20yr simultaneously
        _event_families: set[str] = set()

        for market in markets:
            mid = market["_id"]
            if not mid or now - self._fired.get(mid, 0) < COOLDOWN:
                continue

            yes = market["_yes_price"]
            no  = market["_no_price"]
            vol = market["_volume"]

            if learning.is_bad_price(yes):
                continue

            side, entry, confidence, reason = self._score_market(yes, no, vol)

            if side and entry and confidence and confidence >= config.MIN_CONFIDENCE:
                self._fired[mid] = now
                q = (market.get("question") or market.get("title") or "")
                
                # One position per event family (first 35 chars of question = event fingerprint)
                event_key = q[:35].strip().lower()
                if event_key in _event_families:
                    continue  # Already have a signal on this underlying event
                _event_families.add(event_key)
                
                signals.append({
                    "strategy":    "value_bet",
                    "venue":       "polymarket",
                    "market_id":   mid,
                    "side":        side,
                    "entry_price": entry,
                    "confidence":  confidence,
                    "edge":        round((1.0 - entry) * confidence - entry * (1 - confidence) - POLY_FEE, 3),
                    "volume":      vol,
                    "timestamp":   now,
                    "days_to_expiry": market.get("_days_to_expiry", 999),
                    "reason":      f"ValueBet: {reason} | {q[:40]}",
                })
                logger.debug(f"📈 ValueBet: {reason} | {q[:40]}")

            if len(signals) >= 3:
                break

        # Prioritize shorter-duration markets — faster data feedback, less capital lock-up
        signals.sort(key=lambda s: s.get("days_to_expiry", 999))
        return signals
