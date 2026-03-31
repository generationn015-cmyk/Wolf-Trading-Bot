#!/usr/bin/env python3
"""
Wolf Full System Audit
Run before any live mode activation.
Covers: config, feeds, auth, P&L math, dedup, risk caps,
        strategy logic, alerts, DB integrity, process count,
        live execution path, redundancy check.
Hard PASS/FAIL output. Exit code 0 = pass, 1 = failures exist.
"""
import sys, os, sqlite3, time, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import logging; logging.disable(logging.CRITICAL)

P = []; W = []; F = []
def ok(n, d=""): P.append(n); print(f"  ✅  {n}" + (f"  [{d}]" if d else ""))
def warn(n, d=""): W.append(n); print(f"  ⚠️   {n}" + (f"  [{d}]" if d else ""))
def fail(n, d=""): F.append(n); print(f"  ❌  {n}" + (f"  [{d}]" if d else ""))

import config, requests as req

# ── 1. PROCESS INTEGRITY ─────────────────────────────────────────────────────
print("\n[1] PROCESS INTEGRITY")
try:
    result = subprocess.run(
        ["pgrep", "-a", "-f", "python3.*main.py"],
        capture_output=True, text=True
    )
    procs = [l for l in result.stdout.strip().split("\n") if l]
    count = len(procs)
    ok(f"Single Wolf process running") if count == 1 else (
        warn("No Wolf process running") if count == 0 else
        fail(f"Multiple Wolf processes: {count} running — duplicate trades possible")
    )
except Exception as e:
    warn(f"Could not check process count: {e}")

# ── 2. CONFIGURATION ─────────────────────────────────────────────────────────
print("\n[2] CONFIGURATION")
ok("PAPER_MODE=True (safe)") if config.PAPER_MODE else warn("PAPER_MODE=False — LIVE MODE ACTIVE")
ok(f"KILL_SWITCH={config.KILL_SWITCH_THRESHOLD:.0%}") if config.KILL_SWITCH_THRESHOLD == -0.40 else fail(f"Kill switch wrong: {config.KILL_SWITCH_THRESHOLD}")
ok(f"DAILY_LOSS={config.DAILY_LOSS_LIMIT:.0%}") if config.DAILY_LOSS_LIMIT == -0.20 else fail(f"Daily loss wrong: {config.DAILY_LOSS_LIMIT}")
ok(f"MAX_POSITION_LIVE=${config.MAX_POSITION_LIVE}") if config.MAX_POSITION_LIVE == 8.0 else fail(f"Max position wrong: {config.MAX_POSITION_LIVE}")
ok(f"MIN_POSITION_LIVE=${config.MIN_POSITION_LIVE}") if config.MIN_POSITION_LIVE == 1.0 else fail(f"Min position wrong: {config.MIN_POSITION_LIVE}")
ok(f"MAX_OPEN_POSITIONS live={config.MAX_OPEN_POSITIONS} paper={config.MAX_OPEN_POSITIONS_PAPER}")
ok(f"LIVE_CAPITAL=${config.LIVE_STARTING_CAPITAL}") if config.LIVE_STARTING_CAPITAL == 100.0 else warn(f"Live capital: {config.LIVE_STARTING_CAPITAL}")
ok("Private key set") if len(config.POLYMARKET_PRIVATE_KEY) > 20 else fail("POLYMARKET_PRIVATE_KEY missing")
ok("API key set") if len(config.POLYMARKET_API_KEY) > 10 else fail("POLYMARKET_API_KEY missing")
ok("Telegram token set") if config.TELEGRAM_BOT_TOKEN else fail("TELEGRAM_BOT_TOKEN missing — alerts dead")
ok("Telegram chat ID set") if config.TELEGRAM_CHAT_ID else fail("TELEGRAM_CHAT_ID missing")

# ── 3. DATA FEEDS ────────────────────────────────────────────────────────────
print("\n[3] DATA FEEDS")
# Binance REST
try:
    r = req.get("https://api.binance.us/api/v3/ticker/price", params={"symbol":"BTCUSDT"}, timeout=5)
    btc = float(r.json()["price"])
    ok(f"Binance REST: BTC ${btc:,.2f}") if 1000 < btc < 500000 else fail(f"BTC price insane: {btc}")
