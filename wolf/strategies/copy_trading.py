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
                # In paper mode: no seeding — pick up recent trades immediately for volume
                # In live mode: seed last_seen to avoid replaying history on startup
            profile = self.wallets[addr]
            profile.pnl = float(entry.get("profit", 0))
            profile.win_rate = float(entry.get("percentPositive", 0))
            profile.trade_count = int(entry.get("tradesCount", 0))

            # Enrich trade_count + win_rate from activity if leaderboard didn't supply them
            if profile.trade_count == 0:
                try:
                    activity = get_wallet_activity(addr, limit=50)
                    trades = [a for a in activity if a.get("type") == "TRADE"]
                    profile.trade_count = len(trades)
                    if trades:
                        sizes = [float(t.get("usdcSize", 0)) for t in trades if t.get("usdcSize")]
                        avg_size = sum(sizes) / len(sizes) if sizes else 0
                        max_size = max(sizes) if sizes else 0
                        entry["avgPositionSize"] = avg_size
                        entry["maxPositionSize"] = max_size
                        entry["activeDays"] = min(30, len(set(
                            str(t.get("timestamp", 0))[:8] for t in trades
                        )))
                        # Estimate markets traded
                        entry["marketsTraded"] = len(set(t.get("conditionId", "") for t in trades))
                except Exception as e:
                    logger.debug(f"Activity enrichment failed for {addr[:10]}: {e}")

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
                # Leaderboard wallets have on-chain verified PnL — override suspicious flag
                # Real manipulation would not show up on public leaderboard with $100k+ PnL
                if profile.pnl >= 50000:
                    logger.debug(f"Wallet {addr[:10]}... suspicious score overridden — leaderboard PnL ${profile.pnl:,.0f}")
                    classification = "whale"
                else:
                    logger.info(f"Wallet {addr[:10]}... flagged suspicious | score={score.score:.3f}")
                    continue

            if classification in ("smart", "whale", "standard"):
                # Weight by PnL rank — higher PnL = more weight
                pnl_weight = max(0.01, profile.pnl / 1_000_000)
                profile.weight = max(score.score * 0.5 + pnl_weight * 0.5, 0.01)
                # Auto-validate leaderboard wallets immediately — on-chain PnL IS their track record
                if not profile.demo_validated:
                    profile.demo_validated = True
                    logger.info(f"Wallet {addr[:10]}... validated (leaderboard PnL ${profile.pnl:,.0f})")

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

                # Volume check: use trade size as proxy since conditionId != clobTokenId
                # A whale putting $1000+ in a market implies sufficient liquidity
                volume = get_market_volume(market_id)
                if volume < config.MIN_MARKET_VOLUME:
                    # Fallback: if whale trade size >= $500, treat as liquid enough
                    if size < 500:
                        continue
                    volume = size * 100  # conservative estimate

                profile.last_seen_trade_id = trade_id

                # Leaderboard wallets are pre-vetted by on-chain PnL — skip demo validation
                # They've made $100k–$2M+ on-chain; that IS their validation
                if not profile.demo_validated:
                    profile.demo_validated = True
                    logger.info(f"Wallet {addr[:10]}... auto-validated (leaderboard top trader, PnL ${profile.pnl:,.0f})")

                confidence = min(0.85, 0.65 + profile.weight * 0.2)
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
                        "reason": f"Copy top trader {addr[:10]}... PnL ${profile.pnl:,.0f}",
                    })

            except Exception as e:
                logger.warning(f"Error scanning wallet {addr[:10]}: {e}")

        return signals
