"""
Wolf Mission Control — Dashboard Backend
FastAPI + WebSocket — serves live trading data to the frontend.
Accessible on 0.0.0.0:5000 (VPS-accessible).
"""
import sys, os, time, json, asyncio, sqlite3, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import config

app = FastAPI(title="Wolf Mission Control")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket manager ─────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws) if ws in self.active else None

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# ── Data helpers ──────────────────────────────────────────────────────────────
def get_db():
    return sqlite3.connect(config.DB_PATH)

def fetch_stats():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Overall
    c.execute('''SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                 ROUND(SUM(pnl),2), ROUND(AVG(confidence),3)
                 FROM paper_trades WHERE resolved=1 AND simulated=0''')
    total, wins, pnl, avg_conf = c.fetchone()
    total = total or 0; wins = wins or 0; pnl = pnl or 0.0
    wr = round(wins / total * 100, 1) if total else 0

    # By strategy
    c.execute('''SELECT strategy, COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                 ROUND(SUM(pnl),2), ROUND(AVG(confidence),3)
                 FROM paper_trades WHERE resolved=1 AND simulated=0
                 GROUP BY strategy ORDER BY SUM(pnl) DESC''')
    strats = []
    for row in c.fetchall():
        name, t, w, p, cf = row
        strats.append({
            "name": name, "trades": t or 0,
            "wins": w or 0, "pnl": p or 0.0,
            "wr": round((w or 0) / t * 100, 1) if t else 0,
            "avg_conf": cf or 0,
        })

    # Open positions
    c.execute('''SELECT strategy, side, entry_price, size, timestamp, market_id, reason
                 FROM paper_trades WHERE resolved=0 AND simulated=0 ORDER BY timestamp DESC''')
    opens = []
    import re as _re
    for row in c.fetchall():
        strat, side, ep, sz, ts, mid, reason = row
        # Extract human-readable name from reason
        market_name = ""
        if reason:
            pipe = reason.find(" | ")
            if pipe >= 0:
                market_name = reason[pipe+3:].strip()[:55]
            elif "Copy top trader" in reason:
                w = _re.search(r"0x[a-f0-9]+", reason)
                market_name = f"Whale: {w.group()[:10]}…" if w else "Whale copy"
            else:
                market_name = reason[:50]
        opens.append({
            "strategy": strat, "side": side, "entry_price": ep,
            "size": sz, "age_min": round((time.time() - (ts or 0)) / 60, 1),
            "market_id": market_name or (mid or "")[:28],
        })

    # P&L curve (hourly buckets)
    c.execute('''SELECT CAST(timestamp/3600 AS INT)*3600 as bucket,
                 ROUND(SUM(pnl),2), COUNT(*)
                 FROM paper_trades WHERE resolved=1 AND simulated=0
                 GROUP BY bucket ORDER BY bucket''')
    curve_raw = c.fetchall()
    running = 1000.0
    curve = []
    for bucket, p2, cnt in curve_raw:
        running += (p2 or 0)
        curve.append({"ts": bucket, "balance": round(running, 2), "pnl": p2 or 0})

    # Recent trades
    c.execute('''SELECT strategy, side, entry_price, exit_price, pnl, won,
                 confidence, timestamp
                 FROM paper_trades WHERE resolved=1 AND simulated=0
                 ORDER BY timestamp DESC LIMIT 20''')
    recent = []
    for row in c.fetchall():
        strat, side, ep, xp, p2, won, cf, ts = row
        recent.append({
            "strategy": strat, "side": side, "entry": ep, "exit": xp,
            "pnl": p2, "won": won, "confidence": cf,
            "time": time.strftime("%H:%M", time.localtime(ts or 0)),
        })

    # Learning engine state
    state_path = os.path.join(os.path.dirname(config.DB_PATH), 'learning_state.json')
    floors = {}; bad_ranges = []
    if os.path.exists(state_path):
        try:
            s = json.loads(open(state_path).read())
            floors = s.get('floors', {})
            bad_ranges = s.get('bad_ranges', [])
        except Exception:
            pass

    # Best/worst
    c.execute('SELECT MAX(pnl), MIN(pnl) FROM paper_trades WHERE resolved=1 AND simulated=0')
    best, worst = c.fetchone()

    # Health
    c.execute('SELECT * FROM health_checks ORDER BY timestamp DESC LIMIT 1')
    health_row = c.fetchone()
    health = dict(zip([d[0] for d in c.description], health_row)) if health_row else {}

    conn.close()

    return {
        "balance": round(config.PAPER_STARTING_CAPITAL + pnl, 2),
        "pnl": pnl, "total": total, "wins": wins, "losses": total - wins,
        "wr": wr, "avg_conf": avg_conf or 0,
        "best_trade": best or 0, "worst_trade": worst or 0,
        "paper_mode": config.PAPER_MODE,
        "strategies": strats,
        "open_positions": opens,
        "pnl_curve": curve,
        "recent_trades": recent,
        "learning": {"floors": floors, "bad_ranges": bad_ranges},
        "health": health,
        "gate_passed": wr >= 72 and total >= 100,
        "timestamp": time.time(),
    }

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/api/stats")
def api_stats():
    return JSONResponse(fetch_stats())

@app.get("/api/watchlist")
def api_watchlist():
    """Return top 20 Polymarket wallets Wolf is watching."""
    from feeds.polymarket_feed import get_top_wallets
    wallets = get_top_wallets(limit=20)
    result = []
    for i, w in enumerate(wallets):
        result.append({
            "rank": i + 1,
            "username": w.get("userName") or w.get("wallet", "")[:12] + "…",
            "wallet": w.get("wallet", ""),
            "pnl": round(w.get("profit", 0), 2),
            "vol": round(w.get("vol", 0), 2),
        })
    return JSONResponse(result)

@app.get("/api/logs")
def api_logs():
    log_path = os.path.join(os.path.dirname(config.DB_PATH), 'wolf.log')
    lines = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            lines = f.readlines()[-100:]
    return JSONResponse({"lines": [l.rstrip() for l in lines]})

@app.post("/api/control/{action}")
def api_control(action: str):
    wolf_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if action == "restart":
        subprocess.Popen(
            ["bash", "-c", "pkill -f 'python3.*main.py'; sleep 2; bash watchdog.sh >> /tmp/watchdog.log 2>&1 &"],
            cwd=wolf_dir
        )
        return JSONResponse({"ok": True, "action": "restart"})
    elif action == "kill":
        subprocess.Popen(["pkill", "-f", "python3.*main.py"])
        return JSONResponse({"ok": True, "action": "kill"})
    return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)

# ── WebSocket live feed ───────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            data = fetch_stats()
            await ws.send_json(data)
            await asyncio.sleep(5)  # push update every 5s
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)

# ── Serve frontend ────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return HTMLResponse(open(html_path).read())

def run_dashboard():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="warning")

if __name__ == "__main__":
    run_dashboard()
