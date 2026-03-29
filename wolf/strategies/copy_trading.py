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
from feeds.polymarket_feed import get_top_wallets, get_wallet_positions, get_market_volume

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

    async def refresh_wallets(self):
        """Pull top wallets and update profiles."""
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return

        top = get_top_wallets(limit=15)
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

        # Compute weights based on ROI rank
        validated = [w for w in self.wallets.values() if w.demo_validated]
        if validated:
            total_pnl = sum(max(w.pnl, 0) for w in validated)
            for w in validated:
                w.weight = max(w.pnl, 0) / total_pnl if total_pnl > 0 else 1.0 / len(validated)

        self._last_refresh = now
        logger.info(f"Wallets refreshed: {len(self.wallets)} tracked, {len(validated)} validated")

    async def scan(self) -> list[dict]:
        """Scan tracked wallets for fresh trades to copy."""
        await self.refresh_wallets()
        signals = []

        for addr, profile in self.wallets.items():
            try:
                positions = get_wallet_positions(addr, limit=5)
                if not positions:
                    continue

                latest = positions[0]
                trade_id = latest.get("id", "")
                if trade_id == profile.last_seen_trade_id:
                    continue  # Already seen this trade

                # Check freshness
                trade_ts = latest.get("timestamp", 0)
                if isinstance(trade_ts, str):
                    from datetime import datetime
                    try:
                        trade_ts = datetime.fromisoformat(trade_ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        trade_ts = 0
                age_sec = time.time() - float(trade_ts)
                if age_sec > config.COPY_TRADE_MAX_AGE_SEC:
                    continue

                # Extract trade details
                market_id = latest.get("market", "")
                side = latest.get("side", "").upper()
                size = float(latest.get("size", 0))
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
