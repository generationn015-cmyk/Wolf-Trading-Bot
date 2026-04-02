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

# ── HTML Template (Wolf of Wall Street style) ─────────────────────────
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wolf of All Streets | Mission Control</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #06060e; color: #e2e8f0; font-family: 'Segoe UI', system-ui, sans-serif; }
  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
  
  /* Header */
  .header { display: flex; justify-content: space-between; align-items: center; padding: 20px 0; border-bottom: 1px solid #1e293b; margin-bottom: 24px; }
  .header h1 { font-size: 28px; font-weight: 800; background: linear-gradient(135deg, #f59e0b, #ef4444); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .header .status { display: flex; align-items: center; gap: 8px; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; animation: pulse 2s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  
  /* Stats Grid */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 20px; }
  .stat-card .label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .stat-card .value { font-size: 28px; font-weight: 700; }
  .stat-card .value.green { color: #22c55e; }
  .stat-card .value.red { color: #ef4444; }
  .stat-card .value.gold { color: #f59e0b; }
  
  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 24px; background: #0f172a; padding: 4px; border-radius: 12px; }
  .tab { padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; color: #64748b; transition: all 0.2s; }
  .tab.active { background: #1e293b; color: #f59e0b; }
  .tab:hover { color: #e2e8f0; }
  
  /* Tables */
  .table-container { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; overflow: hidden; }
  table { width: 100%; border-collapse: collapse; }
  th { background: #1e293b; padding: 12px 16px; text-align: left; font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; }
  td { padding: 12px 16px; border-bottom: 1px solid #1e293b; font-size: 14px; }
  tr:hover { background: rgba(245, 158, 11, 0.05); }
  .side-YES { color: #22c55e; font-weight: 700; }
  .side-NO { color: #ef4444; font-weight: 700; }
  .side-LONG { color: #22c55e; font-weight: 700; }
  .side-SHORT { color: #ef4444; font-weight: 700; }
  
  /* Activity Feed */
  .feed { max-height: 400px; overflow-y: auto; }
  .feed-item { padding: 12px 16px; border-bottom: 1px solid #1e293b; font-size: 13px; }
  .feed-item .time { color: #64748b; font-size: 11px; }
  .feed-item.TRADE { border-left: 3px solid #f59e0b; }
  .feed-item.ALERT { border-left: 3px solid #3b82f6; }
  
  /* Learning */
  .learning-card { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .learning-card h3 { color: #f59e0b; margin-bottom: 12px; }
  .lesson { padding: 8px 0; border-bottom: 1px solid #1e293b; font-size: 14px; }
  
  /* Market Data */
  .market-ticker { display: flex; gap: 24px; padding: 12px 0; }
  .market-item { display: flex; align-items: center; gap: 8px; }
  .market-item .symbol { font-weight: 700; color: #f59e0b; }
  .market-item .price { font-size: 18px; font-weight: 600; }
  
  /* Auto-refresh indicator */
  .refresh-bar { position: fixed; bottom: 0; left: 0; right: 0; height: 3px; background: #1e293b; }
  .refresh-bar .progress { height: 100%; background: #f59e0b; transition: width 0.1s linear; }
  
  @media (max-width: 768px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .stat-card .value { font-size: 20px; }
    table { font-size: 12px; }
    th, td { padding: 8px; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🐺 WOLF OF ALL STREETS</h1>
    <div class="status">
      <div class="status-dot" id="status-dot"></div>
      <span id="status-text">Connecting...</span>
    </div>
  </div>
  
  <div class="market-ticker" id="market-ticker"></div>
  
  <div class="stats" id="stats-grid"></div>
  
  <div class="tabs">
    <div class="tab active" onclick="showTab('trades')">Trades</div>
    <div class="tab" onclick="showTab('activity')">Activity</div>
    <div class="tab" onclick="showTab('learning')">Learning</div>
  </div>
  
  <div id="tab-content"></div>
  
  <div class="refresh-bar"><div class="progress" id="refresh-progress"></div></div>
</div>

<script>
let currentTab = 'trades';
let refreshInterval = 5000;
let lastRefresh = 0;

async function fetchData() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    updateDashboard(data);
    lastRefresh = Date.now();
  } catch(e) {
    document.getElementById('status-text').textContent = 'Disconnected';
    document.getElementById('status-dot').style.background = '#ef4444';
  }
}

function updateDashboard(data) {
  const p = data.performance || {};
  const trades = data.trades || [];
  const logs = data.activityLogs || [];
  const markets = data.marketData || [];
  const learning = data.learning || {};
  
  // Status
  document.getElementById('status-text').textContent = data.status?.message || 'Online';
  document.getElementById('status-dot').style.background = '#22c55e';
  
  // Market ticker
  document.getElementById('market-ticker').innerHTML = markets.map(m => `
    <div class="market-item">
      <span class="symbol">${m.symbol}</span>
      <span class="price">$${Number(m.price).toLocaleString()}</span>
    </div>
  `).join('');
  
  // Stats
  const openTrades = trades.filter(t => t.status === 'OPEN');
  document.getElementById('stats-grid').innerHTML = `
    <div class="stat-card">
      <div class="label">Balance</div>
      <div class="value gold">$${Number(p.balance || 0).toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Open Positions</div>
      <div class="value">${openTrades.length}</div>
    </div>
    <div class="stat-card">
      <div class="label">Win Rate</div>
      <div class="value ${p.winRate >= 50 ? 'green' : 'red'}">${Number(p.winRate || 0).toFixed(1)}%</div>
    </div>
    <div class="stat-card">
      <div class="label">Daily P&L</div>
      <div class="value ${p.dailyPnl >= 0 ? 'green' : 'red'}">$${Number(p.dailyPnl || 0).toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Total P&L</div>
      <div class="value ${p.totalProfit >= 0 ? 'green' : 'red'}">$${Number(p.totalProfit || 0).toFixed(2)}</div>
    </div>
    <div class="stat-card">
      <div class="label">Mode</div>
      <div class="value gold">${p.paperMode ? 'PAPER' : 'LIVE'}</div>
    </div>
  `;
  
  // Tab content
  if (currentTab === 'trades') renderTrades(openTrades);
  else if (currentTab === 'activity') renderActivity(logs);
  else if (currentTab === 'learning') renderLearning(learning);
}

function renderTrades(trades) {
  document.getElementById('tab-content').innerHTML = `
    <div class="table-container">
      <table>
        <tr><th>Market</th><th>Side</th><th>Strategy</th><th>Entry</th><th>Size</th><th>Confidence</th></tr>
        ${trades.slice(0, 50).map(t => `
          <tr>
            <td>${(t.symbol || '').substring(0, 40)}</td>
            <td class="side-${t.side}">${t.side}</td>
            <td>${t.strategy}</td>
            <td>$${Number(t.entryPrice || 0).toFixed(3)}</td>
            <td>$${Number(t.quantity || 0).toFixed(2)}</td>
            <td>${(Number(t.confidence || 0) * 100).toFixed(0)}%</td>
          </tr>
        `).join('')}
      </table>
    </div>
  `;
}

function renderActivity(logs) {
  document.getElementById('tab-content').innerHTML = `
    <div class="table-container feed">
      ${logs.slice(0, 50).map(l => `
        <div class="feed-item ${l.type || ''}">
          <div class="time">${new Date(l.timestamp).toLocaleTimeString()}</div>
          <div>${l.message}</div>
        </div>
      `).join('')}
    </div>
  `;
}

function renderLearning(learning) {
  const lessons = learning.lessonsLearned || [];
  document.getElementById('tab-content').innerHTML = `
    <div class="learning-card">
      <h3>📚 Learning Engine</h3>
      <p style="margin-bottom: 12px; color: #94a3b8;">${learning.currentModule || 'Initializing...'}</p>
      <p>Progress: ${learning.progress || 0} trades analyzed | Accuracy: ${learning.accuracy || 0}%</p>
    </div>
    <div class="learning-card">
      <h3>📝 Lessons Learned</h3>
      ${lessons.map(l => `<div class="lesson">${l}</div>`).join('') || '<div class="lesson">No lessons yet — waiting for trade resolutions</div>'}
    </div>
  `;
}

function showTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  fetchData();
}

// Auto-refresh
setInterval(fetchData, refreshInterval);
setInterval(() => {
  const pct = Math.min(100, ((Date.now() - lastRefresh) / refreshInterval) * 100);
  document.getElementById('refresh-progress').style.width = pct + '%';
}, 100);

fetchData();
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
            SELECT id, strategy, market_id, side, size, entry_price, timestamp, confidence
            FROM paper_trades WHERE resolved=0 AND COALESCE(void,0)=0
            ORDER BY timestamp DESC LIMIT 100
        """).fetchall()
        trades = [{
            "id": f"pt_{r[0]}", "strategy": r[1], "symbol": r[2][:50] if r[2] else "?",
            "side": r[3], "quantity": round(r[4], 2), "entryPrice": round(r[5], 4),
            "exitPrice": 0, "status": "OPEN", "pnl": 0, "pnlPercent": 0,
            "entryTime": int(r[6] * 1000) if r[6] else 0, "exitTime": 0,
            "confidence": round(r[7], 4) if r[7] else 0,
        } for r in open_rows]

        # Performance
        resolved = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0").fetchone()[0]
        wins = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND won=1 AND COALESCE(void,0)=0").fetchone()[0]
        total_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0").fetchone()[0]
        wr = (wins / resolved * 100) if resolved > 0 else 0
        balance = config.PAPER_STARTING_CAPITAL + total_pnl

        day_pnl = c.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND COALESCE(void,0)=0 AND timestamp > ?", (now - 86400,)).fetchone()[0]

        conn.close()

        # Market data
        market_data = [{"symbol": "BTC", "price": 0, "change": 0, "changePercent": 0},
                       {"symbol": "ETH", "price": 0, "change": 0, "changePercent": 0}]
        try:
            import requests
            btc = requests.get("https://api.binance.us/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=3).json()
            eth = requests.get("https://api.binance.us/api/v3/ticker/price", params={"symbol": "ETHUSDT"}, timeout=3).json()
            market_data = [
                {"symbol": "BTC", "price": float(btc.get("price", 0)), "change": 0, "changePercent": 0},
                {"symbol": "ETH", "price": float(eth.get("price", 0)), "change": 0, "changePercent": 0},
            ]
        except:
            pass

        return jsonify({
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
            "marketData": market_data,
            "learning": {"progress": resolved, "modulesCompleted": 0, "totalModules": 13,
                         "currentModule": f"{resolved} trades analyzed", "accuracy": round(wr, 1),
                         "lessonsLearned": []},
            "isStale": False,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', 8080))
    print(f"🐺 Wolf Dashboard starting on http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)

def run_dashboard():
    """Start the Flask dashboard server."""
    port = int(os.getenv('DASHBOARD_PORT', 8080))
    print(f"🐺 Wolf Dashboard starting on http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
