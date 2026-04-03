"""
Wolf Trading Bot — Kalshi Copy Trading Strategy
Mirrors Kalshi's top performers. Same logic as Polymarket copy trading
but adapted for Kalshi's market structure and API.

Key Kalshi differences:
- Markets are US-regulated — more "real world" events (macro, sports, news)
- Different user base = different mispricing patterns
- Fees ~1% built into spread — factor into edge calc
- Prices in cents (0–100) normalized to 0.0–1.0

Strategy:
1. Fetch Kalshi leaderboard (top PnL wallets)
2. Monitor their recent trades
3. Mirror trades meeting confidence threshold
4. Prioritize near-expiry markets with high-confidence outcomes
"""
import time
import logging
import asyncio
import requests
from dataclasses import dataclass, field
from typing import Optional
import config
from feeds.kalshi_feed import get_active_markets, KALSHI_API_BASE
from learning_engine import learning

logger = logging.getLogger("wolf.strategy.kalshi_copy")

KALSHI_FEE  = 0.01   # ~1% fee estimate
COOLDOWN    = 600    # 10 min per market
MIN_CONF    = 0.72


@dataclass
class KalshiWallet:
    user_id: str
    pnl: float = 0.0
    win_rate: float = 0.0
    trades: int = 0
    validated: bool = False


class KalshiCopyTrader:
    def __init__(self):
        self._wallets: dict[str, KalshiWallet] = {}
        self._last_refresh: float = 0.0
        self._refresh_ttl:  float = 600   # 10 min
        self._fired:        dict[str, float] = {}
        self._market_cache: list[dict] = []
        self._market_ts:    float = 0.0

    def _fetch_leaderboard(self) -> list[dict]:
        """Fetch Kalshi leaderboard — top traders by PnL."""
        try:
            resp = requests.get(
                f"{KALSHI_API_BASE}/leaderboard",
                params={"limit": 20},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                return data.get("leaderboard", []) or data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Kalshi leaderboard: {e}")
        return []

    def _fetch_user_positions(self, user_id: str) -> list[dict]:
        """Fetch recent positions for a Kalshi user."""
        try:
            resp = requests.get(
                f"{KALSHI_API_BASE}/portfolio/positions",
                params={"user_id": user_id, "limit": 10},
                timeout=8,
            )
            if resp.ok:
                data = resp.json()
                return data.get("market_positions", []) or []
        except Exception as e:
            logger.debug(f"Kalshi positions {user_id}: {e}")
        return []

    async def refresh_wallets(self):
        now = time.time()
        if now - self._last_refresh < self._refresh_ttl:
            return
        self._last_refresh = now

        leaders = self._fetch_leaderboard()
        for entry in leaders[:15]:
            uid = entry.get("user_id") or entry.get("id", "")
            if not uid:
                continue
            pnl = float(entry.get("total_pnl", entry.get("pnl", 0)) or 0)
            if uid not in self._wallets:
                self._wallets[uid] = KalshiWallet(
                    user_id=uid, pnl=pnl,
                    validated=(pnl > 5000),
                )
            else:
                self._wallets[uid].pnl = pnl

        if self._wallets:
            logger.info(f"Kalshi wallets: {len(self._wallets)} tracked")

    async def scan(self) -> list[dict]:
        signals = []
        await self.refresh_wallets()
        now = time.time()

        # Refresh market cache
        if now - self._market_ts > 180:
            self._market_cache = get_active_markets(limit=50)
            self._market_ts = now

        if not self._market_cache:
            return signals

        # Build market lookup
        market_map = {m["_ticker"]: m for m in self._market_cache}

        for uid, wallet in self._wallets.items():
            if not wallet.validated:
                continue
            try:
                positions = self._fetch_user_positions(uid)
                for pos in positions[:3]:
                    ticker = pos.get("ticker", "")
                    if not ticker or ticker not in market_map:
                        continue
                    if now - self._fired.get(ticker, 0) < COOLDOWN:
                        continue

                    market = market_map[ticker]
                    side = "YES" if pos.get("position", 0) > 0 else "NO"
                    entry_price = market["_yes_ask"] if side == "YES" else market["_no_ask"]

                    if not (0.08 <= entry_price <= 0.88):
                        continue

                    # Skip bad price ranges learned from Polymarket
                    if learning.is_bad_price(entry_price):
                        continue

                    # Edge after Kalshi fee
                    edge = max(0, (1.0 - entry_price) - KALSHI_FEE - 0.02)
                    if edge <= 0.05:
                        continue

                    confidence = min(0.88, 0.70 + (wallet.pnl / 100_000) * 0.05)
                    confidence = max(confidence, learning.get_confidence_floor("kalshi_copy"))

                    if confidence < MIN_CONF:
                        continue

                    vol = market.get("_volume", 0)
                    self._fired[ticker] = now

                    signals.append({
                        "strategy":    "kalshi_copy",
                        "venue":       "kalshi",
                        "market_id":   ticker,
                        "side":        side,
                        "entry_price": entry_price,
                        "confidence":  confidence,
                        "edge":        edge,
                        "volume":      vol,
                        "days_to_expiry": 0,
                        "market_end": 0,
                        "timestamp":   now,
                        "reason": (
                            f"Kalshi copy: {uid[:12]}… PnL ${wallet.pnl:,.0f} | "
                            f"{market['_title'][:40]} {side}@{entry_price:.2f}"
                        ),
                    })

                if len(signals) >= 3:
                    break
            except Exception as e:
                logger.debug(f"Kalshi scan {uid}: {e}")

        return signals
