"""
Keltner Channel Flipped (Mean Reversion)
Timeframe: 5-minute candles
"""

def ema(values, period):
    result = [values[0]] * len(values)
    k = 2 / (period + 1)
    for i in range(1, len(values)):
        result[i] = values[i] * k + result[i-1] * (1 - k)
    return result

def atr(highs, lows, closes, period=10):
    tr = [highs[0] - lows[0]] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    return ema(tr, period)

def signal(i, closes, highs, lows):
    mid = ema(closes, 20)
    atr_vals = atr(highs, lows, closes)
    upper = mid[i] + 1.5 * atr_vals[i]
    lower = mid[i] - 1.5 * atr_vals[i]
    if closes[i] > upper: return -1   # SHORT: fade breakout
    if closes[i] < lower: return 1    # LONG: fade breakdown
    return 0

CONFIGS = {
    "DEFAULT": {"stop_loss": 0.015, "hold_max": 120, "name": "Keltner Reversion"},
}
