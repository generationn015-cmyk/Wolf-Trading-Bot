"""
Wolf → wolfofallstreets.xyz dashboard pusher.

Pushes Wolf state to individual REST endpoints every PUSH_INTERVAL seconds.
Non-blocking: failures are logged silently and never affect trading.

Endpoints used:
  POST /api/wolf/status       — {status, message}
  POST /api/wolf/performance  — {dailyPnl, weeklyPnl, monthlyPnl, totalTrades,
                                  winRate, winStreak, bestStreak, totalProfit}
  POST /api/wolf/trades       — one POST per trade (upsert by id)
  POST /api/wolf/market       — market data updates (BTC, ETH prices)
"""
import logging
import time
import requests
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger("wolf.feeds.dashboard")

BASE_URL       = "https://wolfofallstreets.xyz/api/wolf"
PUSH_INTERVAL  = 30   # seconds between full syncs
_last_push     = 0.0
_pushed_trade_ids: set = set()

def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "x-wolf-api-key": config.WOLF_DASHBOARD_API_KEY,
    }

def _post(endpoint: str, payload: dict, timeout: int = 6) -> bool:
    """POST to a dashboard endpoint. Returns True on success."""
    if not config.WOLF_DASHBOARD_API_KEY:
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/{endpoint}",
            json=payload,
            headers=_headers(),
            timeout=timeout,
        )
        if r.ok and r.json().get("success"):
            return True
        logger.debug(f"Dashboard {endpoint} rejected: {r.text[:120]}")
        return False
    except Exception as e:
        logger.debug(f"Dashboard push error ({endpoint}): {e}")
        return False


def push_status(open_positions: int, mode: str = "PAPER") -> bool:
    return _post("status", {
        "status": "online",
        "message": f"{mode} — {open_positions} open position{'s' if open_positions != 1 else ''}",
    })


def push_performance(stats: dict) -> bool:
    return _post("performance", {
        "dailyPnl":        round(float(stats.get("daily_pnl", 0) or 0), 2),
        "weeklyPnl":       round(float(stats.get("weekly_pnl", 0) or 0), 2),
        "monthlyPnl":      round(float(stats.get("total_pnl", 0) or 0), 2),
        "totalTrades":     int(stats.get("total_trades", 0) or 0),
        "winRate":         round(float(stats.get("win_rate", 0) or 0) * 100, 1),
        "winStreak":       int(stats.get("win_streak", 0) or 0),
        "bestStreak":      int(stats.get("best_streak", 0) or 0),
        "totalProfit":     round(float(stats.get("total_pnl", 0) or 0), 2),
    })


def push_trade(trade_id: str, symbol: str, side: str, strategy: str,
               entry_price: float, quantity: float, status: str,
               pnl: float = 0.0, pnl_percent: float = 0.0,
               entry_time: int = 0, exit_time: int = 0,
               exit_price: float = 0.0) -> bool:
    payload = {
        "id":          trade_id,
        "symbol":      symbol,
        "side":        side,
        "strategy":    strategy,
        "entryPrice":  round(entry_price, 4),
        "exitPrice":   round(exit_price, 4) if exit_price else 0,
        "quantity":    round(quantity, 2),
        "status":      status,
        "pnl":         round(pnl, 4),
        "pnlPercent":  round(pnl_percent, 2),
        "entryTime":   entry_time,
        "exitTime":    exit_time if exit_time else 0,
    }
    ok = _post("trades", payload)
    if ok:
        _pushed_trade_ids.add(trade_id)
    return ok


def push_market_data(btc_price: float = 0.0, eth_price: float = 0.0) -> bool:
    """Push BTC/ETH prices to market endpoint."""
    if not (btc_price or eth_price):
        return False
    try:
        for sym, price in [("BTC", btc_price), ("ETH", eth_price)]:
            if price > 0:
                _post("market", {
                    "symbol": sym,
                    "price":  round(price, 2),
                    "change": 0,
                    "changePercent": 0,
                })
        return True
    except Exception as e:
        logger.debug(f"Market data push error: {e}")
        return False


def push_to_dashboard(force: bool = False) -> bool:
    """
    Full dashboard sync. Called every N seconds from main loop.
    Pushes status, performance, all trades, and market prices.
    """
    global _last_push
    now = time.time()
    if not force and (now - _last_push) < PUSH_INTERVAL:
        return False
    if not config.WOLF_DASHBOARD_API_KEY:
        return False

    _last_push = now
    ok_count = 0

    try:
        conn = sqlite3.connect(config.DB_PATH)
        c = conn.cursor()

        # Stats (real trades only — simulated=0)
        row = c.execute(
            "SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl) "
            "FROM paper_trades WHERE resolved=1 AND simulated=0"
        ).fetchone()
        total, wins, pnl = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)
        win_rate = (wins / total * 100) if total else 0.0
        open_count = c.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND simulated=0"
        ).fetchone()[0]

        # Status
        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        if push_status(open_count, mode):
            ok_count += 1

        # Performance
        if push_performance({
            "total_trades": total,
            "win_rate": win_rate / 100,
            "total_pnl": pnl,
            "win_streak": 0,
            "best_streak": 0,
        }):
            ok_count += 1

        # All open positions
        open_trades = c.execute(
            "SELECT id, strategy, market_id, side, size, entry_price, timestamp "
            "FROM paper_trades WHERE resolved=0 AND simulated=0"
        ).fetchall()
        for row in open_trades:
            tid, strat, market, side, size, ep, ts = row
            trade_id = f"pt_{tid}"
            # Estimate current P&L (mark-to-market not available, show 0 until resolved)
            push_trade(
                trade_id=trade_id,
                symbol=market[:40],
                side=side,
                strategy=strat,
                entry_price=float(ep),
                quantity=float(size or 0),
                status="open",
                entry_time=int(float(ts) * 1000),
            )
            ok_count += 1

        # Resolved trades (last 20)
        closed_trades = c.execute(
            "SELECT id, strategy, market_id, side, size, entry_price, "
            "exit_price, pnl, timestamp, won "
            "FROM paper_trades WHERE resolved=1 AND simulated=0 "
            "ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        for row in closed_trades:
            tid, strat, market, side, size, ep, xp, pnl_t, ts, won = row
            ep, xp, pnl_t = float(ep), float(xp or 0), float(pnl_t or 0)
            size = float(size or 0)
            pnl_pct = (pnl_t / size * 100) if size else 0
            push_trade(
                trade_id=f"pt_{tid}",
                symbol=market[:40],
                side=side,
                strategy=strat,
                entry_price=ep,
                exit_price=xp,
                quantity=size,
                status="won" if won else "lost",
                pnl=pnl_t,
                pnl_percent=pnl_pct,
                entry_time=int(float(ts) * 1000),
            )
            ok_count += 1

        # Market prices
        try:
            from feeds.binance_feed import btc_feed, eth_feed
            push_market_data(btc_feed.get_price(), eth_feed.get_price())
        except Exception:
            pass

        conn.close()

    except Exception as e:
        logger.warning(f"Dashboard push failed: {e}")
        return False

    logger.debug(f"Dashboard sync complete: {ok_count} updates pushed")
    return ok_count > 0
