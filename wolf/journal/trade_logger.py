"""
Wolf Trading Bot — Trade Journal
SQLite storage for all trades, signals, health checks, paper trades.
Every decision logged. Nothing forgotten. Duplicates blocked at DB level.
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
                    reason TEXT,
                    UNIQUE(strategy, market_id, side, resolved)
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
        """
        Insert a paper trade. Uses INSERT OR IGNORE to silently drop duplicates
        within the same 5-minute window (strategy + market_id + side + resolved + bucket).
        Returns True if inserted, False if deduplicated.
        """
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO paper_trades
                    (timestamp, strategy, venue, market_id, side, size,
                     entry_price, exit_price, pnl, won, resolved, confidence, edge, reason,
                     market_end, days_to_expiry, slug, sub_strategy, tp_price, sl_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                trade.get("market_end", 0.0),
                trade.get("days_to_expiry", 0.0),
                trade.get("slug", ""),
                trade.get("sub_strategy"),
                trade.get("tp_price"),
                trade.get("sl_price"),
            ))
            conn.commit()
            inserted = cur.rowcount > 0
            if not inserted:
                logger.debug(
                    f"Dedup blocked: {trade.get('strategy')} "
                    f"{trade.get('market_id','')[:16]}… {trade.get('side')}"
                )
            return inserted

    def update_paper_trade_resolved(self, market_id: str, strategy: str,
                                     side: str, won: bool, exit_price: float,
                                     pnl: float, void: bool = False):
        """Update an open paper trade to resolved status.
        Retries up to 3x on lock/busy to prevent db_write_fail Guardian alerts."""
        import time as _time
        for _attempt in range(3):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10)
                # Use DELETE+INSERT pattern to avoid UNIQUE(strategy, market_id, side, resolved)
                # constraint violation when resolving a trade that was already force-closed
                cur = conn.execute(
                    "SELECT id, timestamp, venue, size, entry_price, confidence, edge, reason, "
                    "market_end, days_to_expiry, slug, sub_strategy, tp_price, sl_price, simulated "
                    "FROM paper_trades "
                    "WHERE market_id=? AND strategy=? AND side=? AND resolved=0 AND simulated=0 "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (market_id, strategy, side)
                )
                row = cur.fetchone()
                if row:
                    row_id = row[0]
                    conn.execute("DELETE FROM paper_trades WHERE id=?", (row_id,))
                    conn.execute("""
                        INSERT INTO paper_trades
                            (timestamp, strategy, venue, market_id, side, size, entry_price,
                             exit_price, pnl, won, resolved, confidence, edge, reason,
                             market_end, days_to_expiry, slug, sub_strategy, tp_price, sl_price, simulated, void)
                        VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)
                    """, (row[1], strategy, row[2], market_id, side, row[3], row[4],
                          exit_price, pnl, 1 if won else 0,
                          row[5], row[6], row[7], row[8], row[9], row[10], row[11], row[12], row[13], row[14],
                          1 if void else 0))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if _attempt < 2:
                    _time.sleep(0.5 * (_attempt + 1))
                    continue
                logger.error(f"DB update FAILED after 3 attempts: {e}")
                raise

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
                            ["timestamp","strategy","venue","market_id",
                             "side","confidence","edge"]}),
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
            paper = conn.execute("""
                SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl)
                FROM paper_trades WHERE resolved=1 AND simulated=0 AND void=0
            """).fetchone()
            total_p = paper[0] or 0
            wins_p  = paper[1] or 0
            pnl_p   = paper[2] or 0.0

            live = conn.execute("""
                SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(pnl)
                FROM trades WHERE status='closed'
            """).fetchone()
            total_l = live[0] or 0
            wins_l  = live[1] or 0
            pnl_l   = live[2] or 0.0

            # Per-strategy breakdown (paper)
            strat_rows = conn.execute("""
                SELECT strategy,
                       COUNT(*) as total,
                       SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as pnl,
                       AVG(confidence) as avg_conf,
                       AVG(entry_price) as avg_price
                FROM paper_trades WHERE resolved=1 AND simulated=0 AND void=0
                GROUP BY strategy
            """).fetchall()
            by_strategy = {}
            for row in strat_rows:
                s = row[0]
                by_strategy[s] = {
                    "total": row[1], "wins": row[2],
                    "win_rate": row[2]/row[1] if row[1] else 0,
                    "pnl": row[3] or 0.0,
                    "avg_confidence": row[4] or 0.0,
                    "avg_price": row[5] or 0.0,
                }

            return {
                "paper": {
                    "total": total_p, "wins": wins_p,
                    "win_rate": wins_p / total_p if total_p else 0,
                    "pnl": pnl_p,
                    "by_strategy": by_strategy,
                },
                "live": {
                    "total": total_l, "wins": wins_l,
                    "win_rate": wins_l / total_l if total_l else 0,
                    "pnl": pnl_l,
                },
            }

    def get_recent_trades(self, limit: int = 100, strategy: str = None) -> list[dict]:
        """Return recent resolved paper trades for analysis."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            q = "SELECT * FROM paper_trades WHERE resolved=1 AND simulated=0 AND void=0"
            params = []
            if strategy:
                q += " AND strategy=?"
                params.append(strategy)
            q += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]

    def export_csv(self, output_path: str = "/tmp/wolf_trades.csv"):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY timestamp DESC"
            ).fetchall()
            cols = [d[0] for d in conn.execute(
                "SELECT * FROM paper_trades LIMIT 0"
            ).description]
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(cols)
            writer.writerows(rows)
        logger.info(f"Exported {len(rows)} paper trades to {output_path}")
        return output_path