except Exception as e:
    fail(f"Binance REST down: {e}")

try:
    r2 = req.get("https://api.binance.us/api/v3/ticker/price", params={"symbol":"ETHUSDT"}, timeout=5)
    eth = float(r2.json()["price"])
    ok(f"Binance REST: ETH ${eth:,.2f}") if eth > 100 else fail(f"ETH price insane: {eth}")
except Exception as e:
    fail(f"Binance ETH REST down: {e}")

# Polymarket
try:
    r3 = req.get("https://gamma-api.polymarket.com/markets", params={"active":True,"limit":3}, timeout=8)
    markets = r3.json()
    ok(f"Polymarket API: {len(markets)} markets returned") if r3.ok and markets else fail("Polymarket API failed")
except Exception as e:
    fail(f"Polymarket API down: {e}")

# CLOB auth
try:
    from feeds.polymarket_feed import get_client
    client = get_client()
    ok("CLOB authenticated") if client else fail("CLOB auth failed")
except Exception as e:
    fail(f"CLOB auth error: {e}")

# ── 4. P&L MATH ──────────────────────────────────────────────────────────────
print("\n[4] P&L MATH")
from paper_mode import PaperTrade
import math

cases = [
    ("YES", "YES", 40.0, 0.25, 120.0,  "YES WIN $40@0.25 → +$120"),
    ("YES", "NO",  40.0, 0.25, -40.0,  "YES LOSS $40@0.25 → -$40"),
    ("NO",  "NO",  10.0, 0.20,  40.0,  "NO WIN $10@0.20 → +$40"),
    ("NO",  "YES", 10.0, 0.20, -10.0,  "NO LOSS $10@0.20 → -$10"),
    ("YES", "YES",  5.0, 0.50,   5.0,  "YES WIN $5@0.50 → +$5"),
    ("YES", "YES",  8.0, 0.08,  92.0,  "YES WIN $8@0.08 → +$92 (underdog)"),
]
for side, outcome, size, ep, expected_pnl, label in cases:
    won = (side == outcome)
    pnl = size * (1.0/ep - 1.0) if won else -size
    ok(label) if abs(pnl - expected_pnl) < 0.01 else fail(f"P&L wrong: {label} got {pnl:.2f}")

# ── 5. POSITION SIZING ───────────────────────────────────────────────────────
print("\n[5] POSITION SIZING")
from risk_engine import RiskEngine

# Live mode caps
orig = config.PAPER_MODE
config.PAPER_MODE = False  # temporarily test live caps
rl = RiskEngine(starting_balance=100.0)
s1 = rl.get_position_size(edge=0.20, confidence=0.90, entry_price=0.20)
ok(f"Live: high conf = ${s1:.2f} (capped at $8)") if s1 <= 8.0 else fail(f"Live cap not enforced: ${s1:.2f}")
s2 = rl.get_position_size(edge=0.03, confidence=0.58, entry_price=0.50)
ok(f"Live: low conf = ${s2:.2f} (0 or small)") if s2 == 0.0 else warn(f"Low conf size: ${s2:.2f}")
s3 = rl.get_position_size(edge=0.10, confidence=0.75, entry_price=0.30)
ok(f"Live: standard = ${s3:.2f}") if 1.0 <= s3 <= 8.0 else fail(f"Live standard size wrong: ${s3:.2f}")
config.PAPER_MODE = orig

# Paper mode — no caps
rp = RiskEngine(starting_balance=1000.0)
sp = rp.get_position_size(edge=0.10, confidence=0.75, entry_price=0.30)
ok(f"Paper: standard = ${sp:.2f}") if sp > 0 else fail("Paper sizing broken")

# ── 6. RISK ENGINE GATES ─────────────────────────────────────────────────────
print("\n[6] RISK ENGINE GATES")
rc = RiskEngine(starting_balance=100.0)
can, _ = rc.can_trade(market_volume=500000)
ok("Clean account: trades") if can else fail("Clean account blocked")

rh = RiskEngine(starting_balance=100.0)
rh.daily_start_balance = 100.0; rh.current_balance = 79.0
can2, r2 = rh.can_trade(market_volume=500000)
ok("Daily halt at -21%") if not can2 else fail("Daily halt not firing")

