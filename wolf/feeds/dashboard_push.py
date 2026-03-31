"""
Wolf → wolfofallstreets.xyz dashboard pusher.

Pushes ALL Wolf state to individual REST endpoints.
Covers: status, performance, trades (positions + ledger), market data,
        learning/evolution, activity log, D-Dub signals, P&L chart data.
"""
import logging
import time
import re
import sqlite3
import requests
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger("wolf.feeds.dashboard")

BASE_URL      = "https://wolfofallstreets.xyz/api/wolf"
PUSH_INTERVAL = 30   # seconds between full syncs
_last_push    = 0.0

def _headers():
    return {
        "Content-Type": "application/json",
        "x-wolf-api-key": config.WOLF_DASHBOARD_API_KEY,
    }

def _post(endpoint: str, payload: dict, timeout: int = 7) -> bool:
    if not config.WOLF_DASHBOARD_API_KEY:
        return False
    try:
        r = requests.post(f"{BASE_URL}/{endpoint}", json=payload,
                          headers=_headers(), timeout=timeout)
        ok = r.ok and r.json().get("success", False)
        if not ok:
            logger.debug(f"Dashboard /{endpoint}: {r.text[:80]}")
        return ok
    except Exception as e:
        logger.debug(f"Dashboard push error ({endpoint}): {e}")
        return False

def _post_webhook(event: str, data: dict) -> bool:
    """Post to webhook endpoint with a valid event type."""
    if not config.WOLF_DASHBOARD_API_KEY:
        return False
    try:
        payload = {"event": event, **data}
        r = requests.post(f"{BASE_URL}/webhook", json=payload,
                          headers=_headers(), timeout=6)
        return r.ok and r.json().get("success", False)
    except Exception:
        return False


def _extract_name(strategy: str, reason: str, market_id: str) -> str:
    """Extract a human-readable market name from the reason field."""
    if not reason:
        return market_id[:28] + "…"
    pipe = reason.find(" | ")
    if pipe >= 0:
        name = reason[pipe+3:].strip()
        return name[:52] if name else market_id[:28]
    if "Copy top trader" in reason:
        wallet = re.search(r"0x[a-f0-9]+", reason)
        w = wallet.group()[:10] if wallet else "?"
        return f"Whale copy: {w}…"
    if "MM " in reason or "market_making" in strategy:
        return f"Market making: {market_id[:16]}…"
    return reason[:50]

