"""
5-Red Candle Reversal Strategy
Applies to: XRP, LDO (configurable stop loss)
Timeframe: 5-minute candles
"""

def count_streak(opens, closes):
    streak = [0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] < opens[i]:  # red
            streak[i] = streak[i-1] - 1 if streak[i-1] < 0 else -1
        elif closes[i] > opens[i]:  # green
            streak[i] = streak[i-1] + 1 if streak[i-1] > 0 else 1
    return streak

def signal(i, streak):
    if streak[i] <= -5: return 1   # LONG after 5 reds
    if streak[i] >= 5: return -1   # SHORT after 5 greens
    return 0

# Strategy configs per asset
CONFIGS = {
    "XRPUSDT": {"stop_loss": 0.01, "hold_max": 120, "name": "XRP 5-Red Reversal"},
    "LDOUSDT": {"stop_loss": 0.015, "hold_max": 120, "name": "LDO 5-Red Reversal"},
}