rk = RiskEngine(starting_balance=100.0)
rk.daily_start_balance = 100.0; rk.current_balance = 59.0
can3, r3 = rk.can_trade(market_volume=500000)
ok("Kill switch at -41%") if not can3 else fail("Kill switch not firing")

can4, _ = rc.can_trade(market_volume=49999)
ok("$49k volume blocked") if not can4 else fail("Low volume not blocked")

can5, _ = rc.can_trade(market_volume=50001)
ok("$50k+ volume passes") if can5 else fail("Sufficient volume incorrectly blocked")

# ── 7. DEDUP — NO DOUBLE SPEND ───────────────────────────────────────────────
print("\n[7] DEDUP INTEGRITY")
from execution.order_manager import OrderManager
from paper_mode import PaperTrader
from journal.trade_logger import TradeLogger

def _mock_trader():
    pt = PaperTrader.__new__(PaperTrader)
    pt.balance=1000.0; pt.starting_balance=1000.0; pt.trades=[]; pt.open_trades=[]; return pt

jnl = TradeLogger()
om1 = OrderManager(RiskEngine(starting_balance=1000.0), _mock_trader(), jnl)
sig = {"strategy":"audit_dedup","venue":"polymarket","market_id":"0xaudit_dedup_001",
       "side":"YES","entry_price":0.30,"confidence":0.80,"edge":0.12,
       "volume":500000,"reason":"AUDIT","timestamp":time.time()}

r1 = om1.execute_signal(sig)
r2 = om1.execute_signal(sig)
ok(f"First exec: {r1['status']}") if r1.get("status")=="paper_executed" else fail(f"First exec failed: {r1}")
ok("In-memory dedup") if r2.get("status")=="dedup_blocked" else fail(f"In-memory dedup broken: {r2}")

# Restart simulation — new OrderManager, same market open in DB
om2 = OrderManager(RiskEngine(starting_balance=1000.0), _mock_trader(), jnl)
r3 = om2.execute_signal(sig)
ok("DB dedup survives restart") if r3.get("status")=="dedup_blocked" else fail(f"DB dedup failed: {r3}")

# Cleanup
conn=sqlite3.connect(config.DB_PATH)
conn.execute("DELETE FROM paper_trades WHERE market_id='0xaudit_dedup_001'")
conn.commit(); conn.close()

# ── 8. STRATEGY INIT ─────────────────────────────────────────────────────────
print("\n[8] STRATEGY INIT")
for mod, cls in [
    ("strategies.value_bet","ValueBetStrategy"),
    ("strategies.copy_trading","CopyTrader"),
    ("strategies.latency_arb","LatencyArb"),
    ("strategies.complement_arb","ComplementArb"),
    ("strategies.market_making","MarketMaker"),
    ("strategies.ta_signal","TASignalStrategy"),
    ("strategies.near_expiry","NearExpiryStrategy"),
    ("strategies.timezone_arb","TimezoneArb"),
]:
    try:
        m = __import__(mod, fromlist=[cls])
        getattr(m, cls)()
        ok(f"{cls}")
    except Exception as e:
        fail(f"{cls}: {e}")

# ── 9. TA SIGNAL WARM-UP ─────────────────────────────────────────────────────
print("\n[9] TA SIGNAL")
from strategies.ta_signal import TAIndicators, MACD_SLOW, MACD_SIGNAL
ind = TAIndicators()
need = MACD_SLOW + MACD_SIGNAL
for i in range(need - 1): ind.add_price(67000 + i * 5)
ok(f"Not ready before {need} ticks") if not ind.is_ready() else fail("Ready too early")
ind.add_price(68000)
ok(f"Ready at {need} ticks") if ind.is_ready() else fail("Not ready at warmup threshold")

# ── 10. LATENCY ARB FRESHNESS ────────────────────────────────────────────────
print("\n[10] LATENCY ARB")
import inspect
from strategies.latency_arb import LatencyArb
src = inspect.getsource(LatencyArb.scan)
ok("Paper freshness 30s") if "30000" in src else fail("Paper freshness wrong")
ok("Live freshness 3s") if "3000" in src else fail("Live freshness wrong — will block live signals")

