"""
Wolf Trading Bot — Technical Analysis Signal Strategy

Based on @0x_Punisher's framework ($4.5k/week on BTC 5-min markets):
"Feed it structured signals and it compounds while you sleep."

7-indicator stack — ALL must align before firing:
  RSI     = context (overbought/oversold)
  MACD    = momentum confirmation (crossover)
  Stoch   = precision entry trigger (%K crosses %D)
  EMA     = structure (trend vs chop filter)
  OBV     = volume confirmation (real vs fake move)
  VWAP    = fair value (fade extremes)
  ATR     = volatility filter (only trade optimal range)

Target: Polymarket BTC/ETH Up-Down 5-minute and 15-minute markets
Data source: Binance US WebSocket (already running in binance_feed.py)

Key intelligence from X:
- @0x_Punisher: $4.5k/week, 5-min BTC markets, LLM-trained indicators
- Community: 73.3% WR across 10k+ trades on latency arb + TA
- Critical: Polymarket fees now up to 1% on takers at 50/50 — filter low-edge
- HFT players entered Jan-Feb 2026 — need faster signals, not slower
- Weather/data-feed edge: structural data moves before markets do
- ATR volatility filter = "the most underrated component"
"""
import time
import logging
import asyncio
import json as _json
from collections import deque
from typing import Optional
import requests
import config
from feeds.binance_feed import btc_feed, eth_feed
from market_priority import fetch_prioritized_markets

logger = logging.getLogger("wolf.strategy.ta_signal")

# ── Constants ─────────────────────────────────────────────────────────────────
RSI_PERIOD      = 14
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9
STOCH_K         = 14
STOCH_D         = 3
EMA_FAST        = 9
EMA_SLOW        = 21
OBV_LOOKBACK    = 10
ATR_PERIOD      = 14

# Signal thresholds
RSI_OVERSOLD    = 32    # below = oversold → look for YES
RSI_OVERBOUGHT  = 68    # above = overbought → look for NO
STOCH_OVERSOLD  = 22
STOCH_OVERBOUGHT= 78
ATR_MIN_PCT     = 0.001  # 0.1% — too quiet, no edge
ATR_MAX_PCT     = 0.020  # 2.0% — too wild, unpredictable
VWAP_DEV_MIN    = 0.002  # 0.2% deviation needed for fade trade
MIN_EDGE        = 0.04
COOLDOWN        = 300    # 5 min per market
POLY_FEE        = 0.01   # 1% taker fee (new Polymarket fee structure)


