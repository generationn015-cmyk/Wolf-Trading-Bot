"""
Wolf Mission Control — Dashboard Backend
FastAPI + WebSocket — serves live trading data to the frontend.
Accessible on 0.0.0.0:5000 (VPS-accessible).
"""
import sys, os, time, json, asyncio, sqlite3, subprocess, secrets, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

# ── Persistent auth ───────────────────────────────────────────────────────────
# Password is set ONCE in .env as WOLF_DASHBOARD_PASSWORD.
# Never auto-generated. If blank, auth is skipped (local-only use).
# Sessions are tracked via a signed cookie — no re-login unless browser clears cookies.

_COOKIE_NAME = "wolf_session"
_SESSION_STORE: set[str] = set()  # In-memory session tokens (cleared on restart — forces re-login once per Wolf start)

def _check_password(raw: str) -> bool:
    """Compare submitted password against configured password (constant-time)."""
    expected = config.WOLF_DASHBOARD_PASSWORD
    if not expected:
        return True  # No password configured — open access
    return secrets.compare_digest(raw.strip(), expected.strip())

def _make_session_token() -> str:
    return secrets.token_hex(32)

def _auth_required(request: Request) -> None:
    """FastAPI dependency — raises 401 if password is set and session is not valid."""
    if not config.WOLF_DASHBOARD_PASSWORD:
        return  # No password set — allow all
    token = request.cookies.get(_COOKIE_NAME, "")
    if token not in _SESSION_STORE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

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

    # By strategy + sub_strategy (btc_scalper sub-modes shown individually)
    c.execute('''SELECT
                   CASE WHEN sub_strategy IS NOT NULL THEN strategy || '/' || sub_strategy
                        ELSE strategy END as display_name,
                   COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END),
                   ROUND(SUM(pnl),2), ROUND(AVG(confidence),3)
                 FROM paper_trades WHERE resolved=1 AND simulated=0
                 GROUP BY display_name ORDER BY SUM(pnl) DESC''')
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
    c.execute('''SELECT strategy, side, entry_price, size, timestamp, market_id, reason,
                        COALESCE(sub_strategy,''), COALESCE(tp_price,0), COALESCE(sl_price,0)
                 FROM paper_trades WHERE resolved=0 AND simulated=0 ORDER BY timestamp DESC''')
    opens = []
    import re as _re
    for row in c.fetchall():
        strat, side, ep, sz, ts, mid, reason, sub_strat, tp, sl = row
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
        display_strat = f"{strat}/{sub_strat}" if sub_strat else strat
        opens.append({
            "strategy": display_strat, "side": side, "entry_price": ep,
            "size": sz, "age_min": round((time.time() - (ts or 0)) / 60, 1),
            "market_id": market_name or (mid or "")[:28],
            "tp_price": tp or None, "sl_price": sl or None,
        })

    # P&L curve (hourly buckets)
    c.execute('''SELECT CAST(timestamp/3600 AS INT)*3600 as bucket,
                 ROUND(SUM(pnl),2), COUNT(*)
                 FROM paper_trades WHERE resolved=1 AND simulated=0
                 GROUP BY bucket ORDER BY bucket''')
    curve_raw = c.fetchall()
    running = config.PAPER_STARTING_CAPITAL
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

# ── Login / logout endpoints ──────────────────────────────────────────────────
@app.post("/api/login")
async def api_login(request: Request):
    """Authenticate with dashboard password. Sets a persistent session cookie."""
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)

    if not _check_password(password):
        return JSONResponse({"ok": False, "error": "incorrect password"}, status_code=401)

    token = _make_session_token()
    _SESSION_STORE.add(token)
    resp = JSONResponse({"ok": True})
    # max_age=30 days — session persists across browser sessions
    resp.set_cookie(_COOKIE_NAME, token, max_age=30*24*3600, httponly=True, samesite="lax")
    return resp

