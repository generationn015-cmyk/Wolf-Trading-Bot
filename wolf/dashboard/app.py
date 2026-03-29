"""
Wolf Trading Bot — Local Dashboard
Runs on 127.0.0.1:5000. Shows live stats, positions, health.
Paper mode banner always visible when PAPER_MODE=True.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, jsonify
import config
from journal.trade_logger import TradeLogger
import sqlite3
import time

app = Flask(__name__)
journal = TradeLogger()

@app.route("/")
def index():
    return render_template("index.html", paper_mode=config.PAPER_MODE)

@app.route("/api/stats")
def api_stats():
    stats = journal.get_stats()
    return jsonify({
        "paper_mode": config.PAPER_MODE,
        "paper": stats["paper"],
        "live": stats["live"],
        "timestamp": time.time(),
    })

@app.route("/api/recent_trades")
def api_recent_trades():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/health")
def api_health():
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM health_checks ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return jsonify(dict(row) if row else {"status": "no data"})

@app.route("/api/whale_moves")
def api_whale_moves():
    try:
        with sqlite3.connect(config.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM whale_moves ORDER BY timestamp DESC LIMIT 20"
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

def run_dashboard():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    run_dashboard()
