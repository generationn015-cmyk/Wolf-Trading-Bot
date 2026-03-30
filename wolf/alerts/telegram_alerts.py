"""
Wolf Trading Bot — Telegram Alerts
Sends alerts to Jefe. INFO / WARNING / CRITICAL levels.
CRITICAL alerts are always sent regardless of mode.
INFO/WARNING alerts are suppressed in paper mode — only fire in live mode.
"""
import requests
import logging
import config

logger = logging.getLogger("wolf.alerts")

def send_alert(message: str, level: str = "INFO"):
    """Send a Telegram alert to Jefe.
    
    In paper mode: only CRITICAL alerts are sent.
    In live mode: all alerts are sent.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning(f"Telegram not configured. [{level}] {message}")
        return False

    # Suppress non-critical alerts in paper mode
    if config.PAPER_MODE and level.upper() != "CRITICAL":
        logger.info(f"[PAPER MODE - suppressed] [{level}] {message}")
        return True

    prefix = {
        "INFO": "🐺",
        "WARNING": "⚠️",
        "CRITICAL": "🚨🚨🚨",
    }.get(level.upper(), "🐺")

    text = f"{prefix} *Wolf [{level}]*\n{message}"

    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=10)
        if not resp.ok:
            logger.error(f"Telegram send failed: {resp.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def alert_kill_switch(drawdown_pct: float):
    send_alert(f"KILL SWITCH TRIGGERED\nDrawdown: {drawdown_pct:.1%}\nAll trading halted.", "CRITICAL")

def alert_daily_halt(daily_pnl_pct: float):
    send_alert(f"Daily loss limit hit: {daily_pnl_pct:.1%}\nTrading halted until tomorrow.", "CRITICAL")

def alert_whale_move(wallet: str, market: str, side: str, size: float, venue: str):
    send_alert(f"Whale move detected [{venue}]\nWallet: {wallet[:10]}...\nMarket: {market}\nSide: {side} | Size: ${size:,.0f}", "WARNING")

def alert_system_down(component: str, error: str):
    send_alert(f"System component down: {component}\nError: {error}", "CRITICAL")

def alert_paper_gate_passed(stats: dict):
    send_alert(
        f"Paper gate PASSED! 🎯\nTrades: {stats['total_trades']}\nWin rate: {stats['win_rate']:.1%}\nP&L: ${stats['total_pnl']:+.2f}\n\nReady to request live authorization from Jefe.",
        "INFO"
    )

def alert_trade_executed(strategy: str, venue: str, market: str, side: str, size: float, paper: bool):
    mode = "PAPER" if paper else "LIVE"
    send_alert(f"[{mode}] Trade executed\nStrategy: {strategy} | Venue: {venue}\nMarket: {market}\n{side} ${size:.2f}", "INFO")
