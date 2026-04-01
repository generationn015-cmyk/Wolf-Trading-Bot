"""Lighter Engine Configuration"""
import os

# ── Engine ──────────────────────────────────────────────────────────────
ENGINE_NAME         = "Lighter"
PAPER_MODE          = True
PAPER_STARTING_CAPITAL = 100.0

# ── Lighter API ─────────────────────────────────────────────────────────
LIGHTER_API_KEY_ID     = os.getenv("LIGHTER_API_KEY_ID", "")
LIGHTER_API_KEY_SECRET = os.getenv("LIGHTER_API_KEY_SECRET", "")
LIGHTER_WS_URL         = "wss://mainnet.lighter.xyz/ws"
LIGHTER_API_URL        = "https://mainnet.lighter.xyz/api"

# ── Markets ─────────────────────────────────────────────────────────────
PRIMARY_MARKET = "BTC-PERP"
MARKETS        = ["BTC-PERP", "ETH-PERP", "SOL-PERP"]

# ── Risk ────────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE_PCT  = 0.01    # 1% of account
MAX_LEVERAGE            = 5
DEFAULT_LEVERAGE        = 3
DAILY_LOSS_LIMIT_PCT    = 0.03    # Stop trading if down 3% today
MAX_OPEN_POSITIONS      = 5
MAX_SINGLE_MARKET_PCT   = 0.20    # No more than 20% in one market

# ── Timing ──────────────────────────────────────────────────────────────
CYCLE_INTERVAL          = 30      # seconds between strategy scans
FUNDING_CHECK_INTERVAL  = 300     # check funding rate every 5 min

# ── Database ────────────────────────────────────────────────────────────
DB_PATH = os.getenv("LIGHTER_DB_PATH", "data/lighter.db")

# ── Dashboard ───────────────────────────────────────────────────────────
DASHBOARD_ENABLED = False
DASHBOARD_API_KEY = os.getenv("LIGHTER_DASHBOARD_API_KEY", "")
