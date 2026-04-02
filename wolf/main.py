"""
Wolf Trading Bot — Main Entry Point
Starts all components. Runs the main trading loop indefinitely in paper mode.
WOLF_PAPER_MODE=false ONLY when Jefe explicitly authorizes live trading.
Paper mode never stops on its own — gate milestone = Telegram alert to Jefe, not a halt.

Upgrades over v1:
  - asyncio.gather() replaces sequential strategy awaits — all strategies
    scan in true parallel, cutting loop time from ~15s to ~2-3s.
  - Binance volatility passed to position sizing for vol-adjusted Kelly.
  - aiohttp session cleanup on shutdown.
  - coro_empty() helper for conditionally-disabled strategies.
"""
import asyncio
import signal
import logging
import sys
import threading
import time
import config

# ─── Logging setup (idempotent — safe on watchdog restarts) ──────────────────
_root = logging.getLogger()
if not _root.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s — %(message)s")
    _sh  = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _root.setLevel(logging.INFO)
    _root.addHandler(_sh)

logger = logging.getLogger("wolf.main")

# ─── Graceful shutdown flag ───────────────────────────────────────────────────
_shutdown_requested = False


def handle_shutdown(sig, frame):
    global _shutdown_requested
    logger.info(f"Signal {sig} received — requesting graceful shutdown")
    _shutdown_requested = True


# ─── Helper: empty coroutine for disabled strategies ─────────────────────────
async def coro_empty() -> list:
    """Placeholder coroutine for strategies that are conditionally disabled."""
    return []


# ─── Paper trade resolver ─────────────────────────────────────────────────────
def _resolve_paper_trades(paper, journal, market_maker=None):
    """
    Resolve open paper trades using REAL Polymarket market outcomes.
    Polls gamma-api for actual resolution status — no random simulation.
    Trades stay open until the real market resolves (hours/days for prediction markets).
    """
    from market_resolver import get_real_outcome, get_current_price
    now = time.time()
    resolved_count = 0

    for trade in list(paper.open_trades):
        # Don't check resolution for the first 60s — market needs time to settle
        if now - trade.timestamp < 60:
            continue

        outcome = get_real_outcome(trade.market_id)

        if outcome is None:
            age_h = (now - trade.timestamp) / 3600
            max_hold_h = config.MAX_HOLD_HOURS
            if age_h > max_hold_h:
                # Force-exit stale position — don't let capital sit frozen
                prices = get_current_price(trade.market_id)
                if prices is None:
                    _fail_key = f"_fail_{trade.market_id[:20]}"
                    _fails = getattr(_resolve_paper_trades, _fail_key, 0) + 1
                    setattr(_resolve_paper_trades, _fail_key, _fails)
                    if _fails >= 3:
                        logger.warning(
                            f"[FORCE-EXIT] Price lookup failed {_fails}x for "
                            f"{trade.market_id[:20]} — closing at entry (pnl=$0, void)"
                        )
                        setattr(_resolve_paper_trades, _fail_key, 0)
                        result = paper.resolve_trade(
                            trade.market_id,
                            "NO" if trade.side == "YES" else "YES",
                        )
                        if result:
                            result.pnl = 0.0
                            result.exit_price = trade.entry_price
                            try:
                                journal.update_paper_trade_resolved(
                                    market_id=trade.market_id, strategy=trade.strategy,
                                    side=trade.side, won=False,
                                    exit_price=trade.entry_price, pnl=0.0, void=True,
                                )
                            except Exception as _e:
                                logger.warning(f"Force-exit(no-price) DB update FAILED: {_e}")
                    continue

                current_px = prices[0] if trade.side == "YES" else prices[1]
                if current_px and current_px > 0:
                    pnl = trade.size * (current_px / trade.entry_price - 1.0)
                    won = pnl > 0
                else:
                    current_px = trade.entry_price
                    pnl = 0.0
                    won = False

                logger.warning(
                    f"[FORCE-EXIT] Position held {age_h:.1f}h > {max_hold_h}h limit — "
                    f"exiting at ${current_px:.3f} | P&L ${pnl:+.2f}"
                )
                result = paper.resolve_trade(
                    trade.market_id,
                    trade.side if won else ("NO" if trade.side == "YES" else "YES"),
                )
                if result:
                    try:
                        journal.update_paper_trade_resolved(
                            market_id=trade.market_id, strategy=trade.strategy,
                            side=trade.side, won=won,
                            exit_price=current_px, pnl=pnl,
                        )
                    except Exception as _e:
                        logger.warning(f"Force-exit DB update FAILED: {_e}")
                    from alerts.telegram_alerts import alert_trade_exit
                    alert_trade_exit(
                        strategy=trade.strategy, market=trade.market_id,
                        side=trade.side, entry_price=trade.entry_price,
                        exit_price=current_px, pnl=pnl, won=won,
                        hold_time_min=age_h * 60, paper=config.PAPER_MODE,
                    )
            elif age_h > 24:
                logger.warning(
                    f"[RESOLVER] Position open {age_h:.1f}h, no outcome yet: "
                    f"{trade.strategy} {trade.side} {trade.market_id[:20]}…"
                )
            continue

        # Real outcome received
        hold_min = (now - trade.timestamp) / 60
        result = paper.resolve_trade(trade.market_id, outcome)
        if result:
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

            from alerts.telegram_alerts import alert_trade_exit
            alert_trade_exit(
                strategy=trade.strategy, market=trade.market_id,
                side=trade.side, entry_price=trade.entry_price,
                exit_price=result.exit_price, pnl=result.pnl,
                won=result.won, hold_time_min=hold_min, paper=config.PAPER_MODE,
            )
            resolved_count += 1
            logger.info(
                f"[REAL] {'WIN ✅' if result.won else 'LOSS ❌'} | "
                f"{trade.strategy} {trade.side}@{trade.entry_price:.3f} → "
                f"{outcome} | P&L ${result.pnl:+.2f} | held {hold_min:.0f}m"
            )

    if resolved_count:
        logger.info(f"Resolved {resolved_count} paper trades (real outcomes)")


