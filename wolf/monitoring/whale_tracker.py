"""
Wolf Trading Bot — Whale Tracker
Polls top Polymarket wallets every 60s.
Detects large trades > $500 and fires Telegram alerts.
"""
import asyncio
import time
import logging
import sqlite3
import config
from feeds.polymarket_feed import get_top_wallets, get_wallet_positions
from alerts.telegram_alerts import alert_whale_move

logger = logging.getLogger("wolf.whale")

class WhaleTracker:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._seen_trade_ids: set = set()
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Whale tracker started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        while self._running:
            try:
                await self._scan_wallets()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Whale tracker error: {e}")
                await asyncio.sleep(30)

    async def _scan_wallets(self):
        wallets = get_top_wallets(limit=20)
        for wallet in wallets:
            addr = wallet.get("proxy_wallet") or wallet.get("wallet", "")
            if not addr:
                continue
            try:
                positions = get_wallet_positions(addr, limit=5)
                for pos in positions:
                    trade_id = pos.get("id", "")
                    if trade_id in self._seen_trade_ids:
                        continue
                    self._seen_trade_ids.add(trade_id)

                    size = float(pos.get("size", 0))
                    if size >= config.WHALE_ALERT_THRESHOLD:
                        market = pos.get("market", "unknown")
                        side = pos.get("side", "unknown")
                        alert_whale_move(addr, market, side, size, "polymarket")
                        self._store_whale_move(addr, market, side, size, pos.get("price", 0))
            except Exception as e:
                logger.warning(f"Error checking wallet {addr[:10]}: {e}")

        # Keep seen set bounded
        if len(self._seen_trade_ids) > 10000:
            self._seen_trade_ids = set(list(self._seen_trade_ids)[-5000:])

    def _store_whale_move(self, wallet, market, side, size, price):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS whale_moves (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL, wallet TEXT, market TEXT,
                        side TEXT, size REAL, price REAL, venue TEXT
                    )
                """)
                conn.execute(
                    "INSERT INTO whale_moves VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), wallet, market, side, size, price, "polymarket")
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to store whale move: {e}")