def push_to_dashboard(force: bool = False) -> bool:
    global _last_push
    now = time.time()
    if not force and (now - _last_push) < PUSH_INTERVAL:
        return False
    if not config.WOLF_DASHBOARD_API_KEY:
        return False
    _last_push = now

    try:
        conn = sqlite3.connect(config.DB_PATH)
        c = conn.cursor()

        # ── Core stats (real trades only) ─────────────────────────────────────
        stats = c.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                   SUM(pnl),
                   MAX(CASE WHEN won=1 THEN 1 ELSE 0 END),  -- streak helper
                   MIN(timestamp), MAX(timestamp)
            FROM paper_trades WHERE resolved=1 AND simulated=0
        """).fetchone()
        total_t = int(stats[0] or 0)
        wins    = int(stats[1] or 0)
        total_pnl = float(stats[2] or 0)
        win_rate  = round((wins / total_t * 100) if total_t else 0, 1)

        open_pos = c.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND simulated=0"
        ).fetchone()[0]
        mode = "PAPER" if config.PAPER_MODE else "LIVE"

        # Win streak calculation
        recent = c.execute("""
            SELECT won FROM paper_trades WHERE resolved=1 AND simulated=0
            ORDER BY timestamp DESC LIMIT 20
        """).fetchall()
        streak, best = 0, 0
        cur = 0
        for row in recent:
            if row[0]:
                cur += 1
                best = max(best, cur)
                if len(recent) - recent.index(row) <= streak + 1:
                    streak = cur
            else:
                cur = 0

        # Daily / weekly P&L
        day_start   = now - 86400
        week_start  = now - 604800
        daily_pnl   = float(c.execute("SELECT SUM(pnl) FROM paper_trades WHERE resolved=1 AND simulated=0 AND timestamp > ?", (day_start,)).fetchone()[0] or 0)
        weekly_pnl  = float(c.execute("SELECT SUM(pnl) FROM paper_trades WHERE resolved=1 AND simulated=0 AND timestamp > ?", (week_start,)).fetchone()[0] or 0)

        # ── 1. Status ─────────────────────────────────────────────────────────
        _post("status", {
            "status": "online",
            "message": f"{mode} · {open_pos} open · {total_t} resolved · WR {win_rate}%",
        })

        # ── 2. Performance ────────────────────────────────────────────────────
        _post("performance", {
            "dailyPnl":    round(daily_pnl, 2),
            "weeklyPnl":   round(weekly_pnl, 2),
            "monthlyPnl":  round(total_pnl, 2),
            "totalTrades": total_t,
            "winRate":     win_rate,
            "winStreak":   streak,
            "bestStreak":  best,
            "totalProfit": round(total_pnl, 2),
        })

        # ── 3. Open positions ─────────────────────────────────────────────────
        open_rows = c.execute("""
            SELECT id, strategy, market_id, side, size, entry_price, timestamp, reason, market_end, confidence
            FROM paper_trades WHERE resolved=0 AND simulated=0
            ORDER BY timestamp DESC
        """).fetchall()
        for row in open_rows:
            tid, strat, mid, side, size, ep, ts, reason, mend, conf = row
            symbol = _extract_name(strat, reason or "", mid)
            _post("trades", {
                "id":         f"pt_{tid}",
                "symbol":     symbol,
                "side":       side,
                "strategy":   strat.replace("_", " ").title(),
                "entryPrice": round(float(ep), 4),
                "exitPrice":  0,
                "quantity":   round(float(size or 0), 2),
                "status":     "open",
                "pnl":        0,
                "pnlPercent": 0,
                "entryTime":  int(float(ts) * 1000),
                "exitTime":   0,
                "marketEnd":  int(float(mend or 0) * 1000),
                "confidence": round(float(conf or 0), 4),
            })

        # ── 4. Ledger — resolved trades (last 50) ─────────────────────────────
        closed_rows = c.execute("""
            SELECT id, strategy, market_id, side, size, entry_price,
                   exit_price, pnl, timestamp, won, reason
            FROM paper_trades WHERE resolved=1 AND simulated=0
            ORDER BY timestamp DESC LIMIT 50
        """).fetchall()
        for row in closed_rows:
            tid, strat, mid, side, size, ep, xp, pnl_v, ts, won, reason = row
            ep, xp = float(ep), float(xp or 0)
            pnl_v  = float(pnl_v or 0)
            size   = float(size or 0)
            name   = _extract_name(strat, reason or "", mid)
            pnl_pct = round((pnl_v / size * 100) if size else 0, 2)
            _post("trades", {
                "id":         f"pt_{tid}",
                "symbol":     name,
                "side":       side,
                "strategy":   strat.replace("_", " ").title(),
                "entryPrice": round(ep, 4),
                "exitPrice":  round(xp, 4),
                "quantity":   round(size, 2),
                "status":     "won" if won else "lost",
                "pnl":        round(pnl_v, 4),
                "pnlPercent": pnl_pct,
                "entryTime":  int(float(ts) * 1000),
                "exitTime":   int(float(ts) * 1000) + 3600000,
            })

        # ── 5. P&L chart data ─────────────────────────────────────────────────
        pnl_history = c.execute("""
            SELECT date(timestamp, 'unixepoch') as dt,
                   SUM(pnl) as daily,
                   COUNT(*) as trades
            FROM paper_trades WHERE resolved=1 AND simulated=0
            GROUP BY dt ORDER BY dt ASC
        """).fetchall()
        cumulative = 0
        for dt, daily, n_trades in pnl_history:
            cumulative += float(daily or 0)
            _post("pnldata", {
                "date":       dt,
                "pnl":        round(float(daily or 0), 2),
                "cumulative": round(cumulative, 2),
                "trades":     int(n_trades),
            })

        # ── 6. D-Dub signal (market sentiment indicator) ──────────────────────
        # D-Dub = "Directional Dub" — composite signal from strategy confidence
        # Range 0-100: <30 bearish, 30-70 neutral, >70 bullish
        if open_rows:
            prices = [float(r[5]) for r in open_rows]  # entry prices
            yes_sides = sum(1 for r in open_rows if r[3] == "YES")
            no_sides  = len(open_rows) - yes_sides
            # Score: high YES bias + low avg price = strong underdog long thesis
            yes_bias  = yes_sides / len(open_rows) if open_rows else 0.5
            avg_price = sum(prices) / len(prices) if prices else 0.5
            ddub_val  = round(40 + (yes_bias * 40) - (avg_price * 20), 1)
            ddub_val  = max(0, min(100, ddub_val))
            signal    = "bullish" if ddub_val > 60 else ("bearish" if ddub_val < 40 else "neutral")
        else:
            ddub_val, signal = 50.0, "neutral"

        _post("ddub", {
            "signal": signal,
            "value":  ddub_val,
            "time":   datetime.now(timezone.utc).isoformat(),
        })

        # ── 7. Market data (BTC/ETH) — direct REST to avoid singleton issue ────
        try:
            import requests as _req
            btc_r = _req.get("https://api.binance.us/api/v3/ticker/price",
                             params={"symbol": "BTCUSDT"}, timeout=4)
            eth_r = _req.get("https://api.binance.us/api/v3/ticker/price",
                             params={"symbol": "ETHUSDT"}, timeout=4)
            btc = float(btc_r.json().get("price", 0))
            eth = float(eth_r.json().get("price", 0))
            if btc > 0:
                _post("market", {"symbol": "BTC", "price": round(btc, 2),
                                 "change": 0, "changePercent": 0})
            if eth > 0:
                _post("market", {"symbol": "ETH", "price": round(eth, 2),
                                 "change": 0, "changePercent": 0})
        except Exception:
            pass

        # ── 8. Activity log via webhook (evolution/learning updates) ────────────
        # Post strategy performance summary as alert events
        try:
            lesson_lines = []
            if total_t > 0:
                for s, t, w, p, ap in strat_rows:
                    if t > 0:
                        wr = round(w/t*100)
                        lesson_lines.append(f"{s.replace('_',' ').title()}: {w}/{t} WR {wr}% | P&L ${float(p or 0):+.2f}")
            else:
                lesson_lines = [
                    f"Paper test running — {open_pos} active positions",
                    "Strategies: Value Bet · Copy Trading · Market Making",
                    "Copy trading: tracking top 20 Polymarket whale wallets",
                    "Value bet: underdog markets ≤30 days to expiry",
                    "Market making: paired YES/NO spread capture",
                    "Self-correction active — blocked low-WR price ranges",
                    "First trade resolutions expected within 21h of entry",
                ]
            for lesson in lesson_lines[:3]:  # Rate limit: 3 per push cycle
                _post_webhook("alert", {"message": lesson, "priority": "low"})
        except Exception:
            pass

        # ── 9. Learning / Evolution tab ───────────────────────────────────────
        # Pull lessons from learning engine and strategy analytics
        lessons_raw = []
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from learning_engine import LearningEngine
            le = LearningEngine()
            status = le.get_status()
            progress = le.get_progress()
            lessons_raw = le.get_lessons() if hasattr(le, 'get_lessons') else []
        except Exception:
            status = "Learning engine initializing"
            progress = 0
            lessons_raw = []

        # Per-strategy performance for evolution tab
        strat_rows = c.execute("""
            SELECT strategy,
                   COUNT(*) as trades,
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                   SUM(pnl) as pnl,
                   AVG(entry_price) as avg_price
            FROM paper_trades WHERE resolved=1 AND simulated=0
            GROUP BY strategy
        """).fetchall()

        lessons = [f"{s}: {w}/{t} WR {round(w/t*100)}% P&L ${float(p or 0):+.2f}"
                   for s, t, w, p, _ in strat_rows if t > 0]
        if not lessons:
            lessons = ["No resolved trades yet — paper test running",
                       "Positions open: waiting for market resolution",
                       "First force-exits expected ~21h from first entry",
                       "Monitoring: copy_trading, value_bet, market_making",
                       "D-Dub Index: tracking composite market sentiment"]

        # The site uses /api/wolf/state GET which aggregates — push learning via webhook
        # Push activity log entry for learning status
        _post_webhook("alert", {
            "message": f"Evolution: {len(lessons)} lessons · {total_t} trades analyzed · WR {win_rate}%",
            "priority": "low",
        })

        conn.close()
        logger.debug(f"Dashboard full sync: {open_pos} open, {total_t} resolved, WR {win_rate}%")
        return True

    except Exception as e:
        logger.warning(f"Dashboard push failed: {e}")
        return False
