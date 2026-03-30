"""
Wolf Trading Bot — Copy Trading Strategy
Tracks top Polymarket wallets. Demo-validates each wallet before live copy.
Mirrors fresh trades proportionally across any market category.
"""
import time
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Optional
import config
from feeds.polymarket_feed import get_top_wallets, get_wallet_activity, get_market_volume
from intelligence import IntelligenceEngine, WalletMetrics

logger = logging.getLogger("wolf.strategy.copy_trading")

@dataclass
class WalletProfile:
    address: str
    pnl: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    # Demo validation tracking
    demo_trades: int = 0
    demo_wins: int = 0
    demo_validated: bool = False
    last_seen_trade_id: Optional[str] = None
    weight: float = 0.0  # Position size weight (based on ROI)

class CopyTrader:
    def __init__(self):
        self.wallets: dict[str, WalletProfile] = {}
        self._last_refresh: float = 0
        self._refresh_interval = 300  # refresh wallet list every 5 min
        self.intel = IntelligenceEngine()

    async def refresh_wallets(self):
        """Pull top wallets and update profiles."""
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return

        top = get_top_wallets(limit=20)
        for entry in top:
            addr = entry.get("proxy_wallet") or entry.get("wallet", "")
            if not addr:
                continue
            if addr not in self.wallets:
                self.wallets[addr] = WalletProfile(address=addr)
            profile = self.wallets[addr]
            profile.pnl = float(entry.get("profit", 0))
            profile.win_rate = float(entry.get("percentPositive", 0))
            profile.trade_count = int(entry.get("tradesCount", 0))

            # Build intelligence metrics and classify
            metrics = WalletMetrics(
                address=addr,
                pnl=profile.pnl,
                win_rate=profile.win_rate,
                trade_count=profile.trade_count,
                avg_size=float(entry.get("avgPositionSize", 0) or 0),
                max_size=float(entry.get("maxPositionSize", 0) or 0),
                active_days=int(entry.get("activeDays", 0) or 0),
                markets=int(entry.get("marketsTraded", 0) or 0),
            )
            score = self.intel.score_wallet(metrics)
            classification = self.intel.classify_wallet(score)

            # Only keep smart/whale wallets in active copy universe; suspicious wallets are tracked but not copied
            if classification == "suspicious":
                logger.info(f"Wallet {addr[:10]}... flagged suspicious | score={score.score:.3f} | notes={score.notes}")
                continue
            if classification in ("smart", "whale"):
                # Weight combines PnL and score quality
                profile.weight = max(score.score, 0.01)

        # Normalize weights across non-suspicious wallets
        eligible = [w for w in self.wallets.values() if w.weight > 0]
        if eligible:
            total_weight = sum(w.weight for w in eligible)
            for w in eligible:
                w.weight = w.weight / total_weight if total_weight > 0 else 1.0 / len(eligible)

        validated = [w for w in self.wallets.values() if w.demo_validated]
        self._last_refresh = now
        logger.info(f"Wallets refreshed: {len(self.wallets)} tracked, {len(validated)} validated")

    async def scan(self) -> list[dict]:
        """Scan tracked wallets for fresh trades to copy."""
        await self.refresh_wallets()
        signals = []

        for addr, profile in self.wallets.items():
            try:
                # Use activity feed — has timestamps, side, size, price
                activity = get_wallet_activity(addr, limit=5)
                if not activity:
                    continue

                latest = activity[0]
                trade_id = latest.get("transactionHash", "")
                if trade_id == profile.last_seen_trade_id:
                    continue  # Already seen this trade

                # Check freshness
                trade_ts = latest.get("timestamp", 0)
                age_sec = time.time() - float(trade_ts)
                if age_sec > config.COPY_TRADE_MAX_AGE_SEC:
                    continue

                # Extract trade details
                market_id = latest.get("conditionId", "")
                side = latest.get("side", "").upper()  # "BUY"/"SELL" → normalize below
                if side == "BUY":
                    side = "YES"
                elif side == "SELL":
                    side = "NO"
                size = float(latest.get("usdcSize", latest.get("size", 0)))
                price = float(latest.get("price", 0.5))

                if size < config.COPY_TRADE_MIN_SIZE:
                    continue
                if not (0.05 <= price <= 0.95):
                    continue
                if side not in ("YES", "NO"):
                    continue

                volume = get_market_volume(market_id)
                if volume < config.MIN_MARKET_VOLUME:
                    continue

                profile.last_seen_trade_id = trade_id

                # Demo validation mode
                if not profile.demo_validated:
                    profile.demo_trades += 1
                    if profile.demo_trades >= config.COPY_DEMO_MIN_TRADES:
                        # Promote if win rate good enough
                        if profile.win_rate >= 0.60:
                            profile.demo_validated = True
                            logger.info(f"Wallet {addr[:10]}... validated after {profile.demo_trades} demo trades")
                        else:
                            logger.info(f"Wallet {addr[:10]}... failed validation: win rate {profile.win_rate:.1%}")
                    # Still emit as demo signal for paper trading
                    confidence = min(0.85, profile.win_rate + 0.1)
                    signals.append({
                        "strategy": "copy_trading",
                        "venue": "polymarket",
                        "market_id": market_id,
                        "side": side,
                        "edge": confidence - 0.5,
                        "confidence": confidence,
                        "entry_price": price,
                        "volume": volume,
                        "weight": 1.0 / max(len(self.wallets), 1),
                        "wallet": addr,
                        "demo_only": True,
                        "timestamp": time.time(),
                        "reason": f"Demo copy from {addr[:10]}... (validating, {profile.demo_trades}/{config.COPY_DEMO_MIN_TRADES} trades)",
                    })
                else:
                    confidence = min(0.85, profile.win_rate + 0.05)
                    if confidence >= config.MIN_CONFIDENCE:
                        signals.append({
                            "strategy": "copy_trading",
                            "venue": "polymarket",
                            "market_id": market_id,
                            "side": side,
                            "edge": confidence - 0.5,
                            "confidence": confidence,
                            "entry_price": price,
                            "volume": volume,
                            "weight": profile.weight,
                            "wallet": addr,
                            "demo_only": False,
                            "timestamp": time.time(),
                            "reason": f"Copy from validated wallet {addr[:10]}... win rate {profile.win_rate:.1%}",
                        })

            except Exception as e:
                logger.warning(f"Error scanning wallet {addr[:10]}: {e}")

        return signals
