"""
Wolf Trading Bot — Intelligence Layer
Safe pattern absorption from external research:
- wallet scoring (poly_data concept)
- whale / suspicious wallet classification (polyterm concept)
- insider-style anomaly scoring (insider-tracker concept)

This module stays custom. No external repo code copied.
"""
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class WalletMetrics:
    address: str
    pnl: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    avg_size: float = 0.0
    max_size: float = 0.0
    active_days: int = 0
    markets: int = 0
    recent_sizes: list[float] = field(default_factory=list)
    recent_market_volumes: list[float] = field(default_factory=list)

@dataclass
class WalletScore:
    address: str
    score: float
    sharpe_like: float
    consistency: float
    size_discipline: float
    whale: bool = False
    suspicious: bool = False
    anomaly_score: float = 0.0
    notes: list[str] = field(default_factory=list)

class IntelligenceEngine:
    """Scores wallets and classifies behavior for copy trading + alerts."""

    def score_wallet(self, metrics: WalletMetrics) -> WalletScore:
        notes = []

        # Sharpe-like proxy: win rate * log(trades) * pnl stability
        trade_factor = math.log(max(metrics.trade_count, 1) + 1)
        pnl_factor = math.tanh(metrics.pnl / 10000.0)
        sharpe_like = metrics.win_rate * trade_factor * (1 + pnl_factor)

        # Consistency: reward many active days and many trades
        consistency = min(1.0, (metrics.active_days / 30.0) * 0.5 + (metrics.trade_count / 500.0) * 0.5)

        # Size discipline: penalize wildly oversized outliers
        size_discipline = 1.0
        if metrics.recent_sizes and len(metrics.recent_sizes) >= 3:
            mean_size = statistics.mean(metrics.recent_sizes)
            if mean_size > 0:
                outlier_ratio = metrics.max_size / mean_size
                if outlier_ratio > 10:
                    size_discipline -= 0.4
                    notes.append(f"Position sizing erratic ({outlier_ratio:.1f}x outlier)")
                elif outlier_ratio > 5:
                    size_discipline -= 0.2
                    notes.append(f"Position sizing moderately erratic ({outlier_ratio:.1f}x outlier)")

        score = sharpe_like * 0.5 + consistency * 0.3 + size_discipline * 0.2

        # Whale classification
        whale = metrics.avg_size >= 500 or metrics.max_size >= 2000
        if whale:
            notes.append("Whale wallet")

        # Suspicious classification
        suspicious = False
        anomaly_score = 0.0
        if metrics.trade_count < 20 and metrics.pnl > 10000:
            suspicious = True
            anomaly_score += 0.4
            notes.append("High PnL on very low trade count")
        if metrics.max_size > max(metrics.avg_size * 15, 3000):
            suspicious = True
            anomaly_score += 0.3
            notes.append("Unusual single-trade size spike")
        if metrics.markets <= 2 and metrics.pnl > 5000:
            anomaly_score += 0.2
            notes.append("Concentrated profits in very few markets")
        if metrics.recent_market_volumes and len(metrics.recent_market_volumes) >= 3:
            illiquid_count = len([v for v in metrics.recent_market_volumes if v < 10000])
            if illiquid_count >= 2:
                anomaly_score += 0.2
                notes.append("Trading in low-liquidity markets")

        return WalletScore(
            address=metrics.address,
            score=round(score, 4),
            sharpe_like=round(sharpe_like, 4),
            consistency=round(consistency, 4),
            size_discipline=round(size_discipline, 4),
            whale=whale,
            suspicious=suspicious,
            anomaly_score=round(min(anomaly_score, 1.0), 4),
            notes=notes,
        )

    def classify_wallet(self, score: WalletScore) -> str:
        if score.suspicious and score.anomaly_score >= 0.5:
            return "suspicious"
        if score.whale:
            return "whale"
        if score.score >= 1.0:
            return "smart"
        return "standard"