@app.post("/api/logout")
def api_logout(response: Response, request: Request):
    token = request.cookies.get(_COOKIE_NAME, "")
    _SESSION_STORE.discard(token)
    response.delete_cookie(_COOKIE_NAME)
    return JSONResponse({"ok": True})

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/api/stats")
def api_stats(request: Request):
    _auth_required(request)
    return JSONResponse(fetch_stats())

@app.get("/api/watchlist")
def api_watchlist(request: Request):
    _auth_required(request)
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
def api_logs(request: Request):
    _auth_required(request)
    log_path = os.path.join(os.path.dirname(config.DB_PATH), 'wolf.log')
    lines = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            lines = f.readlines()[-100:]
    return JSONResponse({"lines": [l.rstrip() for l in lines]})

@app.post("/api/control/{action}")
def api_control(action: str, request: Request):
    _auth_required(request)
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
    # Check auth via cookie before accepting connection
    if config.WOLF_DASHBOARD_PASSWORD:
        token = ws.cookies.get(_COOKIE_NAME, "")
        if token not in _SESSION_STORE:
            await ws.close(code=4401)
            return
    await manager.connect(ws)
    try:
        while True:
            data = fetch_stats()
            await ws.send_json(data)
            await asyncio.sleep(5)  # push every 5s
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)