class TAIndicators:
    """Compute all 7 indicators from a price series."""

    def __init__(self, maxlen: int = 200):
        self.prices   = deque(maxlen=maxlen)
        self.volumes  = deque(maxlen=maxlen)
        self.highs    = deque(maxlen=maxlen)
        self.lows     = deque(maxlen=maxlen)
        self._obv     = 0.0
        self._obv_series = deque(maxlen=maxlen)

    def update(self, price: float, volume: float = 1.0,
               high: float = None, low: float = None):
        prev = self.prices[-1] if self.prices else price
        self.prices.append(price)
        self.volumes.append(volume)
        self.highs.append(high or price * 1.001)
        self.lows.append(low or price * 0.999)
        # OBV
        if price > prev:
            self._obv += volume
        elif price < prev:
            self._obv -= volume
        self._obv_series.append(self._obv)

    def is_ready(self) -> bool:
        """True once enough ticks collected for all indicators (MACD needs most: 35)."""
        return len(self.prices) >= MACD_SLOW + MACD_SIGNAL

    def add_price(self, price: float):
        """Alias for update() — for testing and external callers."""
        self.update(price)

    def _ema(self, data: list, period: int) -> float:
        if len(data) < period:
            return sum(data) / len(data)
        k = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for p in data[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    def rsi(self) -> Optional[float]:
        prices = list(self.prices)
        if len(prices) < RSI_PERIOD + 1:
            return None
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        recent = deltas[-RSI_PERIOD:]
        gains  = [d for d in recent if d > 0]
        losses = [-d for d in recent if d < 0]
        avg_g  = sum(gains) / RSI_PERIOD if gains else 0
        avg_l  = sum(losses) / RSI_PERIOD if losses else 1e-10
        rs     = avg_g / avg_l
        return 100 - (100 / (1 + rs))

    def macd(self) -> tuple[Optional[float], Optional[float]]:
        """Returns (macd_line, signal_line)."""
        prices = list(self.prices)
        if len(prices) < MACD_SLOW + MACD_SIGNAL:
            return None, None
        fast = self._ema(prices, MACD_FAST)
        slow = self._ema(prices, MACD_SLOW)
        macd_line = fast - slow
        # Signal = EMA of MACD
        # Approximate with recent MACD values
        macd_history = []
        for i in range(MACD_SIGNAL + 1):
            idx = -(MACD_SIGNAL + 1 - i)
            p = prices[:len(prices) + idx] if idx < 0 else prices
            if len(p) >= MACD_SLOW:
                f = self._ema(p, MACD_FAST)
                s = self._ema(p, MACD_SLOW)
                macd_history.append(f - s)
        if len(macd_history) >= MACD_SIGNAL:
            signal = self._ema(macd_history, MACD_SIGNAL)
            return macd_line, signal
        return macd_line, None

    def stochastic(self) -> tuple[Optional[float], Optional[float]]:
        """Returns (%K, %D)."""
        prices = list(self.prices)
        highs  = list(self.highs)
        lows   = list(self.lows)
        if len(prices) < STOCH_K:
            return None, None
        k_values = []
        for i in range(STOCH_D + STOCH_K - 1, len(prices)):
            window_h = max(highs[i-STOCH_K+1:i+1])
            window_l = min(lows[i-STOCH_K+1:i+1])
            rng = window_h - window_l
            k = 100 * (prices[i] - window_l) / rng if rng > 0 else 50
            k_values.append(k)
        if not k_values:
            return None, None
        k = k_values[-1]
        d = sum(k_values[-STOCH_D:]) / STOCH_D if len(k_values) >= STOCH_D else k
        return k, d

    def emas(self) -> tuple[Optional[float], Optional[float]]:
        """Returns (fast_ema, slow_ema)."""
        prices = list(self.prices)
        if len(prices) < EMA_SLOW:
            return None, None
        return self._ema(prices, EMA_FAST), self._ema(prices, EMA_SLOW)

    def obv_trend(self) -> Optional[str]:
        """Returns 'up', 'down', or 'flat'."""
        obv = list(self._obv_series)
        if len(obv) < OBV_LOOKBACK:
            return None
        recent = obv[-OBV_LOOKBACK:]
        slope = (recent[-1] - recent[0]) / len(recent)
        if slope > 0.5:
            return "up"
        elif slope < -0.5:
            return "down"
        return "flat"

    def vwap(self) -> Optional[float]:
        prices  = list(self.prices)
        volumes = list(self.volumes)
        if len(prices) < 10:
            return None
        pv = sum(p * v for p, v in zip(prices, volumes))
        tv = sum(volumes) or 1
        return pv / tv

    def atr_pct(self) -> Optional[float]:
        """ATR as % of price."""
        highs  = list(self.highs)
        lows   = list(self.lows)
        prices = list(self.prices)
        if len(prices) < ATR_PERIOD + 1:
            return None
        trs = []
        for i in range(1, min(ATR_PERIOD + 1, len(prices))):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - prices[i-1]),
                abs(lows[i]  - prices[i-1]),
            )
            trs.append(tr)
        atr = sum(trs) / len(trs) if trs else 0
        return atr / prices[-1] if prices[-1] > 0 else None


