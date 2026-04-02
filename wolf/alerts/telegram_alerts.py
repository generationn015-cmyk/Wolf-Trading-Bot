"""
Wolf Trading Bot — Telegram Alerts (CLEANED UP)

Notification philosophy:
  - Trade entries: compact, one per trade
  - Trade exits: compact, one per trade
  - Kill switch / crashes: immediate
  - Daily summary: once at midnight ET
  - NO Guardian scan spam. NO system noise. NO re-duplicate alerts.
"""
import requests
import logging
import time
import os
import random as _random
from dotenv import load_dotenv

_wolf_env = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(_wolf_env, override=True)

import config

logger = logging.getLogger("wolf.alerts")

_BELFORT_QUOTES = [
    "I'm not fucking leaving! The show goes on!",
    "I've been a poor man, and I've been a rich man. And I choose rich every fucking time.",
    "The only thing stopping you from achieving your goals is the bullshit story you keep telling yourself.",
    "Stratton Oakmont IS America.",
]

def _belfort() -> str:
    return _random.choice(_BELFORT_QUOTES)

# ── Rate limiter ───────────────────────────────────────────────────────────────
_alert_sent: dict[str, float] = {}
_minute_count: list[float] = []
_DEDUP_WINDOW = 600   # 10 min max per identical alert
_MAX_PER_MIN  = 4      # hard ceiling (was 6)

def _rate_ok(key: str) -> bool:
    global _minute_count
    now = time.time()
    if now - _alert_sent.get(key, 0) < _DEDUP_WINDOW:
        return False
    _minute_count = [t for t in _minute_count if now - t < 60]
    if len(_minute_count) >= _MAX_PER_MIN:
        return False
    _alert_sent[key] = now
    _minute_count.append(now)
    return True

def _send(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        return resp.ok
    except Exception as e:
        logger.debug(f"Telegram send error: {e}")
        return False


def send_alert(message: str, level: str = "INFO", system: bool = False) -> bool:
    """
    CLEANED UP: 
      - Only fires CRITICAL + kill switch + offline alerts in paper mode
      - Guardian periodic "Scan #N" messages are BLOCKED here (handled separately)
      - Trade entry/exit alerts go through their own compact functions
    """
    if config.PAPER_MODE and level.upper() not in ("CRITICAL",):
        return True  # silent in paper mode except critical
    return _send(f"🐺 <b>Wolf</b>\n{message}")


def alert_trade_entry(strategy, market, side, size, entry_price, confidence, paper=False, days_to_expiry=None):
    """Minimal entry alert — strategy, side, expiry."""
    key = f"entry:{strategy}:{market[:35]}:{side}"
    if not _rate_ok(key):
        return
    strat = strategy.replace("_", " ").title()
    if days_to_expiry is not None:
        if days_to_expiry < 1:
            exp = f"{int(days_to_expiry * 24)}h"
        else:
            exp = f"{days_to_expiry:.1f}d"
    else:
        exp = "?"
    _send(f"🔵 {strat} {side} {exp}")

def alert_trade_exit(strategy, market, side, entry_price, exit_price, pnl, won, hold_time_min, paper=False):
    """Minimal exit — strategy, result, PnL."""
    icon = "✅" if won else "❌"
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    strat = strategy.replace("_", " ").title()
    _send(f"{icon} {strat} {pnl_str}")

def alert_kill_switch(drawdown_pct: float):
    _send(f"🚨 KILL SWITCH — Drawdown: {drawdown_pct:.1%}\nBot halted. Check account.")

def alert_daily_halt(daily_pnl_pct: float, daily_pnl_usd: float):
    _send(f"⛔ Daily loss limit: {daily_pnl_pct:.1%} (${daily_pnl_usd:+.2f})\nHalted until 00:00 ET.")

def alert_system_down(component: str, error: str):
    _send(f"🚨 System: {component}\n`{error[:150]}`")

def alert_paper_gate_passed(stats: dict):
    _send(f"🎯 Paper Gate PASSED\nTrades: {stats['total_trades']} · WR: {stats['win_rate']:.1%} · P&L: ${stats['total_pnl']:+.2f}\nReady for live.")

def alert_wr_threshold(wr: float, trades: int, pnl: float):
    _send(f"🟢 WR Gate: {wr:.1%} · {trades} trades · P&L: ${pnl:+.2f}")

def alert_whale_move(wallet: str, market: str, side: str, size: float, venue: str):
    if config.PAPER_MODE: return
    _send(f"🐋 Whale [{venue}] `{wallet[:12]}…` {side} ${size:,.0f}\n_{market[:50]}_")

def alert_daily_summary(stats: dict):
    """Once-per-day summary at midnight ET. NOT a Guardian scan."""
    _send(
        f"📊 <b>Daily Summary</b>\n"
        f"Trades: {stats.get('total',0)} | WR: {stats.get('wr',0):.1%}\n"
        f"P&L: ${stats.get('pnl',0):+.2f} | Balance: ${stats.get('balance',0):.2f}\n"
        f"Open: {stats.get('open_pos',0)}"
    )

def _fmt_duration(minutes: float) -> str:
    if minutes < 60: return f"{int(minutes)}m"
    h, m = int(minutes // 60), int(minutes % 60)
    return f"{h}h {m}m" if m else f"{h}h"

def alert_trade_executed(strategy, venue, market, side, size, paper):
    """Legacy compat."""
    alert_trade_entry(strategy, market, side, size, 0.5, 0.75, paper=paper)
