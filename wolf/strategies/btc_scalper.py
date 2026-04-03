"""
Wolf Trading Bot — BTC Scalper Strategy

Three sub-strategies targeting Polymarket BTC 15-minute Up/Down markets:
  1. LateStageArb    — buy YES 0.70–0.90 when BTC moved $50+ in current window
  2. BreakoutScalper — buy YES 0.01–0.55 when BTC breakout ($40+), TP=0.75 SL=25%
  3. FlashCrash      — buy YES 0.01–0.15 after flash crash ($-25 to $-200), contrarian reversal

Each sub-strategy has:
  - Independent signal logic
  - Independent confidence floor (learning engine tracks per tag)
  - Independent cooldown
  - No cross-contamination with value_bet or other strategies

BTC price feed: shared with main Wolf BTC feed (no extra API calls).
Market targeting: specifically hunts "Bitcoin Up or Down" 15-min markets on Polymarket.
"""

import time
import logging
import json as _json
import requests
import config
from learning_engine import learning
from market_priority import fetch_prioritized_markets

logger = logging.getLogger("wolf.strategy.btc_scalper")

# ── Sub-strategy tags (learning engine tracks these independently) ─────────────
TAG_LATE_STAGE    = "late_stage_arb"
TAG_BREAKOUT      = "breakout_scalper"
TAG_FLASH_CRASH   = "flash_crash"

# ── Price trigger config ───────────────────────────────────────────────────────
LATE_STAGE_MIN_PRICE   = 0.70   # YES must be 0.70–0.90 (late-stage momentum)
LATE_STAGE_MAX_PRICE   = 0.90
LATE_STAGE_BTC_MOVE    = 50.0   # BTC moved $50+ in window

BREAKOUT_MIN_PRICE     = 0.01   # YES 0.01–0.55 (early breakout)
BREAKOUT_MAX_PRICE     = 0.55
BREAKOUT_BTC_MOVE      = 40.0   # BTC moved $40+ upward
BREAKOUT_TP            = 0.75   # Take profit at 0.75
BREAKOUT_SL_PCT        = 0.25   # Stop loss 25% below entry

FLASH_CRASH_MIN_PRICE  = 0.01   # YES 0.01–0.15 (deep underdog after crash)
FLASH_CRASH_MAX_PRICE  = 0.15
FLASH_CRASH_BTC_DROP_MIN = -200.0  # BTC dropped $25–$200
FLASH_CRASH_BTC_DROP_MAX = -25.0
FLASH_CRASH_TP         = 0.45   # Take profit at 0.45

# ── Cooldowns (per market per sub-strategy) ────────────────────────────────────
COOLDOWN_SECS = 300   # 5 min between signals on same market+tag

# ── Market fetch ──────────────────────────────────────────────────────────────
_market_cache: list[dict] = []
_market_cache_ts: float = 0.0
_MARKET_CACHE_TTL = 60  # 60s cache — BTC markets refresh fast


def _fetch_btc_markets() -> list[dict]:
    """Fetch only active BTC Up/Down 15-min markets from Polymarket."""
    global _market_cache, _market_cache_ts
    now = time.time()
    if now - _market_cache_ts < _MARKET_CACHE_TTL and _market_cache:
        return _market_cache

    try:
        markets = fetch_prioritized_markets(limit=200, max_days=2)
        if not isinstance(markets, list):
            return _market_cache

        filtered = []
        for m in markets:
            q = (m.get("question") or m.get("title") or "").lower()
            # Target specifically "Bitcoin Up or Down" 15-min markets
            if "bitcoin up or down" not in q and "btc up or down" not in q:
                continue
            # Must have 15min in title or be clearly short-duration
            if "15" not in q and "15min" not in q and "15-min" not in q:
                # Allow if slug contains 15 or market is clearly a short window
                slug = m.get("slug", "")
                if "15" not in slug:
                    continue

            op = m.get("outcomePrices", [])
            if isinstance(op, str):
                try:
                    op = _json.loads(op)
                except Exception:
                    op = []
            if not op or len(op) < 2:
                continue

            try:
                yes_p = float(op[0])
                no_p  = float(op[1])
            except (ValueError, TypeError):
                continue

            if yes_p + no_p < 0.05:
                continue  # Market zeroed out / closed

            m["_yes_price"] = yes_p
            m["_no_price"]  = no_p
            m["_id"]        = m.get("conditionId") or m.get("id", "")
            m["_question"]  = m.get("question") or m.get("title") or ""
            m["_slug"]      = m.get("slug", "")
            filtered.append(m)

        _market_cache    = filtered
        _market_cache_ts = now
        logger.debug(f"[BTC_SCALPER] Found {len(filtered)} active BTC 15-min markets")

    except Exception as e:
        logger.warning(f"[BTC_SCALPER] Market fetch error: {e}")

    return _market_cache


