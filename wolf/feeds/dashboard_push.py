"""
Dashboard webhook pusher — sends Wolf state to wolfofallstreets.xyz every 30s
and on every trade entry/exit.

Site expects POST /api/wolf/webhook with header x-wolf-api-key: <key>
Payload: full state matching the site's /api/wolf/state schema.
"""
import os
import time
import logging
import sqlite3
import json
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("wolf.feeds.dashboard")

DASHBOARD_URL = "https://wolfofallstreets.xyz/api/wolf/webhook"
PUSH_INTERVAL = 30  # seconds between background pushes
_last_push = 0.0


def _get_api_key() -> str:
    import config
    return getattr(config, "WOLF_DASHBOARD_API_KEY", os.getenv("WOLF_DASHBOARD_API_KEY", ""))


def build_state_payload() -> dict:
    """Build the full dashboard state from Wolf's DB and runtime."""
    import config

    try:
        conn = sqlite3.connect(config.DB_PATH)
        c = conn.cursor()

        # ── Performance ──────────────────────────────────────────────────────
        now = time.time()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).timestamp()
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()

        total_row = c.execute(
            "SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl) "
            "FROM paper_trades WHERE resolved=1 AND simulated=0"
        ).fetchone()
        total_trades = total_row[0] or 0
        total_wins = total_row[1] or 0
        total_pnl = float(total_row[2] or 0)
        win_rate = round((total_wins / total_trades * 100) if total_trades > 0 else 0, 1)

        daily_pnl = float(c.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND simulated=0 AND timestamp>?",
            (today_start,)
        ).fetchone()[0])

        weekly_pnl = float(c.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND simulated=0 AND timestamp>?",
            (week_start,)
        ).fetchone()[0])

        # Win streak
        recent = c.execute(
            "SELECT won FROM paper_trades WHERE resolved=1 AND simulated=0 ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        streak = 0
        best_streak = 0
        cur_streak = 0
        for (won,) in recent:
            if won:
                cur_streak += 1
                streak = cur_streak
                best_streak = max(best_streak, cur_streak)
            else:
                cur_streak = 0

        # ── Open trades ────────────────────────────────────────────────────────
        open_rows = c.execute(
            "SELECT id, strategy, market_id, side, entry_price, size, timestamp, reason "
            "FROM paper_trades WHERE resolved=0 ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()

        open_trades = []
        for row in open_rows:
            tid, strat, mid, side, ep, size, ts, reason = row
            market_name = (reason or mid or "").split("|")[-1].strip()[:60]
            open_trades.append({
                "id": str(tid),
                "symbol": market_name,
                "side": side,
                "entryPrice": round(float(ep), 4),
                "exitPrice": None,
                "quantity": round(float(size or 0), 2),
                "status": "open",
                "pnl": None,
                "pnlPercent": None,
                "entryTime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "exitTime": None,
                "strategy": strat,
            })

        # ── Recent resolved ────────────────────────────────────────────────────
        resolved_rows = c.execute(
            "SELECT id, strategy, market_id, side, entry_price, size, pnl, won, timestamp, reason "
            "FROM paper_trades WHERE resolved=1 AND simulated=0 ORDER BY timestamp DESC LIMIT 30"
        ).fetchall()

        resolved_trades = []
        for row in resolved_rows:
            tid, strat, mid, side, ep, size, pnl, won, ts, reason = row
            market_name = (reason or mid or "").split("|")[-1].strip()[:60]
            ep_f = float(ep or 0)
            pnl_f = float(pnl or 0)
            exit_price = 1.0 if won else 0.0
            pnl_pct = round((pnl_f / (float(size or 1))) * 100, 1) if size else 0
            resolved_trades.append({
                "id": str(tid),
                "symbol": market_name,
                "side": side,
                "entryPrice": round(ep_f, 4),
                "exitPrice": exit_price,
                "quantity": round(float(size or 0), 2),
                "status": "won" if won else "lost",
                "pnl": round(pnl_f, 2),
                "pnlPercent": pnl_pct,
                "entryTime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "exitTime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "strategy": strat,
            })

        # ── PnL chart data (daily) ─────────────────────────────────────────────
        pnl_rows = c.execute(
            "SELECT date(datetime(timestamp,'unixepoch')), SUM(pnl), COUNT(*) "
            "FROM paper_trades WHERE resolved=1 AND simulated=0 "
            "GROUP BY date(datetime(timestamp,'unixepoch')) ORDER BY 1"
        ).fetchall()

        cumulative = 0.0
        pnl_data = []
        for date_str, day_pnl, day_trades in pnl_rows:
            day_pnl = float(day_pnl or 0)
            cumulative += day_pnl
            pnl_data.append({
                "date": date_str,
                "pnl": round(day_pnl, 2),
                "cumulative": round(cumulative, 2),
                "trades": day_trades,
            })

        # ── Strategy breakdown ─────────────────────────────────────────────────
        strat_rows = c.execute(
            "SELECT strategy, COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl) "
            "FROM paper_trades WHERE resolved=1 AND simulated=0 GROUP BY strategy"
        ).fetchall()

        strategy_breakdown = {}
        for strat, cnt, wins, spnl in strat_rows:
            strategy_breakdown[strat] = {
                "trades": cnt,
                "winRate": round((wins / cnt * 100) if cnt > 0 else 0, 1),
                "pnl": round(float(spnl or 0), 2),
            }

        conn.close()

        # ── D-Dub Index (synthetic signal strength) ────────────────────────────
        # Based on recent win rate momentum — 0-100 scale
        recent_10 = sum(1 for (won,) in recent[:10] if won)
        ddub_value = round(50 + (recent_10 / 10 - 0.5) * 40 if recent else 50, 1)
        ddub_signal = "BUY" if ddub_value >= 65 else "SELL" if ddub_value <= 45 else "HOLD"
        ddub_data = [{
            "time": int(time.time() - i * 300),
            "value": max(10, min(90, ddub_value + (i % 3 - 1) * 3)),
            "signal": ddub_signal,
        } for i in range(24, -1, -1)]

        # ── Activity log ──────────────────────────────────────────────────────
        activity_logs = []
        for i, row in enumerate(resolved_rows[:10]):
            tid, strat, mid, side, ep, size, pnl_v, won, ts, reason = row
            pnl_v = float(pnl_v or 0)
            market_name = (reason or "").split("|")[-1].strip()[:50]
            activity_logs.append({
                "id": str(tid),
                "type": "WIN" if won else "LOSS",
                "message": f"{strat.replace('_',' ').title()} {side} — {market_name}",
                "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "priority": "high" if abs(pnl_v) > 50 else "normal",
            })

        # ── Market data (BTC/ETH) ──────────────────────────────────────────────
        try:
            btc_r = requests.get("https://api.binance.us/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=3)
            eth_r = requests.get("https://api.binance.us/api/v3/ticker/24hr?symbol=ETHUSDT", timeout=3)
            btc_d = btc_r.json() if btc_r.ok else {}
            eth_d = eth_r.json() if eth_r.ok else {}
            market_data = [
                {"symbol": "BTC", "price": float(btc_d.get("lastPrice", 0)), "change": float(btc_d.get("priceChange", 0)), "changePercent": float(btc_d.get("priceChangePercent", 0))},
                {"symbol": "ETH", "price": float(eth_d.get("lastPrice", 0)), "change": float(eth_d.get("priceChange", 0)), "changePercent": float(eth_d.get("priceChangePercent", 0))},
            ]
        except Exception:
            market_data = []

        # ── Learning progress ──────────────────────────────────────────────────
        learning_progress = min(100, round(total_trades / 100 * 100, 0))
        learning = {"progress": int(learning_progress)}

        # ── Final payload ──────────────────────────────────────────────────────
        mode = "PAPER" if config.PAPER_MODE else "LIVE"
        return {
            "status": {
                "status": "online",
                "message": f"{mode} — {len(open_trades)} open / {total_trades} resolved",
                "currentPosition": len(open_trades),
                "ddubSignal": {"value": ddub_value, "direction": ddub_signal},
                "paperMode": config.PAPER_MODE,
            },
            "performance": {
                "winRate": win_rate,
                "totalTrades": total_trades,
                "totalProfit": round(total_pnl, 2),
                "dailyPnl": round(daily_pnl, 2),
                "weeklyPnl": round(weekly_pnl, 2),
                "winStreak": streak,
                "bestStreak": best_streak,
                "strategyBreakdown": strategy_breakdown,
            },
            "trades": open_trades + resolved_trades[:20],
            "pnlData": pnl_data,
            "ddubData": ddub_data,
            "activityLogs": activity_logs,
            "marketData": market_data,
            "learning": learning,
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.error(f"Dashboard payload build error: {e}")
        return {}


def push_to_dashboard(force: bool = False) -> bool:
    """Push current Wolf state to the dashboard. Returns True on success."""
    global _last_push
    api_key = _get_api_key()
    if not api_key:
        return False  # Not configured — silent skip

    now = time.time()
    if not force and (now - _last_push) < PUSH_INTERVAL:
        return False

    try:
        payload = build_state_payload()
        if not payload:
            return False
        resp = requests.post(
            DASHBOARD_URL,
            json={"data": payload},
            headers={"x-wolf-api-key": api_key, "Content-Type": "application/json"},
            timeout=8,
        )
        if resp.ok:
            _last_push = now
            logger.debug(f"Dashboard push OK ({resp.status_code})")
            return True
        else:
            logger.warning(f"Dashboard push failed: {resp.status_code} {resp.text[:100]}")
            return False
    except Exception as e:
        logger.debug(f"Dashboard push error: {e}")
        return False