# ── Serve frontend ────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# ── Login page HTML ───────────────────────────────────────────────────────────
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🐺 Wolf Mission Control</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #000; color: #fff; font-family: 'Segoe UI', Arial, sans-serif;
         display: flex; align-items: center; justify-content: center; min-height: 100vh;
         overflow: hidden; }

  /* ── Money rain canvas ── */
  #money-canvas { position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                  pointer-events: none; z-index: 0; }

  /* ── Card ── */
  .card { position: relative; z-index: 10;
          background: rgba(0,0,0,0.82); border: 1px solid #2a2a2a;
          border-radius: 16px; padding: 44px 40px 36px; width: 380px;
          text-align: center; backdrop-filter: blur(12px);
          box-shadow: 0 0 60px rgba(0,180,80,0.15), 0 0 120px rgba(0,0,0,0.6); }

  .banner { background: #f5c518; color: #000; font-size: 0.72em; font-weight: 900;
            letter-spacing: 0.18em; text-transform: uppercase; padding: 5px 14px;
            border-radius: 4px; display: inline-block; margin-bottom: 18px; }

  .title { font-size: 2.4em; font-weight: 900; letter-spacing: 0.04em;
           text-shadow: 0 0 30px rgba(245,197,24,0.5); margin-bottom: 2px; }
  .subtitle { font-size: 0.8em; color: #888; letter-spacing: 0.15em;
              text-transform: uppercase; margin-bottom: 28px; }

  .wolf-icon { font-size: 3.2em; margin-bottom: 8px; display: block;
               filter: drop-shadow(0 0 12px rgba(245,197,24,0.7)); }

  input[type=password] {
    width: 100%; padding: 13px 18px;
    background: rgba(255,255,255,0.05); border: 1px solid #333;
    border-radius: 8px; color: #fff; font-size: 1em; margin-bottom: 14px;
    outline: none; letter-spacing: 0.12em; transition: border-color 0.2s;
  }
  input[type=password]:focus { border-color: #f5c518; box-shadow: 0 0 0 2px rgba(245,197,24,0.2); }
  input[type=password]::placeholder { color: #555; letter-spacing: 0.05em; }

  button { width: 100%; padding: 13px;
           background: linear-gradient(135deg, #f5c518 0%, #d4a00a 100%);
           border: none; border-radius: 8px; color: #000;
           font-size: 1em; font-weight: 800; letter-spacing: 0.08em;
           text-transform: uppercase; cursor: pointer; transition: all 0.2s; }
  button:hover { transform: translateY(-1px); box-shadow: 0 4px 20px rgba(245,197,24,0.4); }
  button:active { transform: translateY(0); }

  .footer { margin-top: 20px; font-size: 0.7em; color: #444; letter-spacing: 0.08em; }
  .err { color: #ff4444; font-size: 0.82em; margin-top: 10px; min-height: 18px; }
</style>
</head>
<body>

<canvas id="money-canvas"></canvas>

<div class="card">
  <span class="wolf-icon">🐺</span>
  <div class="banner">Stratton Oakmont Inc.</div>
  <div class="title">THE WOLF</div>
  <div class="subtitle">Authorized Access Only</div>
  <input type="password" id="pw" placeholder="Enter access code" autofocus
         onkeydown="if(event.key==='Enter') login()">
  <button onclick="login()">ENTER THE FLOOR</button>
  <div class="err" id="err"></div>
  <div class="footer">Wolf System v2 &nbsp;·&nbsp; Mission Control</div>
</div>

<script>
// ── Money rain ────────────────────────────────────────────────────────────────
const canvas = document.getElementById('money-canvas');
const ctx = canvas.getContext('2d');

const SYMBOLS = ['$', '💵', '💰', '$', '$', '💵', '$', '100', '$'];
const GREEN = ['#00c853','#00e676','#69f0ae','#b9f6ca','#a5d6a7'];

let bills = [];

function resize() {
  canvas.width  = window.innerWidth;
  canvas.height = window.innerHeight;
}
resize();
window.addEventListener('resize', () => { resize(); spawnBills(); });

function spawnBills() {
  bills = [];
  const count = Math.floor(canvas.width / 28);
  for (let i = 0; i < count; i++) {
    bills.push(makeBill(Math.random() * canvas.height));
  }
}

function makeBill(startY) {
  return {
    x:       Math.random() * canvas.width,
    y:       startY !== undefined ? startY : -40,
    speed:   1.2 + Math.random() * 2.8,
    size:    13 + Math.random() * 18,
    symbol:  SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)],
    color:   GREEN[Math.floor(Math.random() * GREEN.length)],
    opacity: 0.18 + Math.random() * 0.55,
    sway:    (Math.random() - 0.5) * 0.8,
    rot:     (Math.random() - 0.5) * 0.3,
    angle:   Math.random() * Math.PI * 2,
  };
}

spawnBills();

function animateMoney() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  for (let b of bills) {
    ctx.save();
    ctx.globalAlpha = b.opacity;
    ctx.font = `bold ${b.size}px monospace`;
    ctx.fillStyle = b.color;
    ctx.translate(b.x, b.y);
    ctx.rotate(b.angle);
    ctx.fillText(b.symbol, 0, 0);
    ctx.restore();

    b.y     += b.speed;
    b.x     += b.sway;
    b.angle += b.rot * 0.02;

    if (b.y > canvas.height + 40) {
      Object.assign(b, makeBill());
      b.y = -40;
    }
  }
  requestAnimationFrame(animateMoney);
}
animateMoney();

// ── Login ─────────────────────────────────────────────────────────────────────
async function login() {
  const pw  = document.getElementById('pw').value;
  const err = document.getElementById('err');
  err.textContent = '';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw}),
      credentials: 'same-origin'
    });
    if (r.ok) {
      window.location.href = '/';
    } else {
      err.textContent = 'Access denied.';
      document.getElementById('pw').value = '';
      document.getElementById('pw').focus();
    }
  } catch(e) {
    err.textContent = 'Connection error.';
  }
}
</script>
</body>
</html>"""

@app.get("/login", response_class=HTMLResponse)
def login_page():
    """Login page — only shown when WOLF_DASHBOARD_PASSWORD is set."""
    return HTMLResponse(_LOGIN_HTML)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Main dashboard — redirects to /login if password is set and session is invalid."""
    if config.WOLF_DASHBOARD_PASSWORD:
        token = request.cookies.get(_COOKIE_NAME, "")
        if token not in _SESSION_STORE:
            return RedirectResponse(url="/login", status_code=302)
    html_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return HTMLResponse(open(html_path).read())

def run_dashboard():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="warning")

if __name__ == "__main__":
    run_dashboard()
