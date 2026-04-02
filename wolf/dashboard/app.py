"""
Wolf Self-Hosted Dashboard
Runs on the VPS alongside Wolf. No cold starts. Reads directly from Wolf's DB.
Wolf of Wall Street style — dark theme, gold accents, real-time updates.
"""
import os
import sys
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from flask import Flask, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DB_PATH = config.DB_PATH

def _fmt_duration(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    if seconds < 3600: return f"{int(seconds/60)}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m" if m else f"{h}h"

def _fmt_expiry(dte):
    if dte is None: return "—"
    if dte < 0.04: return f"{int(dte*1440)}m"
    if dte < 1: return f"{dte*24:.1f}h"
    return f"{dte:.1f}d"

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wolf of All Streets | Mission Control</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #06060e; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif; }
  .container { max-width: 1400px; margin: 0 auto; padding: 16px; }
  
  .header { display: flex; justify-content: space-between; align-items: center; padding: 16px 0; border-bottom: 1px solid #1e293b; margin-bottom: 20px; }
  .header h1 { font-size: 24px; font-weight: 800; background: linear-gradient(135deg, #f59e0b, #ef4444); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .status { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #94a3b8; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  
  .ticker { display: flex; gap: 20px; padding: 10px 0; margin-bottom: 16px; font-size: 14px; }
  .ticker span { color: #f59e0b; font-weight: 700; }
  
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat { background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; padding: 14px; }
  .stat .label { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat .val { font-size: 24px; font-weight: 700; margin-top: 4px; }
  .green { color: #22c55e; } .red { color: #ef4444; } .gold { color: #f59e0b; }
  
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; background: #0f172a; padding: 4px; border-radius: 10px; }
  .tab { padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; color: #64748b; transition: all 0.2s; border: none; background: none; }
  .tab.active { background: #1e293b; color: #f59e0b; }
  .tab:hover { color: #e2e8f0; }
  
  .table-wrap { background: #0f172a; border: 1px solid #1e293b; border-radius: 10px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 600px; }
  th { background: #1e293b; padding: 10px 12px; text-align: left; font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
  td { padding: 10px 12px; border-bottom: 1px solid #1e293b; font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover { background: rgba(245, 158, 11, 0.04); }
  .YES { color: #22c55e; font-weight: 700; } .NO { color: #ef4444; font-weight: 700; }
  .market-cell { max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .strat { color: #94a3b8; font-size: 12px; }
  .expiring { color: #ef4444; }
  
  .refresh-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 2px; background: #1e293b; }
  .refresh-bar .prog { height: 100%; background: #f59e0b; transition: width 0.1s linear; }
  
  @media (max-width: 768px) {
    .stats { grid-template-columns: repeat(3, 1fr); }
    .stat .val { font-size: 18px; }
    th, td { padding: 6px 8px; font-size: 12px; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🐺 WOLF OF ALL STREETS</h1>
    <div class="status"><div class="status-dot" id="dot"></div><span id="stxt">Connecting...</span></div>
  </div>
  <div class="ticker" id="ticker"></div>
  <div class="stats" id="stats"></div>
  <div class="tabs">
    <button class="tab active" onclick="tab('open')">Open</button>
    <button class="tab" onclick="tab('resolved')">Resolved</button>
    <button class="tab" onclick="tab('activity')">Activity</button>
  </div>
  <div id="content"></div>
  <div class="refresh-bar"><div class="prog" id="prog"></div></div>
</div>
<script>
let cur='open', last=0;
async function load(){
  try{
    const r=await fetch('/api/state');const d=await r.json();render(d);last=Date.now();
  }catch(e){document.getElementById('stxt').textContent='Disconnected';document.getElementById('dot').style.background='#ef4444';}
}
function render(d){
  const p=d.performance||{},tr=d.trades||[],rs=d.resolved||[],mk=d.marketData||[];
  document.getElementById('stxt').textContent=d.status?.message||'Online';
  document.getElementById('dot').style.background='#22c55e';
  document.getElementById('ticker').innerHTML=mk.map(m=>`<div><span class="gold">${m.symbol}</span> $${Number(m.price).toLocaleString()}</div>`).join('');
  document.getElementById('stats').innerHTML=`
    <div class="stat"><div class="label">Balance</div><div class="val gold">$${(p.balance||0).toFixed(2)}</div></div>
    <div class="stat"><div class="label">Open</div><div class="val">${tr.length}</div></div>
    <div class="stat"><div class="label">Win Rate</div><div class="val ${p.winRate>=50?'green':'red'}">${(p.winRate||0).toFixed(1)}%</div></div>
    <div class="stat"><div class="label">Daily P&L</div><div class="val ${p.dailyPnl>=0?'green':'red'}">$${(p.dailyPnl||0).toFixed(2)}</div></div>
    <div class="stat"><div class="label">Total P&L</div><div class="val ${p.totalProfit>=0?'green':'red'}">$${(p.totalProfit||0).toFixed(2)}</div></div>
    <div class="stat"><div class="label">Mode</div><div class="val gold">${p.paperMode?'PAPER':'LIVE'}</div></div>`;
  if(cur==='open') renderOpen(tr);
  else if(cur==='resolved') renderResolved(rs);
  else renderActivity(d.activityLogs||[]);
}
function renderOpen(t){
  document.getElementById('content').innerHTML=`<div class="table-wrap"><table>
    <tr><th>Market</th><th>Side</th><th>Strategy</th><th>Entry</th><th>Size</th><th>Expiry</th><th>Open For</th><th>Conf</th></tr>
    ${t.map(r=>`<tr>
      <td class="market-cell" title="${r.reason||r.symbol}">${(r.reason||r.symbol||'').substring(0,50)}</td>
      <td class="${r.side}">${r.side}</td>
      <td class="strat">${r.strategy}</td>
      <td>$${(r.entryPrice||0).toFixed(3)}</td>
      <td>$${(r.quantity||0).toFixed(2)}</td>
      <td class="${(r.daysToExpiry||99)<0.5?'expiring':''}">${r.expiryText}</td>
      <td>${r.holdTime}</td>
      <td>${((r.confidence||0)*100).toFixed(0)}%</td>
    </tr>`).join('')||'<tr><td colspan="8" style="text-align:center;color:#64748b;padding:40px">No open positions</td></tr>'}
  </table></div>`;
}
function renderResolved(t){
  document.getElementById('content').innerHTML=`<div class="table-wrap"><table>
    <tr><th>Market</th><th>Side</th><th>Strategy</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Held</th><th>Result</th></tr>
    ${t.map(r=>`<tr>
      <td class="market-cell" title="${r.reason||r.symbol}">${(r.reason||r.symbol||'').substring(0,45)}</td>
      <td class="${r.side}">${r.side}</td>
      <td class="strat">${r.strategy}</td>
      <td>$${(r.entryPrice||0).toFixed(3)}</td>
      <td>$${(r.exitPrice||0).toFixed(3)}</td>
      <td class="${r.pnl>=0?'green':'red'}">$${(r.pnl||0).toFixed(2)}</td>
      <td>${r.holdTime}</td>
      <td>${r.won?'✅':'❌'}</td>
    </tr>`).join('')||'<tr><td colspan="8" style="text-align:center;color:#64748b;padding:40px">No resolved trades</td></tr>'}
  </table></div>`;
}
function renderActivity(logs){
  document.getElementById('content').innerHTML=`<div class="table-wrap" style="max-height:500px;overflow-y:auto">
    ${logs.map(l=>`<div style="padding:10px 16px;border-bottom:1px solid #1e293b;font-size:13px">
      <span style="color:#64748b;font-size:11px">${new Date(l.timestamp).toLocaleTimeString()}</span> ${l.message}</div>`).join('')||'<div style="padding:40px;text-align:center;color:#64748b">No activity yet</div>'}
  </div>`;
}
function tab(t){cur=t;document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));event.target.classList.add('active');load();}
setInterval(load,5000);
setInterval(()=>{document.getElementById('prog').style.width=Math.min(100,((Date.now()-last)/5000)*100)+'%';},100);
load();
</script>
</body>
</html>'''

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/state')
def api_state():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()
        now = time.time()

        # Open positions
        open_rows = c.execute("""
            SELECT id, strategy, market_id, side, size, entry_price, timestamp,
                   confidence, reason, days_to_expiry, market_end
            FROM paper_trades WHERE resolved=0 AND COALESCE(void,0)=0
            ORDER BY timestamp DESC LIMIT 100
        """).fetchall()

        trades = []
        for r in open_rows:
            hold_s = now - r[6]
            dte = r[9]
            trades.append({
                "id": f"pt_{r[0]}", "strategy": r[1], "symbol": (r[2] or "")[:50],
                "side": r[3], "quantity": round(r[4], 2), "entryPrice": round(r[5], 4),
                "confidence": round(r[7], 4) if r[7] else 0,
                "reason": r[8] or "",
                "daysToExpiry": dte,
                "expiryText": _fmt_expiry(dte),
                "holdTime": _fmt_duration(hold_s),
            })

        # Resolved trades
        resolved_rows = c.execute("""
            SELECT id, strategy, market_id, side, size, entry_price, exit_price,
                   pnl, won, timestamp, confidence, reason, days_to_expiry
            FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0
            ORDER BY timestamp DESC LIMIT 50
        """).fetchall()

        resolved = []
        for r in resolved_rows:
            resolved.append({
                "strategy": r[1], "symbol": (r[2] or "")[:50], "side": r[3],
                "entryPrice": round(r[5], 4), "exitPrice": round(r[6], 4),
                "pnl": round(r[7], 2), "won": bool(r[8]),
                "confidence": round(r[10], 4) if r[10] else 0,
                "reason": r[11] or "",
                "holdTime": "—",
            })

        # Performance
        total_resolved = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0").fetchone()[0]
        wins = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND won=1 AND COALESCE(void,0)=0").fetchone()[0]
        total_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0").fetchone()[0]
        wr = (wins / total_resolved * 100) if total_resolved > 0 else 0
        balance = config.PAPER_STARTING_CAPITAL + total_pnl
        day_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0 AND timestamp > ?", (now - 86400,)).fetchone()[0]

        conn.close()

        # Market data
        market_data = [{"symbol": "BTC", "price": 0}, {"symbol": "ETH", "price": 0}]
        try:
            import requests
            btc = requests.get("https://api.binance.us/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=3).json()
            eth = requests.get("https://api.binance.us/api/v3/ticker/price", params={"symbol": "ETHUSDT"}, timeout=3).json()
            market_data = [
                {"symbol": "BTC", "price": float(btc.get("price", 0))},
                {"symbol": "ETH", "price": float(eth.get("price", 0))},
            ]
        except:
            pass

        return jsonify({
            "status": {"message": f"PAPER · {len(trades)} open · {total_resolved} resolved · WR {wr:.0f}%"},
            "performance": {
                "dailyPnl": round(day_pnl, 2), "totalTrades": total_resolved,
                "winRate": round(wr, 1), "totalProfit": round(total_pnl, 2),
                "balance": round(balance, 2), "paperMode": config.PAPER_MODE,
            },
            "trades": trades,
            "resolved": resolved,
            "activityLogs": [],
            "marketData": market_data,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', 8080))
    print(f"🐺 Wolf Dashboard starting on http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)

def run_dashboard():
    port = int(os.getenv('DASHBOARD_PORT', 8080))
    print(f"🐺 Wolf Dashboard starting on http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
