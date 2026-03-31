"""
Wolf Trading Bot — Telegram Alerts
Direct HTTP to Telegram bot API — zero LLM cost, no OpenClaw routing.
Alert philosophy:
  - Paper mode: CRITICAL only (kill switch, system down)
  - Live mode: every trade entry + exit, concise format
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

# ── Jordan Belfort / Wolf of Wall Street quotes (WIN only) ───────────────────
_BELFORT_QUOTES = [
    "I'm not fucking leaving! The show goes on!",
    "I've been a poor man, and I've been a rich man. And I choose rich every fucking time.",
    "Let me tell you something. There's no nobility in poverty.",
    "On a daily basis I consume enough drugs to sedate Manhattan, Long Island, and Queens for a month.",
    "I want you to back yourself into a corner. Give yourself no choice but to succeed.",
    "Act as if! Act as if you're the most successful person in this room.",
    "The easiest way to make money? Other people's money.",
    "Nobody knows if a stock is gonna go up, down, sideways or in fucking circles.",
    "The only thing stopping you from achieving your goals is the bullshit story you keep telling yourself.",
    "Stratton Oakmont IS America.",
]

def _belfort() -> str:
    return _random.choice(_BELFORT_QUOTES)

# ── Alert rate limiter ────────────────────────────────────────────────────────
# Prevents spam when multiple strategies fire on the same market.
# Max 1 entry alert per (strategy+market_id) per 30 minutes.
# Max 6 trade alerts per minute total (hard ceiling).
_alert_sent: dict[str, float] = {}   # key → last sent timestamp
_minute_count: list[float] = []      # timestamps of alerts in last 60s
_DEDUP_WINDOW = 1800   # 30 min — same trade won't double-alert
_MAX_PER_MIN  = 6      # hard ceiling on alerts/minute

def _rate_ok(key: str) -> bool:
    """Return True if this alert should be sent, False if throttled."""
    global _minute_count
    now = time.time()
    # Per-key dedup
    if now - _alert_sent.get(key, 0) < _DEDUP_WINDOW:
        return False
    # Per-minute ceiling
    _minute_count = [t for t in _minute_count if now - t < 60]
    if len(_minute_count) >= _MAX_PER_MIN:
        logger.debug(f"Alert rate limit hit — suppressing {key[:30]}")
        return False
    _alert_sent[key] = now
    _minute_count.append(now)
    return True

# ── Internal send ─────────────────────────────────────────────────────────────
def _send(text: str, parse_mode: str = "HTML") -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug(f"Telegram not configured — suppressed: {text[:60]}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=8,
        )
        return resp.ok
    except Exception as e:
        logger.debug(f"Telegram send error: {e}")
        return False

# ── Generic alert ─────────────────────────────────────────────────────────────
def _send_raw(text: str) -> bool:
    """Direct HTML send for guardian / internal use."""
    return _send(text)


def send_alert(message: str, level: str = "INFO") -> bool:
    if config.PAPER_MODE and level.upper() != "CRITICAL":
        logger.info(f"[PAPER MODE - suppressed] [{level}] {message}")
        return True

    prefix = {"INFO": "🐺", "WARNING": "⚠️", "CRITICAL": "🚨"}.get(level.upper(), "🐺")
    return _send(f"{prefix} <b>Wolf</b>\n{message}")

# ── TRADE ENTRY alert (fires on every live trade) ────────────────────────────
def alert_trade_entry(
    strategy: str,
    market: str,
    side: str,
    size: float,
    entry_price: float,
    confidence: float,
    paper: bool = False,
):
    """
    Compact entry alert:
    ⚡ LIVE | value_bet
    📌 YES @ $0.082  ·  $8.00
    🎯 Conf: 85%
    Will the Avalanche win the 2026 NHL?
    """
    key = f"entry:{strategy}:{market[:40]}:{side}"
    if not _rate_ok(key):
        logger.debug(f"Entry alert suppressed (rate limit): {key[:50]}")
        return

    mode = "PAPER" if paper else "LIVE"
    dot  = "⚫" if paper else "🔴"
    short_market = market[:55] + "…" if len(market) > 55 else market
    short_market = short_market.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    strat_clean = strategy.replace("_", " ").title()
    text = (
        f"{dot} <b>{strat_clean}</b>  {side} @ {entry_price:.3f}  ·  ${size:.2f}\n"
        f"<i>{short_market}</i>"
    )
    sent = _send(text)
    logger.info(f"Trade entry alert {'sent' if sent else 'FAILED'}: {strategy} {side} @{entry_price:.3f} paper={paper}")

# ── TRADE EXIT alert (fires on real resolution) ──────────────────────────────
def alert_trade_exit(
    strategy: str,
    market: str,
    side: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    won: bool,
    hold_time_min: float,
    paper: bool = False,
):
    """
    Compact exit alert:
    ✅ WIN | value_bet
    📌 YES $0.082 → $1.00
    💰 +$75.20  ·  4h 12m
    Will the Avalanche win the 2026 NHL?
    """
    result_icon = "✅" if won else "❌"
    result_word  = "WIN" if won else "LOSS"
    mode = "PAPER" if paper else "LIVE"
    hold = _fmt_duration(hold_time_min)
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    short_market = market[:55] + "…" if len(market) > 55 else market
    short_market = short_market.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    strat_clean = strategy.replace("_", " ").title()
    text = (
        f"{result_icon} <b>{strat_clean}</b>  {side}  {pnl_str}  ({hold})\n"
        f"Entry: {entry_price:.3f} → Exit: {exit_price:.3f}\n"
        f"<i>{short_market}</i>"
    )
    _send(text)

# ── KILL SWITCH ───────────────────────────────────────────────────────────────
def alert_kill_switch(drawdown_pct: float):
    _send(
        f"🚨🚨🚨 *KILL SWITCH*\n"
        f"Drawdown: {drawdown_pct:.1%}\n"
        f"All trading halted immediately.\n"
        f"Check your account."
    )

# ── DAILY HALT ────────────────────────────────────────────────────────────────
def alert_daily_halt(daily_pnl_pct: float, daily_pnl_usd: float):
    _send(
        f"⛔ *Daily loss limit hit*\n"
        f"Loss: {daily_pnl_pct:.1%}  (${daily_pnl_usd:+.2f})\n"
        f"Wolf halted until tomorrow 00:00 ET."
    )

# ── SYSTEM DOWN ───────────────────────────────────────────────────────────────
def alert_system_down(component: str, error: str):
    _send(f"🚨 *System down*: {component}\n`{error[:200]}`")

# ── GATE PASSED ───────────────────────────────────────────────────────────────
def alert_paper_gate_passed(stats: dict):
    _send(
        f"🎯 *Paper gate PASSED*\n"
        f"Trades: {stats['total_trades']}  ·  WR: {stats['win_rate']:.1%}\n"
        f"P&L: ${stats['total_pnl']:+.2f}\n\n"
        f"Ready for live — awaiting Jefe approval."
    )

# ── WR THRESHOLD ALERT ────────────────────────────────────────────────────────
def alert_wr_threshold(wr: float, trades: int, pnl: float):
    """Fires when Wolf crosses the 72% live gate WR threshold."""
    _send(
        f"🟢 *WR Gate Crossed*\n"
        f"Win Rate: {wr:.1%}  ·  {trades} trades\n"
        f"P&L: ${pnl:+.2f}\n\n"
        f"Live gate condition met. Say the word, Jefe. 🐺"
    )

# ── WHALE MOVE ────────────────────────────────────────────────────────────────
def alert_whale_move(wallet: str, market: str, side: str, size: float, venue: str):
    if config.PAPER_MODE:
        return
    _send(
        f"🐋 *Whale move* [{venue}]\n"
        f"`{wallet[:12]}…`  {side} ${size:,.0f}\n"
        f"_{market[:60]}_"
    )

# ── DRY-RUN TEST ─────────────────────────────────────────────────────────────
def alert_trade_executed(strategy: str, venue: str, market: str, side: str, size: float, paper: bool):
    """Legacy compat — routes to entry alert."""
    alert_trade_entry(strategy, market, side, size, 0.5, 0.75, paper=paper)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _fmt_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m" if m else f"{h}h"
