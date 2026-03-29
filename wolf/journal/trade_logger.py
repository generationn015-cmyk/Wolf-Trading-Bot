"""
Wolf Trading Bot — Trade Journal
SQLite storage for all trades, signals, health checks, paper trades.
Every decision logged. Nothing forgotten.
"""
import sqlite3
import json
import csv
import time
import logging
import os
import config

logger = logging.getLogger("wolf.journal")

class TradeLogger:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    strategy TEXT,
                    venue TEXT,
                    market_id TEXT,
                    side TEXT,
                    size REAL,
                    entry_price REAL,
                    exit_price REAL,
                    pnl REAL,
                    status TEXT DEFAULT 'open',
                    order_id TEXT,
                    reason TEXT,
                    meta TEXT
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    strategy TEXT,
                    venue TEXT,
                    market_id TEXT,
                    side TEXT,
                    size REAL,
                    entry_price REAL,
                    exit_price REAL,
                    pnl REAL,
                    won INTEGER,
                    resolved INTEGER DEFAULT 0,
                    confidence REAL,
                    edge REAL,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    strategy TEXT,
                    venue TEXT,
                    market_id TEXT,
                    side TEXT,
                    confidence REAL,
                    edge REAL,
                    executed INTEGER DEFAULT 0,
                    block_reason TEXT,
                    meta TEXT
                );
                CREATE TABLE IF NOT EXISTS health_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    status TEXT,
                    binance_ok INTEGER,
                    polymarket_ok INTEGER,
                    kalshi_ok INTEGER,
                    daily_pnl REAL,
                    balance REAL,
                    open_positions INTEGER,
                    notes TEXT
                );
            """)
            conn.commit()
        logger.info(f"Journal initialized: {self.db_path}")

    def log_trade(self, trade: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO trades (timestamp, strategy, venue, market_id, side, size,
                    entry_price, exit_price, pnl, status, order_id, reason, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("timestamp", time.time()),
                trade.get("strategy"), trade.get("venue"),
                trade.get("market_id"), trade.get("side"),
                trade.get("size"), trade.get("entry_price"),
                trade.get("exit_price"), trade.get("pnl"),
                trade.get("status", "open"), trade.get("order_id"),
                trade.get("reason"), json.dumps(trade.get("meta", {}))
            ))
            conn.commit()

    def log_paper_trade(self, trade: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO paper_trades (timestamp, strategy, venue, market_id, side, size,
                    entry_price, exit_price, pnl, won, resolved, confidence, edge, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("timestamp", time.time()),
                trade.get("strategy"), trade.get("venue"),
                trade.get("market_id"), trade.get("side"),
                trade.get("size"), trade.get("entry_price"),
                trade.get("exit_price"), trade.get("pnl"),
                1 if trade.get("won") else 0,
                1 if trade.get("resolved") else 0,
                trade.get("confidence"), trade.get("edge"),
                trade.get("reason"),
            ))
            conn.commit()

    def log_signal(self, signal: dict, executed: bool = False, block_reason: str = ""):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO signals (timestamp, strategy, venue, market_id, side,
                    confidence, edge, executed, block_reason, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.get("timestamp", time.time()),
                signal.get("strategy"), signal.get("venue"),
                signal.get("market_id"), signal.get("side"),
                signal.get("confidence"), signal.get("edge"),
                1 if executed else 0, block_reason,
                json.dumps({k: v for k, v in signal.items() if k not in
                            ["timestamp","strategy","venue","market_id","side","confidence","edge"]}),
            ))
            conn.commit()

    def log_health(self, health: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO health_checks (timestamp, status, binance_ok, polymarket_ok,
                    kalshi_ok, daily_pnl, balance, open_positions, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                health.get("timestamp", time.time()),
                health.get("status", "ok"),
                1 if health.get("binance_ok") else 0,
                1 if health.get("polymarket_ok") else 0,
                1 if health.get("kalshi_ok") else 0,
                health.get("daily_pnl"), health.get("balance"),
                health.get("open_positions"), health.get("notes"),
            ))
            conn.commit()

    def get_stats(self) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            # Paper trade stats
            paper = conn.execute("""
                SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl)
                FROM paper_trades WHERE resolved=1
            """).fetchone()
            total_p = paper[0] or 0
            wins_p = paper[1] or 0
            pnl_p = paper[2] or 0.0

            # Live trade stats
            live = conn.execute("""
                SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(pnl)
                FROM trades WHERE status='closed'
            """).fetchone()
            total_l = live[0] or 0
            wins_l = live[1] or 0
            pnl_l = live[2] or 0.0

            return {
                "paper": {
                    "total": total_p,
                    "wins": wins_p,
                    "win_rate": wins_p / total_p if total_p else 0,
                    "pnl": pnl_p,
                },
                "live": {
                    "total": total_l,
                    "wins": wins_l,
                    "win_rate": wins_l / total_l if total_l else 0,
                    "pnl": pnl_l,
                },
            }

    def export_csv(self, output_path: str = "/tmp/wolf_trades.csv"):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM paper_trades ORDER BY timestamp DESC").fetchall()
            cols = [d[0] for d in conn.execute("SELECT * FROM paper_trades LIMIT 0").description]
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
        logger.info(f"Exported {len(rows)} paper trades to {output_path}")
        return output_path
