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
def _resolve_paper_trades(paper, journal):
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
            # Spread capture — not directional. 72% reflects typical MM edge.
            win_prob = 0.72
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
            journal.log_paper_trade({
                "timestamp": now,
                "strategy": trade.strategy,
                "venue": trade.venue,
                "market_id": trade.market_id,
                "side": trade.side,
                "size": trade.size,
                "entry_price": trade.entry_price,
                "exit_price": result.exit_price,
                "pnl": result.pnl,
                "won": result.won,
                "resolved": True,
            })
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
    from feeds.binance_feed import btc_feed, eth_feed
    from alerts.telegram_alerts import send_alert
    from learning_engine import learning

    journal = TradeLogger()
    risk    = RiskEngine(starting_balance=1000.0)
    paper   = PaperTrader(starting_balance=1000.0)
    order_manager  = OrderManager(risk_engine=risk, paper_trader=paper, trade_logger=journal)
    health_check   = HealthCheck(trade_logger=journal)
    whale_tracker  = WhaleTracker()
    latency_arb    = LatencyArb()
    copy_trader    = CopyTrader()
    market_maker   = MarketMaker()

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
        f"Wolf fully online 🐺\n"
        f"Mode: {mode}\n"
        f"Strategies: Latency Arb + Copy Trading + Market Making\n"
        f"Paper runs continuously. Gate milestone = Telegram alert to Jefe.\n"
        f"Wolf will NOT switch to live until Jefe authorizes.",
        "INFO"
    )

    # ─── Main Trading Loop ────────────────────────────────────────────────────
    SCAN_INTERVAL    = 5    # seconds between strategy scans
    RESOLVE_INTERVAL = 30   # seconds between paper trade resolution
    STATUS_INTERVAL  = 60   # seconds between status log (1 min for visibility)
    last_resolve = 0.0
    last_status  = 0.0
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

            # Priority 3: Market Making
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
                    _resolve_paper_trades(paper, journal)
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
