"""Write Wolf state to a static JSON file for the dashboard."""
import sys, os, sqlite3, json, time, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = config.DB_PATH
OUTPUT = "/hostinger/src/views/assets/wolf-data.json"

def extract_market_name(reason, market_id):
    """Extract human-readable market name from reason field."""
    if not reason:
        return (market_id or "?")[:40]
    # Pattern: "Strategy: detail | Market Name"
    pipe = reason.find(" | ")
    if pipe >= 0:
        name = reason[pipe+3:].strip()
        return name[:55] if name else (market_id or "?")[:40]
    return reason[:55]

def write_state():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    now = time.time()
    
    open_rows = conn.execute("""
        SELECT id, strategy, market_id, side, size, entry_price, timestamp, confidence, reason, slug, market_end
        FROM paper_trades WHERE resolved=0 AND COALESCE(void,0)=0
        ORDER BY timestamp DESC LIMIT 100
    """).fetchall()
    
    resolved = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0").fetchone()[0]
    wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND won=1 AND COALESCE(void,0)=0").fetchone()[0]
    total_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0").fetchone()[0]
    wr = (wins / resolved * 100) if resolved > 0 else 0
    balance = config.PAPER_STARTING_CAPITAL + total_pnl
    day_pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0 AND timestamp > ?", (now - 86400,)).fetchone()[0]
    
    # Learning data from ALL resolved trades (including voided ones for learning)
    all_resolved = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1").fetchone()[0]
    all_wins = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND won=1").fetchone()[0]
    all_wr = (all_wins / all_resolved * 100) if all_resolved > 0 else 0
    
    strats = conn.execute("""
        SELECT strategy, COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl)
        FROM paper_trades WHERE resolved=1 GROUP BY strategy
    """).fetchall()
    
    lessons = []
    for s, t, w, p in strats:
        if t > 0:
            lessons.append(f"{s.replace('_',' ').title()}: {w}/{t} wins, WR {round(w/t*100)}%, P&L ${float(p or 0):+.2f}")
    
    conn.close()
    
    try:
        btc = float(requests.get("https://api.binance.us/api/v3/ticker/price", params={"symbol":"BTCUSDT"}, timeout=3).json().get("price",0))
        eth = float(requests.get("https://api.binance.us/api/v3/ticker/price", params={"symbol":"ETHUSDT"}, timeout=3).json().get("price",0))
    except:
        btc, eth = 0, 0
    
    # Build clean trades list with readable names
    trades = []
    for r in open_rows:
        tid, strat, mid, side, size, ep, ts, conf, reason, slug, mend = r
        name = extract_market_name(reason, mid)
        # Calculate time to expiry
        expiry_text = ""
        if mend and mend > 0:
            hours = (mend - now) / 3600
            if hours < 0:
                expiry_text = f"EXPIRED ({abs(hours)/24:.0f}d ago)"
            elif hours < 24:
                expiry_text = f"{hours:.1f}h left"
            else:
                expiry_text = f"{hours/24:.1f}d left"
        
        trades.append({
            "id": f"pt_{tid}", "strategy": strat.replace("_", " ").title(),
            "symbol": name, "side": side,
            "quantity": round(size, 2), "entryPrice": round(ep, 4),
            "exitPrice": 0, "status": "OPEN", "pnl": 0, "pnlPercent": 0,
            "entryTime": int(ts * 1000) if ts else 0, "exitTime": 0,
            "confidence": round(conf, 4) if conf else 0,
            "expiry": expiry_text,
        })
    
    data = {
        "status": {"status": "hunting", "message": f"PAPER · {len(open_rows)} open · {resolved} resolved · WR {wr:.0f}%"},
        "performance": {
            "dailyPnl": round(day_pnl, 2), "weeklyPnl": round(total_pnl, 2),
            "monthlyPnl": round(total_pnl, 2), "totalTrades": resolved,
            "winRate": round(wr, 1), "winStreak": 0, "bestStreak": 0,
            "totalProfit": round(total_pnl, 2), "balance": round(balance, 2),
            "paperMode": config.PAPER_MODE,
        },
        "trades": trades,
        "activityLogs": [],
        "marketData": [
            {"symbol": "BTC", "price": round(btc, 2), "change": 0, "changePercent": 0},
            {"symbol": "ETH", "price": round(eth, 2), "change": 0, "changePercent": 0},
        ],
        "learning": {
            "progress": all_resolved, "modulesCompleted": len([s for s,t,w,p in strats if t >= 5 and w/t > 0.5]),
            "totalModules": 13, "currentModule": f"{all_resolved} trades analyzed across {len(strats)} strategies",
            "accuracy": round(all_wr, 1), "lessonsLearned": lessons,
        },
        "isStale": False,
    }
    
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(data, f)

if __name__ == "__main__":
    write_state()
    print(f"Dashboard data written to {OUTPUT}")