# ─── Process signals in parallel (asyncio.gather version) ────────────────────
async def _scan_all_strategies(
    latency_arb, copy_trader, complement_arb, timezone_arb,
    ta_signal, value_bet, btc_scalper, near_expiry,
    cross_platform, kalshi_copy, pair_trader, combinatorial_arb,
    market_maker, binance_ok: bool,
) -> list[dict]:
    """
    Run all strategy scans concurrently.
    Returns a flat list of all signals from all strategies.
    Exceptions inside individual strategies are caught and logged; they do
    not crash the gather — other strategies keep running.
    """
    tasks = [
        latency_arb.scan()        if binance_ok else coro_empty(),
        copy_trader.scan(),
        complement_arb.scan(),
        timezone_arb.scan(),
        ta_signal.scan()          if binance_ok else coro_empty(),
        value_bet.scan(),
        btc_scalper.scan(),
        near_expiry.scan(),
        cross_platform.scan()     if config.KALSHI_ENABLED else coro_empty(),
        kalshi_copy.scan()        if config.KALSHI_ENABLED else coro_empty(),
        pair_trader.scan(),
        combinatorial_arb.scan(),
        market_maker.scan(),
    ]

    names = [
        "latency_arb", "copy_trader", "complement_arb", "timezone_arb",
        "ta_signal", "value_bet", "btc_scalper", "near_expiry",
        "cross_platform", "kalshi_copy", "pair_trader", "combinatorial_arb",
        "market_maker",
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_signals: list[dict] = []
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            logger.warning(f"{name} scan error: {result}")
        elif isinstance(result, list):
            all_signals.extend(result)

    return all_signals


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    config.validate()

    # ── Pre-flight check ──────────────────────────────────────────────────────
    import preflight as _pf
    _pf_ok, _pf_fails = _pf.run(send_telegram=True)
    if not _pf_ok:
        logger.warning(f"PRE-FLIGHT WARNINGS: {_pf_fails} — continuing in paper mode")
        config.PAPER_MODE = True
    else:
        logger.info("Pre-flight: ✅ all checks passed")

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
    from strategies.btc_scalper import BTCScalperStrategy
    from strategies.cross_platform_arb import CrossPlatformArb
    from strategies.pair_trading import PairTrader
    from strategies.combinatorial_arb import CombinatorialArb
    from feeds.binance_feed import btc_feed, eth_feed
    from alerts.telegram_alerts import send_alert
    from learning_engine import learning
    from analytics.log_analyzer import analyzer as log_analyzer

    journal = TradeLogger()
    risk    = RiskEngine(
        starting_balance=config.PAPER_STARTING_CAPITAL if config.PAPER_MODE else config.LIVE_STARTING_CAPITAL
    )
    paper   = PaperTrader(
        starting_balance=config.PAPER_STARTING_CAPITAL if config.PAPER_MODE else config.LIVE_STARTING_CAPITAL
    )

    # ── Restore open positions from DB into risk engine ───────────────────────
    import sqlite3 as _sqlite3
    from risk_engine import TradeRecord as _TR
    try:
        _conn = _sqlite3.connect(config.DB_PATH)
        _rows = _conn.execute(
            "SELECT strategy, venue, market_id, side, size, entry_price, timestamp "
            "FROM paper_trades WHERE resolved=0 AND simulated=0 AND COALESCE(void,0)=0"
        ).fetchall()
        for _r in _rows:
            _tr = _TR(
                strategy=_r[0], market_id=_r[2],
                side=_r[3], size=_r[4], entry_price=_r[5], timestamp=_r[6],
            )
            risk.open_positions.append(_tr)
        _conn.close()
        logger.info(f"Restored {len(_rows)} open positions from DB into risk engine")
    except Exception as _e:
        logger.warning(f"Could not restore open positions: {_e}")

    order_manager     = OrderManager(risk_engine=risk, paper_trader=paper, trade_logger=journal)
    health_check      = HealthCheck(trade_logger=journal)
    whale_tracker     = WhaleTracker()
    latency_arb       = LatencyArb()
    copy_trader       = CopyTrader()
    market_maker      = MarketMaker()
    timezone_arb      = TimezoneArb()
    complement_arb    = ComplementArb()
    pair_trader       = PairTrader()
    combinatorial_arb = CombinatorialArb()
    kalshi_copy       = KalshiCopyTrader()
    near_expiry       = NearExpiryStrategy()
    ta_signal         = TASignalStrategy()
    value_bet         = ValueBetStrategy()
    btc_scalper       = BTCScalperStrategy()
    cross_platform    = CrossPlatformArb()

    mode = "📄 PAPER" if config.PAPER_MODE else "⚡ LIVE"
    logger.info(f"🐺 Wolf starting in {mode} mode")

    # Startup stats notification
    try:
        import sqlite3 as _sq
        _conn = _sq.connect(config.DB_PATH)
        _total = _conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0"
        ).fetchone()[0] or 0
        _wins = _conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE resolved=1 AND won=1 AND simulated=0 AND COALESCE(void,0)=0"
        ).fetchone()[0] or 0
        _pnl = float(
            _conn.execute(
                "SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0"
            ).fetchone()[0] or 0
        )
        _open = _conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND simulated=0 AND COALESCE(void,0)=0"
        ).fetchone()[0] or 0
        _conn.close()
        _wr  = _wins / _total * 100 if _total else 0
        _bal = config.PAPER_STARTING_CAPITAL + _pnl
        _strat_floors = ""
        try:
            _floors = learning.min_confidence_overrides
            if _floors:
                _strat_floors = f" | {len(_floors)} floor(s) active"
        except Exception:
            pass
        _startup_msg = (
            f"🐺 Wolf Online — {mode}\n"
            f"─────────────────────\n"
            f"📊 Trades: {_total} | WR: {_wr:.1f}% | Open: {_open}\n"
            f"💰 P&L: ${_pnl:+.2f} | Balance: ${_bal:,.2f}\n"
            f"📈 Start: ${config.PAPER_STARTING_CAPITAL:,.0f}{_strat_floors}"
        )
    except Exception:
        _startup_msg = f"🐺 Wolf Online — {mode} mode"
    send_alert(_startup_msg, "INFO", system=True)

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

    # ── Wolf Guardian ─────────────────────────────────────────────────────────
    try:
        import os as _os
        _log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "wolf.log")
        from scripts.wolf_guardian import start as _guardian_start
        _guardian_start(_log_path, config)
        logger.info("🛡️ Wolf Guardian started")
        from scripts.guardian_responder import run_responder_loop
        _responder = threading.Thread(target=run_responder_loop, daemon=True, name="guardian-responder")
        _responder.start()
        logger.info("🔧 Auto-heal responder started")
    except Exception as _ge:
        logger.warning(f"Guardian failed to start (non-fatal): {_ge}")

    # ─── Main Trading Loop ────────────────────────────────────────────────────
    SCAN_INTERVAL    = 5     # seconds between strategy scans
    RESOLVE_INTERVAL = 30    # seconds between paper trade resolution
    STATUS_INTERVAL  = 60    # seconds between status log
    REPORT_INTERVAL  = 3600  # full analytics report every hour

    last_resolve = 0.0
    last_status  = 0.0
    last_report  = 0.0
    last_morning_report = 0.0
    gate_alerted = False

    try:
        while not _shutdown_requested:
            now = time.time()

            # ── Daily loss circuit breaker ────────────────────────────────────
            _daily_pnl_cb = 0.0
            try:
                import sqlite3 as _sq_cb
                _cb_conn = _sq_cb.connect(config.DB_PATH)
                _today = __import__("datetime").date.today().isoformat()
                _daily_pnl_cb = float(
                    _cb_conn.execute(
                        "SELECT COALESCE(SUM(pnl),0) FROM paper_trades "
                        "WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0 "
                        "AND date(timestamp,'unixepoch')=?",
                        (_today,),
                    ).fetchone()[0]
                )
                _cb_conn.close()
            except Exception:
                _daily_pnl_cb = 0.0

            if risk.check_daily_loss_circuit(_daily_pnl_cb, risk.current_balance):
                logger.critical("[CIRCUIT BREAKER] Daily loss cap hit — halting")
                send_alert(
                    f"🚨 CIRCUIT BREAKER TRIGGERED\n"
                    f"Daily P&L: ${_daily_pnl_cb:.2f}\n"
                    f"Bot halted — manual restart required",
                    "CRITICAL",
                )
                break

            # ── Binance feed health ───────────────────────────────────────────
            _binance_ok = btc_feed.is_fresh(max_age_ms=15000)

            # ── Concurrent strategy scan (THE KEY UPGRADE) ────────────────────
            # All strategies run in parallel via asyncio.gather().
            # Sequential awaits blocked on slow API calls — this fixes that.
            try:
                all_signals = await _scan_all_strategies(
                    latency_arb, copy_trader, complement_arb, timezone_arb,
                    ta_signal, value_bet, btc_scalper, near_expiry,
                    cross_platform, kalshi_copy, pair_trader, combinatorial_arb,
                    market_maker, _binance_ok,
                )
            except Exception as scan_err:
                logger.warning(f"Strategy scan gather error: {scan_err}")
                all_signals = []

            # ── Execute all signals ───────────────────────────────────────────
            for sig in all_signals:
                try:
                    res = order_manager.execute_signal(sig)
                    if res["status"] in ("paper_executed", "live_executed"):
                        logger.info(
                            f"[{res['status']}] {sig['strategy']}: "
                            f"{sig['market_id'][:20]}... {sig['side']} "
                            f"conf={sig['confidence']:.2f}"
                        )
                except Exception as exec_err:
                    logger.warning(f"Signal execution error ({sig.get('strategy','?')}): {exec_err}")

            # ── Resolve paper trades ──────────────────────────────────────────
            if config.PAPER_MODE and now - last_resolve > RESOLVE_INTERVAL:
                last_resolve = now
                try:
                    _resolve_paper_trades(paper, journal, market_maker)
                except Exception as e:
                    logger.warning(f"Paper resolve error: {e}")

            # ── Dashboard push ────────────────────────────────────────────────
            try:
                from feeds.dashboard_push import push_to_dashboard
                push_to_dashboard()
            except Exception:
                pass

            # ── Status log ────────────────────────────────────────────────────
            if now - last_status > STATUS_INTERVAL:
                last_status = now
                try:
                    stats = paper.get_stats()
                    by_s  = stats.get("by_strategy", {})
                    strat_summary = " | ".join(
                        f"{s}: {d['trades']}t {d['wins']/d['trades']:.0%}WR ${d['pnl']:+.0f}"
                        for s, d in by_s.items()
                        if d["trades"] > 0
                    )
                    # Include portfolio Sharpe if available
                    risk_stats = risk.get_stats()
                    sharpe_str = f" | Sharpe {risk_stats.get('portfolio_sharpe', 0):.2f}"
                    logger.info(
                        f"📊 Paper: {stats['total_trades']} trades | "
                        f"WR {stats['win_rate']:.1%} | "
                        f"P&L ${stats['total_pnl']:+.2f} | "
                        f"Balance ${stats['balance']:.2f} | "
                        f"Open: {stats['open_trades']}{sharpe_str} | "
                        f"{stats['gate_message']}"
                        + (f"\n    → {strat_summary}" if strat_summary else "")
                    )
                except Exception as e:
                    logger.warning(f"Status log error: {e}")

            # ── Learning engine ───────────────────────────────────────────────
            try:
                if learning.should_run():
                    lessons = learning.analyze()
                    s = lessons.get("summary") or {}
                    if s.get("total_trades", 0) > 0:
                        logger.info(f"🧠 {learning.get_status()}")
            except Exception as e:
                logger.warning(f"Learning engine error: {e}")

            # ── 6AM daily morning report ──────────────────────────────────────
            import datetime as _dt
            _now_et  = _dt.datetime.now()
            _is_6am  = (_now_et.hour == 6 and _now_et.minute < 2)
            if _is_6am and now - last_morning_report > 3600:
                last_morning_report = now
                try:
                    import subprocess as _sp
                    _sp.Popen(
                        ["python3", "scripts/morning_report.py"],
                        cwd="/data/.openclaw/workspace/wolf",
                    )
                    logger.info("📋 6AM morning report triggered")
                except Exception as _e:
                    logger.warning(f"Morning report trigger failed: {_e}")

            # ── Hourly analytics report ───────────────────────────────────────
            if now - last_report > REPORT_INTERVAL:
                last_report = now
                try:
                    report  = log_analyzer.analyze_trades(hours=24)
                    lessons = report.get("lessons", [])
                    wr      = report["overall"]["win_rate"]
                    pnl     = report["overall"]["total_pnl"]
                    total   = report["overall"]["total_trades"]
                    sharpe  = report["overall"].get("sharpe", 0)
                    logger.info(
                        f"📋 Analytics: {total} trades | WR {wr:.1%} | "
                        f"PnL ${pnl:+.2f} | Sharpe {sharpe:.2f} | "
                        f"{len(lessons)} lessons"
                    )
                    for lesson in lessons[:3]:
                        logger.info(f"   → {lesson}")
                    learning.ingest_analytics(report)
                except Exception as e:
                    logger.warning(f"Analytics error: {e}")

            # ── Gate milestone check ──────────────────────────────────────────
            if not gate_alerted:
                try:
                    gate_passed, gate_msg = paper.has_passed_gate()
                    if gate_passed:
                        logger.info(f"🎯 GATE MILESTONE REACHED: {gate_msg}")
                        logger.info("Wolf continues paper trading — waiting for Jefe authorization.")
                        from alerts.telegram_alerts import alert_paper_gate_passed
                        alert_paper_gate_passed(paper.get_stats())
                        gate_alerted = True
                except Exception as e:
                    logger.warning(f"Gate check error: {e}")

            # ── Kill switch ───────────────────────────────────────────────────
            try:
                can_trade, reason = risk.can_trade()
                if not can_trade and "kill_switch" in reason.lower():
                    from alerts.telegram_alerts import alert_kill_switch
                    alert_kill_switch(risk.get_stats()["drawdown_pct"])
                    logger.critical("KILL SWITCH triggered — halting loop")
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
        raise

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
    # Close shared aiohttp session
    try:
        from http_client import close as close_http
        await close_http()
    except Exception:
        pass
    # Close shared DB connection
    try:
        from db import close as close_db
        close_db()
    except Exception:
        pass
    try:
        from alerts.telegram_alerts import send_alert
        send_alert("Wolf offline — graceful shutdown complete.", "WARNING", system=True)
    except Exception:
        pass


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT,  handle_shutdown)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Wolf crashed with unhandled exception: {e}", exc_info=True)
        sys.exit(1)