class BTCScalperStrategy:
    """
    Runs three independent BTC scalper sub-strategies.
    Each fires independently — no shared state between sub-strategies.
    """

    def __init__(self):
        # Per (market_id + tag) cooldown tracker
        self._fired: dict[str, float] = {}
        # Per tag TP/SL targets for exit tracking
        self._tp_targets: dict[str, float] = {
            TAG_BREAKOUT:   BREAKOUT_TP,
            TAG_FLASH_CRASH: FLASH_CRASH_TP,
            TAG_LATE_STAGE:  None,  # No TP — rides to resolution
        }

    def _cooldown_key(self, market_id: str, tag: str) -> str:
        return f"{market_id}:{tag}"

    def _on_cooldown(self, market_id: str, tag: str) -> bool:
        key = self._cooldown_key(market_id, tag)
        return time.time() - self._fired.get(key, 0) < COOLDOWN_SECS

    def _mark_fired(self, market_id: str, tag: str):
        self._fired[self._cooldown_key(market_id, tag)] = time.time()

    def _btc_window_move(self) -> float:
        """
        Get BTC price change in current 15-min window.
        Uses the shared BTC feed if available, falls back to Binance REST.
        Returns delta in USD (positive = up, negative = down).
        """
        try:
            # Try shared feed first (zero extra API calls)
            from feeds.binance_feed import btc_feed
            current = btc_feed.get_price()
            if current and current > 0:
                # Get 15-min open price via Binance kline
                resp = requests.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": "BTCUSDT", "interval": "15m", "limit": 2},
                    timeout=5,
                )
                if resp.ok:
                    klines = resp.json()
                    if klines and len(klines) >= 1:
                        # Current candle open price
                        open_price = float(klines[-1][1])
                        return current - open_price
        except Exception as e:
            logger.debug(f"[BTC_SCALPER] BTC move fetch error: {e}")
        return 0.0

    # ── Sub-strategy 1: Late Stage Arb ────────────────────────────────────────
    def _late_stage_arb(self, market: dict, btc_move: float) -> dict | None:
        """
        Buy YES when:
        - YES price is 0.70–0.90 (market already leaning YES)
        - BTC moved $50+ upward in current 15-min window
        Logic: late-stage momentum in short binary markets. Crowd is slow,
        price hasn't fully caught up to BTC move size.
        """
        if self._on_cooldown(market["_id"], TAG_LATE_STAGE):
            return None

        yes = market["_yes_price"]
        if not (LATE_STAGE_MIN_PRICE <= yes <= LATE_STAGE_MAX_PRICE):
            return None
        if btc_move < LATE_STAGE_BTC_MOVE:
            return None

        # Check learning floor
        floor = learning.get_confidence_floor(TAG_LATE_STAGE)
        # Confidence scales with how far BTC moved and how tight the entry is
        move_factor = min(0.10, (btc_move - LATE_STAGE_BTC_MOVE) / 500.0)
        confidence = min(0.92, 0.75 + move_factor)

        if confidence < max(floor, config.MIN_CONFIDENCE):
            return None

        if learning.is_bad_price(yes):
            return None

        self._mark_fired(market["_id"], TAG_LATE_STAGE)
        logger.debug(f"[LATE_STAGE_ARB] YES@{yes:.2f} BTC+${btc_move:.0f} conf={confidence:.2f}")

        return {
            "strategy":     "btc_scalper",
            "sub_strategy": TAG_LATE_STAGE,
            "venue":        "polymarket",
            "market_id":    market["_id"],
            "slug":         market["_slug"],
            "side":         "YES",
            "entry_price":  yes,
            "confidence":   round(confidence, 3),
            "edge":         round(confidence - yes, 3),
            "volume":       float(market.get("volumeNum", 0) or 0),
            "days_to_expiry": 0,  # 15-min market
            "tp_price":     None,
            "sl_price":     None,
            "timestamp":    time.time(),
            "reason":       f"LateStageArb: YES@{yes:.2f} BTC+${btc_move:.0f} | {market['_question'][:50]}",
        }

    # ── Sub-strategy 2: Breakout Scalper ──────────────────────────────────────
    def _breakout_scalper(self, market: dict, btc_move: float) -> dict | None:
        """
        Buy YES when:
        - YES price is 0.01–0.55 (early stage, crowd not yet convinced)
        - BTC moved $40+ upward
        - TP at 0.75, SL at 25% below entry
        Logic: early breakout signal before crowd prices in the move.
        """
        if self._on_cooldown(market["_id"], TAG_BREAKOUT):
            return None

        yes = market["_yes_price"]
        if not (BREAKOUT_MIN_PRICE <= yes <= BREAKOUT_MAX_PRICE):
            return None
        if btc_move < BREAKOUT_BTC_MOVE:
            return None

        floor = learning.get_confidence_floor(TAG_BREAKOUT)
        move_factor = min(0.10, (btc_move - BREAKOUT_BTC_MOVE) / 300.0)
        confidence = min(0.88, 0.72 + move_factor)

        if confidence < max(floor, config.MIN_CONFIDENCE):
            return None

        if learning.is_bad_price(yes):
            return None

        sl_price = round(yes * (1 - BREAKOUT_SL_PCT), 3)
        self._mark_fired(market["_id"], TAG_BREAKOUT)
        logger.debug(f"[BREAKOUT_SCALPER] YES@{yes:.2f} TP={BREAKOUT_TP} SL={sl_price} BTC+${btc_move:.0f}")

        return {
            "strategy":     "btc_scalper",
            "sub_strategy": TAG_BREAKOUT,
            "venue":        "polymarket",
            "market_id":    market["_id"],
            "slug":         market["_slug"],
            "side":         "YES",
            "entry_price":  yes,
            "confidence":   round(confidence, 3),
            "edge":         round(confidence - yes, 3),
            "volume":       float(market.get("volumeNum", 0) or 0),
            "days_to_expiry": 0,
            "market_end": 0,
            "tp_price":    BREAKOUT_TP,
            "sl_price":     sl_price,
            "timestamp":    time.time(),
            "reason":       f"BreakoutScalper: YES@{yes:.2f} TP={BREAKOUT_TP} SL={sl_price} BTC+${btc_move:.0f} | {market['_question'][:40]}",
        }

    # ── Sub-strategy 3: Flash Crash ────────────────────────────────────────────
    def _flash_crash(self, market: dict, btc_move: float) -> dict | None:
        """
        Buy YES when:
        - YES price is 0.01–0.15 (crowd expects continued crash)
        - BTC dropped $25–$200 in window (flash crash)
        - TP at 0.45 (mean reversion target)
        Logic: contrarian reversal — flash crashes on BTC 15-min often snap back.
        """
        if self._on_cooldown(market["_id"], TAG_FLASH_CRASH):
            return None

        yes = market["_yes_price"]
        if not (FLASH_CRASH_MIN_PRICE <= yes <= FLASH_CRASH_MAX_PRICE):
            return None
        if not (FLASH_CRASH_BTC_DROP_MIN <= btc_move <= FLASH_CRASH_BTC_DROP_MAX):
            return None

        floor = learning.get_confidence_floor(TAG_FLASH_CRASH)
        # Confidence scales with crash severity — bigger crash = stronger reversal signal
        crash_factor = min(0.08, abs(btc_move + FLASH_CRASH_BTC_DROP_MAX) / 1000.0)
        confidence = min(0.84, 0.70 + crash_factor)

        # Flash crash probation: require higher confidence until 5 real trades
        if confidence < max(floor, config.MIN_CONFIDENCE + 0.04):
            return None

        if learning.is_bad_price(yes):
            return None

        self._mark_fired(market["_id"], TAG_FLASH_CRASH)
        logger.debug(f"[FLASH_CRASH] YES@{yes:.2f} TP={FLASH_CRASH_TP} BTC${btc_move:.0f} conf={confidence:.2f}")

        return {
            "strategy":     "btc_scalper",
            "sub_strategy": TAG_FLASH_CRASH,
            "venue":        "polymarket",
            "market_id":    market["_id"],
            "slug":         market["_slug"],
            "side":         "YES",
            "entry_price":  yes,
            "confidence":   round(confidence, 3),
            "edge":         round(confidence - yes, 3),
            "volume":       float(market.get("volumeNum", 0) or 0),
            "days_to_expiry": 0,
            "market_end": 0,
            "tp_price":     FLASH_CRASH_TP,
            "sl_price":     None,
            "timestamp":    time.time(),
            "reason":       f"FlashCrash: YES@{yes:.2f} TP={FLASH_CRASH_TP} BTC${btc_move:.0f} | {market['_question'][:40]}",
        }

    # ── Main scan ──────────────────────────────────────────────────────────────
    async def scan(self) -> list[dict]:
        """
        Run all three sub-strategies against current BTC 15-min markets.
        Returns combined signal list — each sub-strategy fires independently.
        Sub-strategies can be paused individually by learning engine.
        """
        signals = []

        # Single BTC move fetch shared across all three sub-strategies
        btc_move = self._btc_window_move()
        if btc_move == 0.0:
            logger.debug("[BTC_SCALPER] BTC move unavailable — skipping scan")
            return signals

        markets = _fetch_btc_markets()
        if not markets:
            logger.debug("[BTC_SCALPER] No BTC 15-min markets found")
            return signals

        for market in markets:
            # Register slug for resolution tracking
            mid = market["_id"]
            slug = market["_slug"]
            if mid and slug:
                try:
                    from market_resolver import register_slug
                    register_slug(mid, slug)
                except Exception:
                    pass

            # Run each sub-strategy independently (skip if paused by learning engine)
            if not learning.is_strategy_paused(TAG_LATE_STAGE):
                sig1 = self._late_stage_arb(market, btc_move)
                if sig1:
                    signals.append(sig1)

            if not learning.is_strategy_paused(TAG_BREAKOUT):
                sig2 = self._breakout_scalper(market, btc_move)
                if sig2:
                    signals.append(sig2)

            if not learning.is_strategy_paused(TAG_FLASH_CRASH):
                sig3 = self._flash_crash(market, btc_move)
                if sig3:
                    signals.append(sig3)

        if signals:
            logger.info(f"[BTC_SCALPER] {len(signals)} signal(s): BTC move=${btc_move:+.0f}")

        return signals
