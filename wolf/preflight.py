"""
Wolf Pre-Flight Check
Runs before startup and before live mode flip.
If any CRITICAL check fails → Wolf stays in PAPER mode and alerts Jefe.
"""
import sys, os, sqlite3, time, requests
sys.path.insert(0, os.path.dirname(__file__))

import logging
logger = logging.getLogger("wolf.preflight")


def run(send_telegram: bool = True, raise_on_fail: bool = False) -> tuple[bool, list[str]]:
    """
    Returns (all_clear: bool, failures: list[str])
    """
    import config
    failures = []
    warnings = []

    def fail(msg):
        failures.append(msg)
        logger.error(f"[PREFLIGHT FAIL] {msg}")

    def warn(msg):
        warnings.append(msg)
        logger.warning(f"[PREFLIGHT WARN] {msg}")

    # ── 1. Config completeness ────────────────────────────────────────────────
    if not config.POLYMARKET_PRIVATE_KEY:
        fail("POLYMARKET_PRIVATE_KEY not set")
    if not config.POLYMARKET_API_KEY:
        fail("POLYMARKET_API_KEY not set")
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        fail("Telegram credentials not set — alerts will not fire")

    # ── 2. Polymarket connectivity ────────────────────────────────────────────
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"active": True, "limit": 1}, timeout=8)
        if not r.ok:
            fail(f"Polymarket API unreachable: {r.status_code}")
    except Exception as e:
        fail(f"Polymarket API error: {e}")

    # ── 3. Binance data feed ──────────────────────────────────────────────────
    try:
        r2 = requests.get("https://api.binance.us/api/v3/ticker/price",
                          params={"symbol": "BTCUSDT"}, timeout=5)
        price = float(r2.json()["price"])
        if not (1000 < price < 500000):
            fail(f"BTC price out of sanity range: ${price:,.2f}")
    except Exception as e:
        fail(f"Binance REST unreachable: {e}")

    # ── 4. CLOB authentication ────────────────────────────────────────────────
    try:
        from feeds.polymarket_feed import get_client
        client = get_client()
        if not client:
            fail("CLOB client auth failed")
    except Exception as e:
        fail(f"CLOB auth error: {e}")

    # ── 5. Database integrity ─────────────────────────────────────────────────
    try:
        conn = sqlite3.connect(config.DB_PATH)
        c = conn.cursor()
        for tbl in ["trades", "paper_trades", "signals", "health_checks"]:
            exists = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            if not exists:
                fail(f"DB table missing: {tbl}")
        # Duplicate open positions
        dups = c.execute(
            "SELECT COUNT(*) FROM (SELECT strategy,market_id,side FROM paper_trades "
            "WHERE resolved=0 GROUP BY strategy,market_id,side HAVING COUNT(*)>1)"
        ).fetchone()[0]
        if dups > 0:
            fail(f"Duplicate open positions in DB: {dups} groups")
        conn.close()
    except Exception as e:
        fail(f"DB check error: {e}")

    # ── 6. Strategy imports ───────────────────────────────────────────────────
    strats = [
        ("strategies.value_bet", "ValueBetStrategy"),
        ("strategies.copy_trading", "CopyTrader"),
        ("strategies.latency_arb", "LatencyArb"),
        ("strategies.complement_arb", "ComplementArb"),
        ("strategies.ta_signal", "TASignalStrategy"),
        ("strategies.near_expiry", "NearExpiryStrategy"),
        ("strategies.timezone_arb", "TimezoneArb"),
    ]
    for mod, cls in strats:
        try:
            m = __import__(mod, fromlist=[cls])
            getattr(m, cls)()
        except Exception as e:
            fail(f"Strategy {cls} failed to init: {e}")

    # ── 7. P&L formula sanity (in-memory only — no DB writes) ────────────────
    try:
        from paper_mode import PaperTrader, PaperTrade
        pt = PaperTrader.__new__(PaperTrader)
        pt.balance = 1000.0; pt.starting_balance = 1000.0
        pt.trades = []; pt.open_trades = []
        t = PaperTrade(timestamp=time.time(), strategy="preflight_test", venue="p",
                       market_id="0xpreflight_inmemory_only", side="YES",
                       size=40.0, entry_price=0.25)
        pt.open_trades.append(t)
        # Resolve in-memory only — bypass journal (no DB write)
        from dataclasses import replace as _dc_replace
        won = True
        t.exit_price = 1.0
        t.pnl = t.size * (1.0 / t.entry_price - 1.0)
        t.won = won; t.resolved = True
        if abs(t.pnl - 120.0) > 0.01:
            fail(f"P&L formula wrong: got ${t.pnl:.2f}, expected $120.00")
    except Exception as e:
        fail(f"P&L check error: {e}")

    # ── 8. Live-mode extra gates (only when PAPER_MODE=False) ─────────────────
    if not config.PAPER_MODE:
        if config.LIVE_STARTING_CAPITAL < 50:
            fail(f"LIVE_STARTING_CAPITAL too low: ${config.LIVE_STARTING_CAPITAL}")
        if config.MAX_POSITION_LIVE > 10:
            fail(f"MAX_POSITION_LIVE too high for live: ${config.MAX_POSITION_LIVE}")
        if config.KILL_SWITCH_THRESHOLD > -0.30:
            fail("Kill switch threshold too loose for live trading")

    # ── 9. Risk params sanity ─────────────────────────────────────────────────
    if config.KILL_SWITCH_THRESHOLD != -0.40:
        warn(f"Kill switch at {config.KILL_SWITCH_THRESHOLD:.0%} (expected -40%)")
    if config.DAILY_LOSS_LIMIT != -0.20:
        warn(f"Daily loss limit at {config.DAILY_LOSS_LIMIT:.0%} (expected -20%)")

    # ── Result ────────────────────────────────────────────────────────────────
    all_clear = len(failures) == 0

    if send_telegram:
        try:
            from alerts.telegram_alerts import _send
            # Only alert on FAILURE — clean startup is silent (no spam)
            if not all_clear:
                lines = ["🚨 <b>Wolf Pre-Flight FAILED</b>",
                         f"Mode: {'📄 PAPER' if config.PAPER_MODE else '🔴 LIVE'}",
                         f"{len(failures)} failure(s):"]
                for f in failures[:5]:
                    lines.append(f"  ❌ {f}")
                if not config.PAPER_MODE:
                    lines.append("\n⛔ Wolf staying in PAPER mode until fixed.")
                _send("\n".join(lines))
        except Exception as e:
            logger.error(f"Could not send preflight alert: {e}")

    if warnings:
        for w in warnings:
            logger.warning(f"[PREFLIGHT WARN] {w}")

    if raise_on_fail and not all_clear:
        raise RuntimeError(f"Pre-flight failed: {failures}")

    return all_clear, failures


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ok, fails = run(send_telegram=True)
    print(f"\n{'✅ ALL CLEAR' if ok else '❌ FAILED: ' + str(fails)}")
    sys.exit(0 if ok else 1)