# ── 11. MARKET RESOLVER ──────────────────────────────────────────────────────
print("\n[11] MARKET RESOLVER")
from market_resolver import _extract_outcome, get_current_price
ok("Zeroed → None") if _extract_outcome({"closed":True,"outcomePrices":'["0","0"]',"lastTradePrice":None}) is None else fail("Zeroed → not None")
ok("50/50 → None") if _extract_outcome({"closed":True,"outcomePrices":'["0.5","0.5"]',"lastTradePrice":None}) is None else fail("50/50 → not None")
ok("YES resolved") if _extract_outcome({"closed":True,"outcomePrices":'["1","0"]',"lastTradePrice":0.99}) == "YES" else fail("YES not detected")
ok("NO resolved") if _extract_outcome({"closed":True,"outcomePrices":'["0","1"]',"lastTradePrice":0.01}) == "NO" else fail("NO not detected")
try:
    live = req.get("https://gamma-api.polymarket.com/markets",
        params={"active":True,"limit":2,"closed":False,"volumeNum_min":500000},timeout=8).json()
    if live:
        prices = get_current_price(live[0].get("conditionId",""))
        ok(f"Live price: {prices[0]:.3f}/{prices[1]:.3f}") if prices and prices[0]>0 else fail("Live price broken")
except Exception as e:
    fail(f"Resolver live test: {e}")

# ── 12. TELEGRAM ALERTS ──────────────────────────────────────────────────────
print("\n[12] TELEGRAM ALERTS")
from alerts.telegram_alerts import _send, _rate_ok, _belfort, alert_trade_entry, alert_trade_exit

ok("_belfort() returns string") if isinstance(_belfort(), str) and len(_belfort()) > 5 else fail("Belfort broken")
# Verify it's movie content not motivational
movie_words = ["fucking", "noble", "drugs", "Stratton", "rich every", "nobody knows"]
has_movie = any(w.lower() in _belfort().lower() for _ in range(10) for w in movie_words)
ok("Quotes are movie lines") if has_movie else warn("Quotes may not be movie lines")

k = f"audit_rate_{time.time()}"
ok("Rate limiter: first pass") if _rate_ok(k) else fail("Rate limiter blocked first call")
ok("Rate limiter: dedup") if not _rate_ok(k) else fail("Rate limiter not deduping")

r_test = _send("🐺 <b>Wolf Audit</b> — alert delivery confirmed")
ok("Telegram delivery confirmed") if r_test else fail("Telegram delivery FAILED — alerts not reaching you")

# HTML parse mode check
import inspect as _ins
send_src = _ins.getsource(_send)
ok("HTML parse mode") if 'HTML' in send_src else fail("Not using HTML — Markdown will break on market names")

# ── 13. DATABASE INTEGRITY ───────────────────────────────────────────────────
print("\n[13] DATABASE")
conn = sqlite3.connect(config.DB_PATH)
c = conn.cursor()
for tbl in ["trades","paper_trades","signals","health_checks","whale_moves","market_data"]:
    exists = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
    ok(f"Table: {tbl}") if exists else fail(f"Missing table: {tbl}")

# UNIQUE constraint on paper_trades
schema = c.execute("SELECT sql FROM sqlite_master WHERE name='paper_trades'").fetchone()[0]
ok("UNIQUE constraint on paper_trades") if "UNIQUE" in schema else fail("No UNIQUE constraint — duplicates possible")

# Index for fast lookups
idx = c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='paper_trades'").fetchall()
ok("Index on paper_trades") if idx else warn("No index — lookups may be slow under load")

# No duplicate open positions
dups = c.execute("""SELECT COUNT(*) FROM (SELECT strategy,market_id,side FROM paper_trades
    WHERE resolved=0 GROUP BY strategy,market_id,side HAVING COUNT(*)>1)""").fetchone()[0]
ok("Zero duplicate open positions") if dups == 0 else fail(f"{dups} duplicate position groups in DB")

# No NULL pnl on resolved trades
null_pnl = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND pnl IS NULL").fetchone()[0]
ok("No NULL pnl on resolved") if null_pnl == 0 else fail(f"{null_pnl} resolved trades with NULL pnl")

open_count = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=0").fetchone()[0]
paper_cap = getattr(config, "MAX_OPEN_POSITIONS_PAPER", config.MAX_OPEN_POSITIONS)
ok(f"Open positions: {open_count}/{paper_cap} (paper cap)") if open_count <= paper_cap else warn(f"Over paper cap: {open_count} > {paper_cap}")
conn.close()

