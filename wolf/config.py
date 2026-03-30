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

# ─── RISK ENGINE PARAMETERS ──────────────────────────────────────────────────
MAX_POSITION_PCT            = float(os.getenv("MAX_POSITION_PCT", "0.08"))   # 8% max per trade
DAILY_LOSS_LIMIT            = float(os.getenv("DAILY_LOSS_LIMIT", "-0.20"))  # -20% daily halt
KILL_SWITCH_THRESHOLD       = float(os.getenv("KILL_SWITCH_THRESHOLD", "-0.40"))  # -40% kill switch
MAX_OPEN_POSITIONS          = int(os.getenv("MAX_OPEN_POSITIONS", "8"))
MAX_POSITIONS_PER_STRATEGY  = int(os.getenv("MAX_POSITIONS_PER_STRATEGY", "3"))  # Allow 3 concurrent per strategy
MIN_MARKET_VOLUME           = float(os.getenv("MIN_MARKET_VOLUME", "10000")) # $10K min — paper mode; raise to $50K+ for live

# ─── STRATEGY PARAMETERS ─────────────────────────────────────────────────────
LATENCY_ARB_THRESHOLD       = float(os.getenv("LATENCY_ARB_THRESHOLD", "0.003"))  # 0.3% divergence
MIN_CONFIDENCE              = float(os.getenv("MIN_CONFIDENCE", "0.68"))     # Balanced: volume + quality
VPIN_SPIKE_THRESHOLD        = float(os.getenv("VPIN_SPIKE_THRESHOLD", "0.15"))
COPY_TRADE_MAX_AGE_SEC      = int(os.getenv("COPY_TRADE_MAX_AGE_SEC", "28800"))  # 8h — paper mode wide window; tighten to 300-600 for live
COPY_TRADE_MIN_SIZE         = float(os.getenv("COPY_TRADE_MIN_SIZE", "10"))   # $10 min — paper mode; $50+ live
COPY_DEMO_MIN_TRADES        = int(os.getenv("COPY_DEMO_MIN_TRADES", "5"))     # Demo validation trades (low — leaderboard wallets have proven PnL track record)
WHALE_ALERT_THRESHOLD       = float(os.getenv("WHALE_ALERT_THRESHOLD", "500")) # $500 whale alert

# ─── PAPER MODE GATE ─────────────────────────────────────────────────────────
PAPER_GATE_MIN_TRADES       = int(os.getenv("PAPER_GATE_MIN_TRADES", "100"))  # Need real sample before trusting
PAPER_GATE_MIN_WIN_RATE     = float(os.getenv("PAPER_GATE_MIN_WIN_RATE", "0.72"))  # Real target: 85–95%

# ─── HEALTH CHECK ────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_SEC      = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "1800"))  # 30 min

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
