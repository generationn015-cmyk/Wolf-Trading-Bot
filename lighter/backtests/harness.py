"""
Live Data Backtest Harness
Fetches real 5m candles from Binance, runs strategy signals, tracks P&L.
"""
import requests
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def fetch_candles(symbol="BTCUSDT", interval="5m", limit=500):
    """Fetch OHLCV candles from Binance."""
    r = requests.get(
        "https://api.binance.us/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10
    )
    data = r.json()
    opens = [float(c[1]) for c in data]
    highs = [float(c[2]) for c in data]
    lows = [float(c[3]) for c in data]
    closes = [float(c[4]) for c in data]
    volumes = [float(c[5]) for c in data]
    timestamps = [int(c[0]) for c in data]
    return opens, highs, lows, closes, volumes, timestamps

def run_backtest(symbol, strategy_mod, config, candles=None):
    """Run a strategy backtest on live data."""
    if candles is None:
        opens, highs, lows, closes, volumes, timestamps = fetch_candles(symbol)
    else:
        opens, highs, lows, closes, volumes, timestamps = candles

    stop_loss = config["stop_loss"]
    hold_max = config["hold_max"]
    name = config["name"]

    # Generate signals
    if "count_streak" in dir(strategy_mod):
        # Candle reversal strategy
        streak = strategy_mod.count_streak(opens, closes)
        signals = [strategy_mod.signal(i, streak) for i in range(len(closes))]
    elif "calc_pivot" in dir(strategy_mod):
        # Pivot reversion strategy
        pivots = strategy_mod.calc_pivot(highs, lows, closes)
        signals = [strategy_mod.signal(i, closes, pivots) for i in range(len(closes))]
    else:
        # Keltner / indicator-based
        signals = [strategy_mod.signal(i, closes, highs, lows) for i in range(len(closes))]

    # Simulate trades
    balance = 1000.0
    trades = []
    position = None

    for i in range(len(closes)):
        sig = signals[i]

        # Check existing position
        if position is not None:
            entry = position["entry"]
            side = position["side"]
            bars_held = i - position["entry_i"]

            if side == "LONG":
                pnl_pct = (closes[i] - entry) / entry
            else:
                pnl_pct = (entry - closes[i]) / entry

            # Exit conditions
            exit_reason = None
            if pnl_pct <= -stop_loss:
                exit_reason = "STOP_LOSS"
            elif bars_held >= hold_max:
                exit_reason = "TIME_EXIT"
            elif (side == "LONG" and sig == -1) or (side == "SHORT" and sig == 1):
                exit_reason = "SIGNAL_REVERSE"

            if exit_reason:
                pnl_usd = balance * 0.02 * pnl_pct  # 2% risk per trade
                balance += pnl_usd
                trades.append({
                    "side": side,
                    "entry": entry,
                    "exit": closes[i],
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_usd": round(pnl_usd, 2),
                    "bars": bars_held,
                    "reason": exit_reason,
                })
                position = None

        # Enter new position
        if position is None and sig != 0:
            position = {
                "side": "LONG" if sig == 1 else "SHORT",
                "entry": closes[i],
                "entry_i": i,
            }

    # Summary
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = sum(t["pnl_usd"] for t in trades)
    wr = (len(wins) / len(trades) * 100) if trades else 0
    avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0

    return {
        "symbol": symbol,
        "name": name,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(abs(sum(t["pnl_usd"] for t in wins) / sum(t["pnl_usd"] for t in losses)), 2) if losses and sum(t["pnl_usd"] for t in losses) != 0 else float('inf'),
        "trades": trades[-5:],  # last 5 trades
    }

if __name__ == "__main__":
    print("Backtest harness ready")