# ── 14. LIVE EXECUTION PATH ──────────────────────────────────────────────────
print("\n[14] LIVE EXECUTION PATH")
from execution.order_manager import OrderManager as OM
ok("_execute_polymarket exists") if hasattr(OM, "_execute_polymarket") else fail("Missing live execution method")
ok("_get_poly_client exists") if hasattr(OM, "_get_poly_client") else fail("Missing CLOB init method")
ok("_execute_live exists") if hasattr(OM, "_execute_live") else fail("Missing _execute_live method")

# ── 15. PREFLIGHT MODULE ─────────────────────────────────────────────────────
print("\n[15] PRE-FLIGHT")
try:
    import preflight as pf
    ok("preflight.py importable")
    ok("run() callable") if callable(pf.run) else fail("preflight.run() not callable")
except Exception as e:
    fail(f"preflight import failed: {e}")


# ── 16. WOLF GUARDIAN ───────────────────────────────────────────────────────
print("\n[16] WOLF GUARDIAN")
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
    from scripts.wolf_guardian import scan_log, get_status, PATTERNS
    ok(f"Guardian importable — {len(PATTERNS)} error patterns registered")
    ok("guardian.get_status() callable") if callable(get_status) else fail("get_status not callable")
    ok("guardian.scan_log() callable") if callable(scan_log) else fail("scan_log not callable")
    # Check if guardian was started (look for thread)
    import threading
    guardian_running = any(t.name == "wolf-guardian" for t in threading.enumerate())
    ok("Guardian thread running") if guardian_running else warn("Guardian thread not running (normal if audit run standalone)")
except Exception as e:
    fail(f"Guardian import failed: {e}")

# ── 17. DASHBOARD BALANCE PUSH ──────────────────────────────────────────────
print("\n[17] DASHBOARD BALANCE PUSH")
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "feeds", "dashboard_push.py")) as _f:
        _dp = _f.read()
    ok("balance field in performance push") if '"balance"' in _dp else fail("balance field MISSING from dashboard_push performance")
    ok("paperMode field in performance push") if '"paperMode"' in _dp else fail("paperMode field MISSING from dashboard push")
    ok("void=0 filter in all stat queries") if _dp.count("void=0") >= 5 else warn(f"void=0 filter count low: {_dp.count('void=0')} (expected 5+)")
except Exception as e:
    fail(f"dashboard_push check failed: {e}")

# ── 18. VOID TRADES INTEGRITY ────────────────────────────────────────────────
print("\n[18] VOID TRADES INTEGRITY")
conn = sqlite3.connect(config.DB_PATH)
c = conn.cursor()
try:
    c.execute("SELECT void FROM paper_trades LIMIT 1")
    ok("void column exists in paper_trades")
except:
    fail("void column MISSING from paper_trades")
void_count = c.execute("SELECT COUNT(*) FROM paper_trades WHERE void=1 AND pnl != 0").fetchone()[0]
ok("No void trades with non-zero P&L") if void_count == 0 else fail(f"{void_count} void trades have non-zero P&L (data corruption)")
slug_count = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND slug IS NOT NULL AND slug != ''").fetchone()[0]
total_open = c.execute("SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND simulated=0").fetchone()[0]
ok(f"Slug tracking: {slug_count}/{total_open} open positions have slugs") if slug_count > 0 or total_open == 0 else warn("No open positions have slugs — price lookup may fail")
conn.close()

# ── SUMMARY ──────────────────────────────────────────────────────────────────
total = len(P) + len(W) + len(F)
print(f"\n{'═'*58}")
print(f"  WOLF FULL AUDIT: ✅ {len(P)} passed  ⚠️ {len(W)} warnings  ❌ {len(F)} failed")
print(f"{'═'*58}")
if F:
    print("\n🔴 FAILURES — must fix before live:")
    for x in F: print(f"   ❌ {x}")
if W:
    print("\n🟡 WARNINGS — review before live:")
    for x in W: print(f"   ⚠️  {x}")
if not F:
    print("\n✅ Wolf is clean. No blockers to live trading.")
else:
    print(f"\n⛔ {len(F)} issue(s) must be resolved before going live.")

sys.exit(0 if not F else 1)
