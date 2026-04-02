"""
Wolf Trading Bot — Configuration
All settings loaded from environment variables. Never hardcode credentials.
"""
import os
from dotenv import load_dotenv

# Load wolf-specific .env first, then fall back to openclaw .env
_wolf_env = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_wolf_env, override=True)
load_dotenv(os.path.expanduser("~/.openclaw/.env"))

# ─── PAPER MODE (MUST be explicitly set False to go live) ───────────────────
PAPER_MODE = os.getenv("WOLF_PAPER_MODE", "true").lower() != "false"

# ─── POLYMARKET CREDENTIALS ─────────────────────────────────────────────────
POLYMARKET_PRIVATE_KEY      = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_API_KEY          = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET       = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE   = os.getenv("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_WALLET_ADDRESS   = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
POLYMARKET_CLOB_URL         = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL        = "https://gamma-api.polymarket.com"

# ─── KALSHI CREDENTIALS ─────────────────────────────────────────────────────
KALSHI_API_KEY_ID           = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH     = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_API_KEY              = os.getenv("KALSHI_API_KEY", "")      # email login
KALSHI_API_SECRET           = os.getenv("KALSHI_API_SECRET", "")   # password
KALSHI_DEMO                 = os.getenv("KALSHI_DEMO", "true")     # use demo until credentialed
KALSHI_ENABLED              = os.getenv("KALSHI_ENABLED", "false").lower() == "true"  # OFF until Jefe authorizes
KALSHI_BASE_URL             = "https://trading.kalshi.com/trade-api/v2"
KALSHI_DEMO_URL             = "https://demo-api.kalshi.co/trade-api/v2"

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID            = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── BINANCE FEED ────────────────────────────────────────────────────────────
BINANCE_WS_BTC              = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_WS_ETH              = "wss://stream.binance.com:9443/ws/ethusdt@trade"

# ─── CAPITAL CONFIGURATION ───────────────────────────────────────────────────
LIVE_STARTING_CAPITAL       = float(os.getenv("LIVE_STARTING_CAPITAL", "100.0"))   # Phase 1 live: $100
PAPER_STARTING_CAPITAL      = float(os.getenv("PAPER_STARTING_CAPITAL", "100.0"))  # Paper mirrors real $100 live account — earn its way up

# ── Blueprint Risk Rules ──────────────────────────────────────────────────────
DAILY_LOSS_CAP_PCT          = float(os.getenv("DAILY_LOSS_CAP_PCT", "0.03"))  # 3% daily loss = halt ($3 on $100)
MODULE_CONSECUTIVE_LOSS_LIMIT = int(os.getenv("MODULE_CONSECUTIVE_LOSS_LIMIT", "2"))  # 2 losses → 24h module pause
MODULE_PAUSE_SECONDS        = int(os.getenv("MODULE_PAUSE_SECONDS", "86400"))  # 24h pause after circuit break
PHASE1_MAX_PORTFOLIO        = float(os.getenv("PHASE1_MAX_PORTFOLIO", "200.0"))  # Phase 1 cap — A/B/C only until $200

# ─── RISK ENGINE PARAMETERS ──────────────────────────────────────────────────
# Live: 8% per trade on $100 = $8 max/trade. Conservative for Phase 1.
MAX_POSITION_PCT            = float(os.getenv("MAX_POSITION_PCT", "0.05"))   # 5% max per trade ($5 on $100) — Blueprint Phase 1 hard cap
MAX_POSITION_PAPER          = float(os.getenv("MAX_POSITION_PAPER", "5.0"))   # Paper: 5% of $100 = $5 max/trade (Blueprint Rule 1)
MAX_POSITION_LIVE           = float(os.getenv("MAX_POSITION_LIVE", "5.0"))   # Hard cap $5 per live trade
MIN_POSITION_LIVE           = float(os.getenv("MIN_POSITION_LIVE", "1.0"))   # Min $1 (Polymarket minimum)
DAILY_LOSS_LIMIT            = float(os.getenv("DAILY_LOSS_LIMIT", "-0.20"))  # -20% daily halt ($20 on live)
KILL_SWITCH_THRESHOLD       = float(os.getenv("KILL_SWITCH_THRESHOLD", "-0.40"))  # -40% kill switch ($40 loss → full stop)
MAX_OPEN_POSITIONS          = int(os.getenv("MAX_OPEN_POSITIONS", "8"))   # live hard cap
MAX_OPEN_POSITIONS_PAPER    = int(os.getenv("MAX_OPEN_POSITIONS_PAPER", "20"))  # paper: wider net for data collection
VALUE_BET_MAX_DAYS          = int(os.getenv("VALUE_BET_MAX_DAYS", "14"))  # Skip markets resolving >14 days out
MAX_HOLD_HOURS              = float(os.getenv("MAX_HOLD_HOURS", "48"))     # Force-exit after 48h — gives prediction markets time to resolve naturally
MAX_POSITIONS_PER_STRATEGY  = int(os.getenv("MAX_POSITIONS_PER_STRATEGY", "8"))   # 8 per strategy allows active trading across all strategies
MIN_MARKET_VOLUME           = float(os.getenv("MIN_MARKET_VOLUME", "1000")) # $1K min liquidity

# ─── STRATEGY PARAMETERS ─────────────────────────────────────────────────────
LATENCY_ARB_THRESHOLD       = float(os.getenv("LATENCY_ARB_THRESHOLD", "0.003"))  # 0.3% divergence
MIN_CONFIDENCE              = float(os.getenv("MIN_CONFIDENCE", "0.68"))     # Balanced: volume + quality
VPIN_SPIKE_THRESHOLD        = float(os.getenv("VPIN_SPIKE_THRESHOLD", "0.30"))  # raised 0.15→0.30: allows 55/45–65/35 markets; still blocks toxic 70/30+
COPY_TRADE_MAX_AGE_SEC      = int(os.getenv("COPY_TRADE_MAX_AGE_SEC", "600" if not (os.getenv("WOLF_PAPER_MODE","true").lower() != "false") else "28800"))  # Live=10min fresh signals only; Paper=8h wide window
COPY_TRADE_MIN_SIZE         = float(os.getenv("COPY_TRADE_MIN_SIZE", "10"))   # $10 min whale size — wider net for more signals
COPY_DEMO_MIN_TRADES        = int(os.getenv("COPY_DEMO_MIN_TRADES", "5"))     # Demo validation trades (low — leaderboard wallets have proven PnL track record)
WHALE_ALERT_THRESHOLD       = float(os.getenv("WHALE_ALERT_THRESHOLD", "500")) # $500 whale alert

# ─── PAPER MODE GATE ─────────────────────────────────────────────────────────
PAPER_GATE_MIN_TRADES       = int(os.getenv("PAPER_GATE_MIN_TRADES", "100"))  # Need real sample before trusting
PAPER_GATE_MIN_WIN_RATE     = float(os.getenv("PAPER_GATE_MIN_WIN_RATE", "0.72"))  # Real target: 85–95%

# ─── HEALTH CHECK ────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_SEC      = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "21600"))  # 6 hours

# ─── DATABASE ────────────────────────────────────────────────────────────────
DB_PATH                     = os.getenv("WOLF_DB_PATH", "/data/.openclaw/workspace/wolf/wolf_data.db")

def validate():
    """Validate critical config on startup. Warn but don't crash in paper mode."""
    warnings = []
    if not POLYMARKET_PRIVATE_KEY:
        warnings.append("POLYMARKET_PRIVATE_KEY not set")
    if not TELEGRAM_BOT_TOKEN:
        warnings.append("TELEGRAM_BOT_TOKEN not set — alerts disabled")
    if not TELEGRAM_CHAT_ID:
        warnings.append("TELEGRAM_CHAT_ID not set — alerts disabled")
    if PAPER_MODE:
        print("🐺 Wolf starting in PAPER MODE — no real trades will be placed")
    else:
        print("⚠️  Wolf starting in LIVE MODE — real money at risk")
    for w in warnings:
        print(f"  ⚠️  CONFIG WARNING: {w}")
    return warnings

# Dashboard
WOLF_DASHBOARD_API_KEY      = os.getenv("WOLF_DASHBOARD_API_KEY", "")
# Dashboard password — set WOLF_DASHBOARD_PASSWORD in .env once and it persists forever.
# Never auto-generated. Empty string = no auth (local-only access).
WOLF_DASHBOARD_PASSWORD     = os.getenv("WOLF_DASHBOARD_PASSWORD", "")
