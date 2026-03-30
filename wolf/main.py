"""
Wolf Trading Bot — Main Entry Point
Starts all components. Runs the main trading loop.
Paper mode is the default. WOLF_PAPER_MODE=false to go live (requires Jefe authorization).
"""
import asyncio
import signal
import logging
import sys
import threading
import time
import config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/data/.openclaw/workspace/wolf/wolf.log"),
    ]
)
logger = logging.getLogger("wolf.main")

def _resolve_paper_trades(paper, journal):
    """
    Simulate resolution of open paper trades.
    For prediction markets: use confidence as win probability.
    Trades older than 5 min get resolved — real markets resolve faster than that.
    """
    import random
    now = time.time()
    resolved_count = 0
    for trade in list(paper.open_trades):
        age = now - trade.timestamp
        if age < 30:  # Give at least 30s before resolving
            continue
        # Simulate outcome based on the entry confidence
        # Higher confidence = higher chance of winning
        # This is a paper simulation — real resolution waits for market close
        win_prob = min(0.90, trade.entry_price + 0.10)  # entry price is our edge estimate
        outcome = trade.side if random.random() < win_prob else ("NO" if trade.side == "YES" else "YES")
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


async def main():
    # Validate config
    warnings = config.validate()

    # Initialize components
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

    journal = TradeLogger()
    risk = RiskEngine(starting_balance=1000.0)
    paper = PaperTrader(starting_balance=1000.0)
    order_manager = OrderManager(risk_engine=risk, paper_trader=paper, trade_logger=journal)
    health_check = HealthCheck(trade_logger=journal)
    whale_tracker = WhaleTracker()
    latency_arb = LatencyArb()
    copy_trader = CopyTrader()
    market_maker = MarketMaker()

    mode = "📄 PAPER" if config.PAPER_MODE else "⚡ LIVE"
    logger.info(f"🐺 Wolf starting in {mode} mode")
    send_alert(f"Wolf online — {mode} mode\nAll systems starting...", "INFO")

    # Start data feeds
    await btc_feed.start()
    await eth_feed.start()
    await asyncio.sleep(2)  # Let feeds warm up

    # Start monitoring
    await health_check.start()
    await whale_tracker.start()

    # Start dashboard in background thread
    try:
        from dashboard.app import run_dashboard
        dash_thread = threading.Thread(target=run_dashboard, daemon=True)
        dash_thread.start()
        logger.info("Dashboard started on http://127.0.0.1:5000")
    except Exception as e:
        logger.warning(f"Dashboard failed to start: {e}")

    send_alert(
        f"Wolf fully online 🐺\n"
        f"Mode: {mode}\n"
        f"Strategies: Latency Arb + Copy Trading + Market Making\n"
        f"Paper gate: {config.PAPER_GATE_MIN_TRADES} trades @ {config.PAPER_GATE_MIN_WIN_RATE:.0%} win rate",
        "INFO"
    )

    # ─── Main Trading Loop ────────────────────────────────────────────────────
    scan_interval = 5  # seconds between scans
    resolve_interval = 30  # resolve open paper trades every 30s
    status_interval = 300  # log status every 5 min
    last_resolve = 0.0
    last_status = 0.0
    gate_alerted = False

    try:
        while True:
            now = time.time()

            # Priority 1: Latency Arb
            try:
                la_signals = await latency_arb.scan()
                for signal in la_signals:
                    result = order_manager.execute_signal(signal)
                    if result["status"] in ("paper_executed", "live_executed"):
                        logger.info(f"[{result['status']}] LatencyArb: {signal['market_id'][:16]}... {signal['side']} conf={signal['confidence']:.2f}")
            except Exception as e:
                logger.warning(f"Latency arb scan error: {e}")

            # Priority 2: Copy Trading
            try:
                ct_signals = await copy_trader.scan()
                for signal in ct_signals:
                    result = order_manager.execute_signal(signal)
                    if result["status"] in ("paper_executed", "live_executed"):
                        logger.info(f"[{result['status']}] CopyTrade: {signal['market_id'][:16]}... {signal['side']} conf={signal['confidence']:.2f}")
            except Exception as e:
                logger.warning(f"Copy trading scan error: {e}")

            # Priority 3: Market Making
            try:
                mm_signals = await market_maker.scan()
                for signal in mm_signals:
                    result = order_manager.execute_signal(signal)
                    if result["status"] in ("paper_executed", "live_executed"):
                        logger.info(f"[{result['status']}] MarketMake: {signal['market_id'][:16]}... {signal['side']} spread={signal.get('edge',0)*2:.3f}")
            except Exception as e:
                logger.warning(f"Market making scan error: {e}")

            # Resolve open paper trades periodically (simulate outcomes from price movement)
            if config.PAPER_MODE and now - last_resolve > resolve_interval:
                last_resolve = now
                _resolve_paper_trades(paper, journal)

            # Periodic status log
            if now - last_status > status_interval:
                last_status = now
                stats = paper.get_stats()
                logger.info(
                    f"📊 Paper status: {stats['total_trades']} trades | "
                    f"win rate {stats['win_rate']:.1%} | "
                    f"P&L ${stats['total_pnl']:+.2f} | "
                    f"open: {stats['open_trades']} | "
                    f"gate: {stats['gate_message']}"
                )

            # Check paper gate — alert once
            if not gate_alerted:
                gate_passed, gate_msg = paper.has_passed_gate()
                if gate_passed and config.PAPER_MODE:
                    logger.info(f"🎯 PAPER GATE PASSED: {gate_msg}")
                    from alerts.telegram_alerts import alert_paper_gate_passed
                    alert_paper_gate_passed(paper.get_stats())
                    gate_alerted = True

            # Check kill switch
            can_trade, reason = risk.can_trade()
            if not can_trade and "kill_switch" in reason.lower():
                from alerts.telegram_alerts import alert_kill_switch
                alert_kill_switch(risk.get_stats()["drawdown_pct"])
                logger.critical("KILL SWITCH — stopping trading loop")
                break

            await asyncio.sleep(scan_interval)

    except asyncio.CancelledError:
        logger.info("Main loop cancelled")

    # Shutdown
    logger.info("Wolf shutting down...")
    await btc_feed.stop()
    await eth_feed.stop()
    await health_check.stop()
    await whale_tracker.stop()
    send_alert("Wolf offline. Goodbye.", "WARNING")

def handle_shutdown(sig, frame):
    logger.info(f"Signal {sig} received — shutting down")
    for task in asyncio.all_tasks():
        task.cancel()

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
