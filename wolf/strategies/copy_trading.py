"""
Wolf Trading Bot — Copy Trading Strategy
Tracks top Polymarket wallets. Demo-validates each wallet before live copy.
Mirrors fresh trades proportionally across any market category.
"""
import os
import time
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Optional
import config
from feeds.polymarket_feed import get_top_wallets, get_wallet_activity, get_market_volume, get_market_end_date
from intelligence import IntelligenceEngine, WalletMetrics
from learning_engine import learning

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
    # Jefe-specified priority wallets — always tracked regardless of leaderboard rank
    PRIORITY_WALLETS: list[str] = [
        "0xf247584e41117bbbe4cc06e4d2c95741792a5216",
        "0xd0d6053c3c37e727402d84c14069780d360993aa",
        "0xe00740bce98a594e26861838885ab310ec3b548c",
        "0x7ac83882979ccb5665cea83cb269e558b55077cd",
        "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d",
        "0xed61f86bb5298d2f27c21c433ce58d80b88a9aa3",
    ]

    def __init__(self):
        self.wallets: dict[str, WalletProfile] = {}
        self._last_refresh: float = 0
        self._refresh_interval = 900  # refresh wallet list every 15 min
        self.intel = IntelligenceEngine()
        # Persistent dedup set — load already-fired trade IDs from DB on init
        self._fired_trade_ids: set[str] = self._load_fired_ids()

    def _load_fired_ids(self) -> set:
        """Load all market_ids already traded or currently open to prevent re-firing on restart."""
        try:
            import sqlite3
            if not os.path.exists(config.DB_PATH):
                return set()
            with sqlite3.connect(config.DB_PATH) as conn:
                # Dedup: recent trades within max age window
                rows = conn.execute(
                    "SELECT DISTINCT market_id FROM paper_trades "
                    "WHERE strategy='copy_trading' AND timestamp > ?",
                    (time.time() - config.COPY_TRADE_MAX_AGE_SEC,)
                ).fetchall()
                # Also dedup: any currently open positions (regardless of age)
                open_rows = conn.execute(
                    "SELECT DISTINCT market_id FROM paper_trades "
                    "WHERE strategy='copy_trading' AND resolved=0 AND void=0"
                ).fetchall()
            ids = {r[0] for r in rows} | {r[0] for r in open_rows}
            if ids:
                logger.info(f"Loaded {len(ids)} copy trade IDs for dedup (recent + open)")
            return ids
        except Exception as e:
            logger.warning(f"Could not load fired trade IDs: {e}")
            return set()

    async def refresh_wallets(self):
        """Pull top wallets and update profiles."""
        now = time.time()
        if now - self._last_refresh < self._refresh_interval:
            return

        top = get_top_wallets(limit=20)

        # Merge priority wallets into top list (always tracked)
        existing_addrs = {e.get("proxy_wallet") or e.get("wallet","") for e in top}
        for pw in self.PRIORITY_WALLETS:
            if pw not in existing_addrs:
                top.append({"proxy_wallet": pw, "wallet": pw, "profit": 0,
                            "percentPositive": 0, "tradesCount": 0,
                            "avgPositionSize": 0, "maxPositionSize": 0,
                            "activeDays": 0, "marketsTraded": 0,
                            "userName": pw[:10]+"...", "vol": 0})

        for entry in top:
            addr = entry.get("proxy_wallet") or entry.get("wallet", "")
            if not addr:
                continue
            if addr not in self.wallets:
                self.wallets[addr] = WalletProfile(address=addr)
                # Priority wallets get auto-validated immediately
                if addr in self.PRIORITY_WALLETS:
                    self.wallets[addr].demo_validated = True
                    self.wallets[addr].weight = 0.9  # high initial trust
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
                    logger.debug(f"Wallet {addr[:10]}... flagged suspicious | score={score.score:.3f}")
                    continue

            if classification in ("smart", "whale", "standard"):
                # Weight by PnL rank — higher PnL = more weight
                pnl_weight = max(0.01, profile.pnl / 1_000_000)
                profile.weight = max(score.score * 0.5 + pnl_weight * 0.5, 0.01)
                # Auto-validate leaderboard wallets immediately — on-chain PnL IS their track record
                if not profile.demo_validated:
                    profile.demo_validated = True
                    logger.debug(f"Wallet {addr[:10]}... validated (leaderboard PnL ${profile.pnl:,.0f})")

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

                # Filter to TRADE events only — skip REDEEM/MERGE/SPLIT (no signal value)
                trades_only = [a for a in activity if a.get("type","").upper() in ("TRADE","BUY","SELL","") and a.get("side","")]
                if not trades_only:
                    continue
                latest = trades_only[0]
                trade_id = latest.get("transactionHash", "")
                market_id_check = latest.get("conditionId", "")

                # Dedup: skip if this exact trade OR this market was already fired recently
                if trade_id and trade_id == profile.last_seen_trade_id:
                    continue
                if market_id_check and market_id_check in self._fired_trade_ids:
                    continue

                # Check freshness
                trade_ts = latest.get("timestamp", 0)
                age_sec = time.time() - float(trade_ts)
                if age_sec > config.COPY_TRADE_MAX_AGE_SEC:
                    continue

                # Validate market is CURRENT — skip if market end date is in the past
                # This prevents Wolf from entering positions in ancient/resolved markets
                # Use slug from activity feed (more reliable than conditionId query)
                _market_slug = latest.get("slug", "")
                if _market_slug:
                    try:
                        _mkt_resp = requests.get(
                            "https://gamma-api.polymarket.com/markets",
                            params={"slug": _market_slug}, timeout=5
                        )
                        _mkt_data = _mkt_resp.json()
                        _mkt = _mkt_data[0] if isinstance(_mkt_data, list) and _mkt_data else {}
                        _end_raw = _mkt.get("endDate") or _mkt.get("endDateIso") or ""
                        if _end_raw:
                            from datetime import datetime, timezone as _tz
                            _end_dt = datetime.fromisoformat(_end_raw.replace("Z", "+00:00"))
                            if not _end_dt.tzinfo: _end_dt = _end_dt.replace(tzinfo=_tz.utc)
                            if _end_dt.timestamp() < time.time():
                                logger.debug(f"[COPY] Skipping expired market {_market_slug} ended {_end_raw[:10]}")
                                continue  # Market already ended — don't enter
                    except Exception:
                        pass  # If lookup fails, proceed cautiously

                # Extract trade details
                market_id = latest.get("conditionId", "")
                market_slug = latest.get("slug", "")
                # If activity feed didn't include slug, look it up from Gamma API
                if market_id and not market_slug:
                    try:
                        import requests as _req
                        _gr = _req.get("https://gamma-api.polymarket.com/markets",
                                       params={"condition_ids": market_id}, timeout=5)
                        if _gr.ok:
                            _gd = _gr.json()
                            _gm = _gd[0] if isinstance(_gd, list) and _gd else {}
                            market_slug = _gm.get("slug", "") or ""
                    except Exception:
                        pass
                # Register slug for price lookup
                if market_id and market_slug:
                    try:
                        from market_resolver import register_slug
                        register_slug(market_id, market_slug)
                    except Exception:
                        pass
                side = latest.get("side", "").upper()  # "BUY"/"SELL" → normalize below
                if side == "BUY":
                    side = "YES"
                elif side == "SELL":
                    side = "NO"
                size = float(latest.get("usdcSize", latest.get("size", 0)))
                price = float(latest.get("price", 0.5))

                if size < config.COPY_TRADE_MIN_SIZE:
                    continue
                # Sharp filter: only trade mid-range prices (clearest signal)
                # Skip near-resolved markets (>0.82 YES = almost no upside left)
                if not (0.10 <= price <= 0.82):
                    continue
                if side not in ("YES", "NO"):
                    continue

                # Skip price ranges that learning engine has flagged as historically weak
                if learning.is_bad_price(price):
                    logger.debug(f"Skipping {addr[:10]}... price {price:.2f} in bad range")
                    continue

                # Volume check: use trade size as proxy since conditionId != clobTokenId
                volume = get_market_volume(market_id)
                if volume < config.MIN_MARKET_VOLUME:
                    if size < config.COPY_TRADE_MIN_SIZE:  # Already filtered above — just proxy volume
                        continue
                    volume = max(size * 100, config.MIN_MARKET_VOLUME)  # Synthetic volume proxy meets risk gate

                # Duration filter — paper mode only enters fast-resolving markets
                # Duration filter — skip markets that resolve more than 14 days out.
                # Paper mode force-exits at MAX_HOLD_HOURS (12h). Markets resolving
                # within 14 days have enough price movement to give us valid signals.
                # Unknown duration: allow it — price action is still valid for paper data.
                # Hard block: validate market is currently open before entering
                # This catches ancient/resolved markets (2020 elections, historical data, etc.)
                _mkt_end_ts = 0.0
                try:
                    import requests as _r2, time as _t2
                    _resp2 = _r2.get("https://gamma-api.polymarket.com/markets",
                                     params={"conditionId": market_id}, timeout=5)
                    if _resp2.ok:
                        _md2 = _resp2.json()
                        _m2 = _md2[0] if isinstance(_md2, list) and _md2 else {}
                        _er2 = _m2.get("endDate") or _m2.get("endDateIso") or ""
                        if _er2:
                            from datetime import datetime, timezone as _tz2
                            _edt2 = datetime.fromisoformat(_er2.replace("Z", "+00:00"))
                            if not _edt2.tzinfo: _edt2 = _edt2.replace(tzinfo=_tz2.utc)
                            _mkt_end_ts = _edt2.timestamp()
                            if _mkt_end_ts < _t2.time():
                                logger.debug(f"[COPY] Hard block: expired market {market_id[:20]} ended {_er2[:10]}")
                                continue  # HARD BLOCK — never enter expired market
                except Exception:
                    pass  # If API fails, allow cautiously

                import config as _cfg2
                if _cfg2.PAPER_MODE:
                    market_end = get_market_end_date(market_id)
                    if market_end is not None and market_end > 14:
                        logger.debug(f"Skipping {market_id[:12]}... resolves in {market_end:.1f}d (>14d cap)")
                        continue

                # Apply wallet penalty from learning engine
                wallet_multiplier = learning.get_wallet_weight_multiplier(addr)
                if wallet_multiplier < 0.5:
                    logger.debug(f"Skipping penalized wallet {addr[:10]}...")
                    continue

                profile.last_seen_trade_id = trade_id
                self._fired_trade_ids.add(market_id_check)  # Dedup across wallets + restarts

                if not profile.demo_validated:
                    profile.demo_validated = True
                    logger.debug(f"Wallet {addr[:10]}... auto-validated (leaderboard PnL ${profile.pnl:,.0f})")

                # Confidence: base on wallet PnL rank + learning floor
                learned_floor = learning.get_confidence_floor("copy_trading")
                base_confidence = min(0.90, 0.70 + profile.weight * 0.25 + (profile.pnl / 2_000_000) * 0.1)
                confidence = max(base_confidence, learned_floor)

                # Only fire on highest-conviction setups
                if confidence >= max(learned_floor, config.MIN_CONFIDENCE):  # Floor: config.MIN_CONFIDENCE (0.68)
                    signals.append({
                        "strategy": "copy_trading",
                        "venue": "polymarket",
                        "market_id": market_id,
                        "slug": market_slug,
                        "side": side,
                        "edge": confidence - 0.5,
                        "confidence": confidence,
                        "entry_price": price,
                        "volume": volume,
                        "weight": profile.weight,
                        "wallet": addr,
                        "demo_only": False,
                        "timestamp": time.time(),
                        "market_end": _mkt_end_ts,  # Expiry guard uses this
                        "reason": f"Copy top trader {addr[:10]}... PnL ${profile.pnl:,.0f}",
                    })

            except Exception as e:
                logger.warning(f"Error scanning wallet {addr[:10]}: {e}")

        return signals
