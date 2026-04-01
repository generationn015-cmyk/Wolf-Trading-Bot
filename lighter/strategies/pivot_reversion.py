"""
Pivot Point Mean Reversion
Applies to: WLD, BCH (configurable stop loss)
Timeframe: 5-minute candles
"""

def calc_pivot(highs, lows, closes, period=48):
    pivots = [0.0] * len(closes)
    for i in range(period, len(closes)):
        h = max(highs[i-period:i])
        l = min(lows[i-period:i])
        c = closes[i-1]
        pivots[i] = (h + l + c) / 3
    return pivots

def signal(i, closes, pivots):
    if pivots[i] <= 0: return 0
    if closes[i] < pivots[i]: return 1    # LONG
    if closes[i] > pivots[i]: return -1   # SHORT
    return 0

CONFIGS = {
    "WLDUSDT": {"stop_loss": 0.01, "hold_max": 60, "name": "WLD Pivot Reversion"},
    "BCHUSDT": {"stop_loss": 0.01, "hold_max": 60, "name": "BCH Pivot Reversion"},
}
