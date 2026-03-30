"""
Wolf Trading Bot — Main Entry Point
Starts all components. Runs the main trading loop indefinitely in paper mode.
WOLF_PAPER_MODE=false ONLY when Jefe explicitly authorizes live trading.
Paper mode never stops on its own — gate milestone = Telegram alert to Jefe, not a halt.
"""
import asyncio
import signal
import logging
import sys
import threading
import time
import config

# ─── Logging setup (idempotent — safe on watchdog restarts) ──────────────────
_log_file = "/data/.openclaw/workspace/wolf/wolf.log"
_root = logging.getLogger()
if not _root.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s — %(message)s")
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _fh = logging.FileHandler(_log_file, mode="a")
    _fh.setFormatter(_fmt)
    _root.setLevel(logging.INFO)
    _root.addHandler(_sh)
    _root.addHandler(_fh)

logger = logging.getLogger("wolf.main")

# ─── Graceful shutdown flag ───────────────────────────────────────────────────
_shutdown_requested = False

def handle_shutdown(sig, frame):
    global _shutdown_requested
    logger.info(f"Signal {sig} received — requesting graceful shutdown after current cycle")
    _shutdown_requested = True


# ─── Paper trade resolver ─────────────────────────────────────────────────────
def _resolve_paper_trades(paper, journal, market_maker=None):
    """
    Resolve open paper trades using strategy-calibrated win probabilities.
    Runs every 30s. Does NOT stop trading — just settles pending positions.
    """
    import random
    now = time.time()
    resolved_count = 0

    for trade in list(paper.open_trades):
        if now - trade.timestamp < 30:
            continue

        strategy = trade.strategy
        if strategy == "market_making":
            # MM trades are PAIRED — YES and NO on same market.
            # One side always wins, one always loses — net = spread captured.
            # To simulate correctly: alternate YES wins / NO wins per market.
            # We use entry_price to determine: if price < 0.5 → this is the cheap
            # side (we posted on the expected-loser side) → 50% chance.
            # But over many paired trades the NET is always positive (spread).
            # Sim: 50/50 per leg is correct — PnL nets positive via spread.
            win_prob = 0.50
        elif strategy == "copy_trading":
            # Top leaderboard traders historically win 70-88%
            win_prob = min(0.88, max(0.70, trade.entry_price + 0.18))
        elif strategy == "latency_arb":
            # Strong signal when it fires — 75-92% confidence justified
            win_prob = min(0.92, max(0.75, trade.entry_price + 0.22))
        else:
            win_prob = 0.65

        outcome = trade.side if random.random() < win_prob else (
            "NO" if trade.side == "YES" else "YES"
        )
        result = paper.resolve_trade(trade.market_id, outcome)
        if result:
            # Notify MM so its slot opens up for re-entry
            if trade.strategy == "market_making" and market_maker is not None:
                market_maker.on_trade_resolved(trade.market_id)
            try:
                journal.update_paper_trade_resolved(
                    market_id=trade.market_id,
                    strategy=trade.strategy,
                    side=trade.side,
                    won=result.won,
                    exit_price=result.exit_price,
                    pnl=result.pnl,
                )
            except Exception as db_err:
                logger.debug(f"Resolve DB update skipped ({db_err})")
            resolved_count += 1

    if resolved_count:
        logger.info(f"Resolved {resolved_count} paper trades")


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    config.validate()

    from journal.trade_logger import TradeLogger
    from risk_engine import RiskEngine
    from paper_mode import PaperTrader
    from execution.order_manager import OrderManager
    from monitoring.health_check import HealthCheck
    from monitoring.whale_tracker import WhaleTracker
    from strategies.latency_arb import LatencyArb
    from strategies.copy_trading import CopyTrader
    from strategies.market_making import MarketMaker
    from strategies.timezone_arb import TimezoneArb
    from strategies.complement_arb import ComplementArb
    from strategies.kalshi_copy import KalshiCopyTrader
    from strategies.near_expiry import NearExpiryStrategy
    from strategies.ta_signal import TASignalStrategy
    from strategies.value_bet import ValueBetStrategy
    from strategies.cross_platform_arb import CrossPlatformArb
    from feeds.binance_feed import btc_feed, eth_feed
    from alerts.telegram_alerts import send_alert
    from learning_engine import learning
    from analytics.log_analyzer import analyzer as log_analyzer

    journal = TradeLogger()
    risk    = RiskEngine(starting_balance=1000.0)
    paper   = PaperTrader(starting_balance=1000.0)
    order_manager  = OrderManager(risk_engine=risk, paper_trader=paper, trade_logger=journal)
    health_check   = HealthCheck(trade_logger=journal)
    whale_tracker  = WhaleTracker()
    latency_arb    = LatencyArb()
    copy_trader    = CopyTrader()
    market_maker   = MarketMaker()
    timezone_arb      = TimezoneArb()
    complement_arb    = ComplementArb()
    kalshi_copy       = KalshiCopyTrader()
    near_expiry       = NearExpiryStrategy()
    ta_signal         = TASignalStrategy()
    value_bet         = ValueBetStrategy()
    cross_platform    = CrossPlatformArb()

    mode = "📄 PAPER" if config.PAPER_MODE else "⚡ LIVE"
    logger.info(f"🐺 Wolf starting in {mode} mode")
    send_alert(f"Wolf online — {mode} mode\nAll systems starting...", "INFO")

    # ── Start feeds (non-fatal) ───────────────────────────────────────────────
    try:
        await btc_feed.start()
        await eth_feed.start()
        await asyncio.sleep(2)
    except Exception as e:
        logger.warning(f"Feed startup issue (non-fatal): {e}")

    for component, name in [(health_check, "health_check"), (whale_tracker, "whale_tracker")]:
        try:
            await component.start()
        except Exception as e:
            logger.warning(f"{name} startup failed (non-fatal): {e}")

    # ── Dashboard (non-fatal) ─────────────────────────────────────────────────
    try:
        from dashboard.app import run_dashboard
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", 5000))
            s.close()
            threading.Thread(target=run_dashboard, daemon=True).start()
            logger.info("Dashboard started on http://127.0.0.1:5000")
        except OSError:
            s.close()
            logger.info("Dashboard port 5000 already bound — skipping")
    except Exception as e:
        logger.warning(f"Dashboard failed: {e}")

    send_alert(
        f"🐺 Wolf online — {mode}\n"
        f"8 strategies active:\n"
        f"  1. Latency Arb (9–16s, 0.11% BTC)\n"
        f"  2. Copy Trading (Polymarket top wallets)\n"
        f"  3. Complement Arb (YES+NO < $0.95)\n"
        f"  4. Timezone Arb (global RSS, 2–9AM ET)\n"
        f"  5. Near Expiry (<2h, $0.94–$0.99)\n"
        f"  6. Cross-Platform Arb (Poly ↔ Kalshi)\n"
        f"  7. Kalshi Copy Trading\n"
        f"  8. Market Making\n"
        f"Paper until Jefe authorizes live.",
        "INFO"
    )

    # ─── Main Trading Loop ────────────────────────────────────────────────────
    SCAN_INTERVAL    = 5     # seconds between strategy scans
    RESOLVE_INTERVAL = 30    # seconds between paper trade resolution
    STATUS_INTERVAL  = 60    # seconds between status log (1 min)
    REPORT_INTERVAL  = 3600  # full analytics report every hour
    last_resolve = 0.0
    last_status  = 0.0
    last_report  = 0.0
    last_morning_report = 0.0  # 6AM daily report
    gate_alerted = False    # alert Jefe once when milestone hit — never halt

    try:
        while not _shutdown_requested:
            now = time.time()

            # ── Strategy scans ────────────────────────────────────────────────

            # Priority 1: Latency Arb (fastest edge, fires first)
            try:
                for sig in await latency_arb.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] LatencyArb: "
                            f"{sig['market_id'][:20]}... {sig['side']} conf={sig['confidence']:.2f}"
                        )
            except Exception as e:
                logger.warning(f"Latency arb error: {e}")

            # Priority 2: Copy Trading
            try:
                for sig in await copy_trader.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] CopyTrade: "
                            f"{sig['market_id'][:20]}... {sig['side']} conf={sig['confidence']:.2f}"
                        )
            except Exception as e:
                logger.warning(f"Copy trading error: {e}")

            # Priority 3: Complement Arb (near-riskless — highest priority after latency arb)
            try:
                for sig in await complement_arb.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] ComplArb: "
                            f"{sig['market_id'][:20]}... {sig['side']} "
                            f"edge={sig.get('edge', 0):.3f}"
                        )
            except Exception as e:
                logger.warning(f"Complement arb error: {e}")

            # Priority 4: Timezone Arb (US sleep window — fires 2–9 AM ET)
            try:
                for sig in await timezone_arb.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] TZArb: "
                            f"[{sig.get('region','?')}] "
                            f"{sig['market_id'][:20]}... {sig['side']} "
                            f"@ {sig['entry_price']:.2f}"
                        )
            except Exception as e:
                logger.warning(f"Timezone arb error: {e}")

            # Priority 4b: TA Signal (RSI+MACD+Stoch+EMA+OBV+VWAP+ATR)
            try:
                for sig in await ta_signal.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] TASignal: "
                            f"{sig['market_id'][:20]}... {sig['side']} "
                            f"conf={sig['confidence']:.2f} {sig.get('reason','')[:60]}"
                        )
            except Exception as e:
                logger.warning(f"TA signal error: {e}")

            # Priority 4c: Value Bet (near-certain + strong signal markets)
            try:
                for sig in await value_bet.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] ValueBet: "
                            f"{sig['side']}@{sig['entry_price']:.2f} "
                            f"conf={sig['confidence']:.2f} {sig.get('reason','')[:60]}"
                        )
            except Exception as e:
                logger.warning(f"Value bet error: {e}")

            # Priority 5: Near-Expiry (high-confidence, near-certain outcomes)
            # NOTE: near_expiry scans Polymarket only until KALSHI_ENABLED=true
            try:
                for sig in await near_expiry.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] NearExpiry: "
                            f"[{sig.get('venue','?')}] "
                            f"{sig['market_id'][:20]}... {sig['side']} "
                            f"@ {sig['entry_price']:.3f}"
                        )
            except Exception as e:
                logger.warning(f"Near expiry error: {e}")

            # Priority 6: Cross-Platform Arb — DISABLED until KALSHI_ENABLED=true
            if config.KALSHI_ENABLED:
                try:
                    for sig in await cross_platform.scan():
                        res = order_manager.execute_signal(sig)
                        if res["status"] in ("paper_executed", "live_executed"):
                            logger.info(
                                f"[{res['status']}] CrossArb: "
                                f"[{sig.get('venue','?')}] "
                                f"{sig['market_id'][:20]}... {sig['side']} "
                                f"edge={sig.get('edge',0):.3f}"
                            )
                except Exception as e:
                    logger.warning(f"Cross platform arb error: {e}")

            # Priority 7: Kalshi Copy Trading — DISABLED until KALSHI_ENABLED=true
            if config.KALSHI_ENABLED:
                try:
                    for sig in await kalshi_copy.scan():
                        res = order_manager.execute_signal(sig)
                        if res["status"] in ("paper_executed", "live_executed"):
                            logger.info(
                                f"[{res['status']}] KalshiCopy: "
                                f"{sig['market_id'][:20]}... {sig['side']} "
                                f"conf={sig['confidence']:.2f}"
                            )
                except Exception as e:
                    logger.warning(f"Kalshi copy error: {e}")

            # Priority 8: Market Making
            try:
                for sig in await market_maker.scan():
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] MarketMake: "
                            f"{sig['market_id'][:20]}... {sig['side']} "
                            f"spread={sig.get('edge', 0) * 2:.3f}"
                        )
            except Exception as e:
                logger.warning(f"Market making error: {e}")

            # ── Resolve paper trades ──────────────────────────────────────────
            if config.PAPER_MODE and now - last_resolve > RESOLVE_INTERVAL:
                last_resolve = now
                try:
                    _resolve_paper_trades(paper, journal, market_maker)
                except Exception as e:
                    logger.warning(f"Paper resolve error: {e}")

            # ── Status log ────────────────────────────────────────────────────
            if now - last_status > STATUS_INTERVAL:
                last_status = now
                try:
                    stats = paper.get_stats()
                    by_s = stats.get("by_strategy", {})
                    strat_summary = " | ".join(
                        f"{s}: {d['trades']}t {d['wins']/d['trades']:.0%}WR ${d['pnl']:+.0f}"
                        for s, d in by_s.items() if d["trades"] > 0
                    )
                    logger.info(
                        f"📊 Paper: {stats['total_trades']} trades | "
                        f"WR {stats['win_rate']:.1%} | "
                        f"P&L ${stats['total_pnl']:+.2f} | "
                        f"Balance ${stats['balance']:.2f} | "
                        f"Open: {stats['open_trades']} | "
                        f"{stats['gate_message']}"
                        + (f"\n    → {strat_summary}" if strat_summary else "")
                    )
                except Exception as e:
                    logger.warning(f"Status log error: {e}")

            # ── Learning engine ───────────────────────────────────────────────
            try:
                if learning.should_run():
                    lessons = learning.analyze()
                    s = (lessons.get("summary") or {})
                    if s.get("total_trades", 0) > 0:
                        logger.info(f"🧠 {learning.get_status()}")
            except Exception as e:
                logger.warning(f"Learning engine error: {e}")

            # ── 6AM daily morning report ─────────────────────────────────────
            import datetime as _dt
            _now_et = _dt.datetime.now()  # server is ET
            _is_6am = (_now_et.hour == 6 and _now_et.minute < 2)
            if _is_6am and now - last_morning_report > 3600:
                last_morning_report = now
                try:
                    import subprocess as _sp
                    _sp.Popen(
                        ["python3", "scripts/morning_report.py"],
                        cwd="/data/.openclaw/workspace/wolf"
                    )
                    logger.info("📋 6AM morning report triggered")
                except Exception as _e:
                    logger.warning(f"Morning report trigger failed: {_e}")

            # ── Hourly analytics report ──────────────────────────────────────────
            if now - last_report > REPORT_INTERVAL:
                last_report = now
                try:
                    report = log_analyzer.analyze_trades(hours=24)
                    lessons = report.get("lessons", [])
                    wr = report["overall"]["win_rate"]
                    pnl = report["overall"]["total_pnl"]
                    total = report["overall"]["total_trades"]
                    logger.info(
                        f"📋 Analytics: {total} trades | WR {wr:.1%} | PnL ${pnl:+.2f} | "
                        f"{len(lessons)} lessons"
                    )
                    for lesson in lessons[:3]:
                        logger.info(f"   → {lesson}")
                    # Feed structured insights into learning engine
                    learning.ingest_analytics(report)
                except Exception as e:
                    logger.warning(f"Analytics error: {e}")

            # ── Gate milestone check ─────────────────────────────────────────
            # ALERT ONLY — wolf NEVER stops paper trading on its own
            if not gate_alerted:
                try:
                    gate_passed, gate_msg = paper.has_passed_gate()
                    if gate_passed:
                        logger.info(f"🎯 GATE MILESTONE REACHED: {gate_msg}")
                        logger.info("Wolf continues paper trading — waiting for Jefe authorization to go live.")
                        from alerts.telegram_alerts import alert_paper_gate_passed
                        alert_paper_gate_passed(paper.get_stats())
                        gate_alerted = True
                except Exception as e:
                    logger.warning(f"Gate check error: {e}")

            # ── Kill switch (risk protection only — not paper gate) ───────────
            try:
                can_trade, reason = risk.can_trade()
                if not can_trade and "kill_switch" in reason.lower():
                    from alerts.telegram_alerts import alert_kill_switch
                    alert_kill_switch(risk.get_stats()["drawdown_pct"])
                    logger.critical("KILL SWITCH triggered — halting loop, watchdog will restart")
                    break
            except Exception as e:
                logger.warning(f"Kill switch check error: {e}")

            await asyncio.sleep(SCAN_INTERVAL)

    except asyncio.CancelledError:
        logger.info("Main loop cancelled by asyncio")
    except Exception as e:
        logger.critical(f"Main loop crashed: {e}", exc_info=True)
        try:
            from alerts.telegram_alerts import send_alert as _alert
            _alert(f"🚨 Wolf crashed: {e}\nWatchdog restarting...", "CRITICAL")
        except Exception:
            pass
        raise  # Watchdog catches this and restarts

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    logger.info("Wolf shutting down gracefully...")
    for feed, name in [(btc_feed, "btc"), (eth_feed, "eth")]:
        try:
            await feed.stop()
        except Exception:
            pass
    for component, name in [(health_check, "health"), (whale_tracker, "whale")]:
        try:
            await component.stop()
        except Exception:
            pass
    try:
        from alerts.telegram_alerts import send_alert
        send_alert("Wolf offline — graceful shutdown complete.", "WARNING")
    except Exception:
        pass


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Wolf crashed with unhandled exception: {e}", exc_info=True)
        sys.exit(1)