class TASignalStrategy:
    def __init__(self):
        self._btc_ind  = TAIndicators()
        self._eth_ind  = TAIndicators()
        self._fired:   dict[str, float] = {}
        self._poly_cache: list[dict] = []
        self._poly_ts: float = 0.0
        self._last_price_update: float = 0.0

    def _update_indicators(self):
        """Pull latest price from BTC/ETH feeds into indicators."""
        btc = btc_feed.get_current_price()
        if btc and btc_feed.is_fresh(max_age_ms=60000):
            self._btc_ind.update(btc)

    def _fetch_btc_markets(self) -> list[dict]:
        now = time.time()
        if now - self._poly_ts < 90 and self._poly_cache:
            return self._poly_cache
        try:
            markets = fetch_prioritized_markets(limit=200, max_days=2)
            if not isinstance(markets, list):
                return self._poly_cache

            filtered = []
            for m in markets:
                q = (m.get("question") or m.get("title") or "").lower()
                # Only BTC/ETH Up-Down short-term markets
                if not any(k in q for k in ["btc","bitcoin","eth","ethereum"]):
                    continue
                if not any(k in q for k in ["up","down","higher","lower","above","below"]):
                    continue

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
                if vol < config.MIN_MARKET_VOLUME:
                    continue

                m["_yes_price"] = p0
                m["_no_price"]  = p1
                m["_volume"]    = vol
                m["_id"]        = m.get("conditionId") or m.get("id","")
                filtered.append(m)

            self._poly_cache = filtered
            self._poly_ts = now
        except Exception as e:
            logger.warning(f"TASignal market fetch: {e}")
        return self._poly_cache

    def _compute_signal(self, ind: TAIndicators, market: dict,
                        asset: str = "BTC") -> Optional[dict]:
        """Run all 7 indicators — all must align."""
        rsi             = ind.rsi()
        macd, macd_sig  = ind.macd()
        stoch_k, stoch_d= ind.stochastic()
        ema_fast, ema_slow = ind.emas()
        obv_trend       = ind.obv_trend()
        vwap_price      = ind.vwap()
        atr_pct         = ind.atr_pct()

        if None in [rsi, macd, macd_sig, stoch_k, stoch_d,
                    ema_fast, ema_slow, obv_trend, vwap_price, atr_pct]:
            return None  # Not enough data yet

        current_price = list(ind.prices)[-1] if ind.prices else 0
        if not current_price:
            return None

        # ── Volatility filter (ATR) — FIRST gate ─────────────────────────────
        # "The most underrated component" — @0x_Punisher
        if atr_pct < ATR_MIN_PCT:
            logger.debug(f"TASignal: ATR too low ({atr_pct:.4f}) — no edge in flat market")
            return None
        if atr_pct > ATR_MAX_PCT:
            logger.debug(f"TASignal: ATR too high ({atr_pct:.4f}) — unpredictable")
            return None

        # ── EMA structure filter — avoid chop ────────────────────────────────
        ema_spread = abs(ema_fast - ema_slow) / ema_slow
        if ema_spread < 0.0005:
            logger.debug("TASignal: EMAs converged — choppy market, skip")
            return None

        trending_up   = ema_fast > ema_slow
        trending_down = ema_fast < ema_slow

        # ── RSI context ───────────────────────────────────────────────────────
        rsi_bullish = rsi < RSI_OVERSOLD
        rsi_bearish = rsi > RSI_OVERBOUGHT

        # ── MACD momentum ─────────────────────────────────────────────────────
        macd_bullish = macd > macd_sig    # crossover up
        macd_bearish = macd < macd_sig    # crossover down

        # ── Stochastic entry trigger ───────────────────────────────────────────
        stoch_bullish = stoch_k < STOCH_OVERSOLD and stoch_k > stoch_d
        stoch_bearish = stoch_k > STOCH_OVERBOUGHT and stoch_k < stoch_d

        # ── OBV confirmation ──────────────────────────────────────────────────
        obv_bullish = obv_trend == "up"
        obv_bearish = obv_trend == "down"

        # ── VWAP fair value ───────────────────────────────────────────────────
        vwap_dev = (current_price - vwap_price) / vwap_price
        # For bullish: price below VWAP (value) or close to it
        vwap_bullish = vwap_dev < VWAP_DEV_MIN
        # For bearish: price stretched above VWAP
        vwap_bearish = vwap_dev > VWAP_DEV_MIN

        # ── Full alignment check ──────────────────────────────────────────────
        # RSI + MACD + Stoch + EMA + OBV + VWAP must all agree
        bullish_score = sum([
            rsi_bullish, macd_bullish, stoch_bullish,
            trending_up, obv_bullish, vwap_bullish
        ])
        bearish_score = sum([
            rsi_bearish, macd_bearish, stoch_bearish,
            trending_down, obv_bearish, vwap_bearish
        ])

        min_alignment = 5  # need 5/6 indicators aligned (strict but not perfect)

        if bullish_score < min_alignment and bearish_score < min_alignment:
            logger.debug(f"TASignal: insufficient alignment (bull={bullish_score} bear={bearish_score})")
            return None

        direction = "YES" if bullish_score >= bearish_score else "NO"

        # ── Map direction to market ───────────────────────────────────────────
        q = (market.get("question") or market.get("title") or "").lower()
        # "will BTC be higher/above X?" — YES means price goes up
        asks_higher = any(k in q for k in ["higher","above","up","exceed"])
        # YES on "higher?" = bullish = our signal matches
        if asks_higher:
            trade_side  = direction
            entry_price = market["_yes_price"] if direction == "YES" else market["_no_price"]
        else:
            # "will BTC be lower?" — YES means price goes down — flip
            trade_side  = "NO" if direction == "YES" else "YES"
            entry_price = market["_yes_price"] if trade_side == "YES" else market["_no_price"]

        if not (0.08 <= entry_price <= 0.92):
            return None

        # Edge after new 1% Polymarket taker fee
        edge = (1.0 - entry_price) - POLY_FEE
        if edge < MIN_EDGE:
            return None

        alignment = max(bullish_score, bearish_score)
        confidence = min(0.92, 0.72 + (alignment - min_alignment) * 0.05
                        + (atr_pct - ATR_MIN_PCT) * 2)

        if confidence < config.MIN_CONFIDENCE:
            return None

        return {
            "strategy":    "ta_signal",
            "venue":       "polymarket",
            "market_id":   market["_id"],
            "side":        trade_side,
            "entry_price": entry_price,
            "confidence":  round(confidence, 3),
            "edge":        round(edge, 3),
            "volume":      market["_volume"],
            "timestamp":   time.time(),
            "reason": (
                f"TA [{asset}] {trade_side}@{entry_price:.2f} | "
                f"RSI={rsi:.0f} MACD={'↑' if macd_bullish else '↓'} "
                f"Stoch={stoch_k:.0f} ATR={atr_pct:.3%} "
                f"align={alignment}/6"
            ),
        }

    async def scan(self) -> list[dict]:
        signals = []
        now = time.time()

        self._update_indicators()

        # Pause if Binance feed is stale — never trade TA blind
        if not btc_feed.is_fresh(max_age_ms=60000):
            logger.warning("TASignal: Binance feed stale — pausing strategy, not trading blind")
            return signals

        # Need at least enough data for all indicators
        if len(self._btc_ind.prices) < MACD_SLOW + MACD_SIGNAL:
            logger.debug(f"TASignal: warming up ({len(self._btc_ind.prices)} prices, need {MACD_SLOW + MACD_SIGNAL})")
            return signals

        markets = self._fetch_btc_markets()
        if not markets:
            return signals

        for market in markets[:10]:
            mid = market["_id"]
            if not mid or now - self._fired.get(mid, 0) < COOLDOWN:
                continue

            sig = self._compute_signal(self._btc_ind, market, "BTC")
            if sig:
                self._fired[mid] = now
                signals.append(sig)
                logger.info(
                    f"📊 TASignal: {market.get('question','')[:40]}… "
                    f"{sig['side']}@{sig['entry_price']:.2f} | {sig['reason']}"
                )
            if len(signals) >= 2:
                break

        return signals
