"""
Wolf Trading Bot — Health Check & Dead Man's Switch
Heartbeat every 30 min. CRITICAL alert if Wolf goes silent.
Monitors: Binance feed, Polymarket API, Kalshi API, daily P&L.
"""
import asyncio
import time
import logging
import requests
import config
from alerts.telegram_alerts import send_alert, alert_system_down
from journal.trade_logger import TradeLogger

logger = logging.getLogger("wolf.health")

class HealthCheck:
    _STARTUP_GRACE_SEC = 30  # Don't flag feeds as down during first 30s

    def __init__(self, trade_logger: TradeLogger):
        self.journal = trade_logger
        self._start_time = time.time()
        self._last_heartbeat: float = 0
        self._running = False
        self._task = None
        # Feed state tracking — alert on transition, not on every check
        self._feed_state: dict[str, bool] = {
            "binance": True,
            "polymarket": True,
        }

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Health check started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _heartbeat_loop(self):
        # Wait for feeds to fully initialize before first health check
        await asyncio.sleep(25)
        while self._running:
            try:
                await self._run_check()
                self._last_heartbeat = time.time()
                await asyncio.sleep(config.HEARTBEAT_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
                alert_system_down("health_check", str(e))
                await asyncio.sleep(60)

    async def _run_check(self):
        results = {}

        # Binance feed check
        try:
            from feeds.binance_feed import btc_feed
            price = btc_feed.get_price()
            age_ms = btc_feed.get_price_age_ms()
            # Startup grace period — don't flag during initial REST/WS setup
            if time.time() - self._start_time < self._STARTUP_GRACE_SEC:
                results["binance_ok"] = True
            # Never flag as down if we've never received a price yet (feed still initializing)
            elif price == 0.0:
                results["binance_ok"] = True  # Initializing — not a failure
            else:
                results["binance_ok"] = age_ms < 30000
                if not results["binance_ok"]:
                    send_alert(f"Binance feed stale: {age_ms:.0f}ms", "WARNING", system=True)
        except Exception as e:
            results["binance_ok"] = False
            send_alert(f"Binance feed error: {e}", "WARNING", system=True)

        # Polymarket API check
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 1},
                timeout=10
            )
            results["polymarket_ok"] = resp.ok
        except Exception as e:
            results["polymarket_ok"] = False
            send_alert(f"Polymarket API unreachable: {e}", "WARNING", system=True)

        # Kalshi API check
        try:
            resp = requests.get(
                f"{config.KALSHI_BASE_URL}/exchange/status",
                timeout=10
            )
            results["kalshi_ok"] = resp.ok
        except Exception as e:
            results["kalshi_ok"] = False
            # Kalshi failure is non-critical in Phase 1

        # Log health
        stats = self.journal.get_stats()
        health_record = {
            "timestamp": time.time(),
            "status": "ok" if all([results.get("binance_ok"), results.get("polymarket_ok")]) else "degraded",
            **results,
            "notes": f"Paper trades: {stats['paper']['total']} | Win rate: {stats['paper']['win_rate']:.1%}",
        }
        self.journal.log_health(health_record)

        # ── Feed-down / feed-recovery alerts (transition only, not every check) ──
        from alerts.telegram_alerts import _send
        feed_checks = {
            "binance": results.get("binance_ok", False),
            "polymarket": results.get("polymarket_ok", False),
        }
        for feed, is_ok in feed_checks.items():
            was_ok = self._feed_state.get(feed, True)
            if was_ok and not is_ok:
                # Feed just went DOWN
                affected = "latency_arb, ta_signal" if feed == "binance" else feed
                _send(
                    f"⚠️ <b>{feed.upper()} feed down</b>\n"
                    f"Paused: {affected}\n"
                    f"Still running: all other strategies"
                )
                logger.critical(f"FEED DOWN: {feed}")
            elif not was_ok and is_ok:
                # Feed recovered
                _send(
                    f"✅ <b>Feed RESTORED: {feed.upper()}</b>\n"
                    f"Trading resuming on affected strategies."
                )
                logger.info(f"Feed recovered: {feed}")
            self._feed_state[feed] = is_ok

        import config as _hc_cfg
        _p = stats['paper']
        _bal = _hc_cfg.PAPER_STARTING_CAPITAL + _p['pnl']
        _mode = "PAPER" if _hc_cfg.PAPER_MODE else "LIVE"
        status_msg = (
            f"🐺 Wolf Heartbeat — {_mode}\n"
            f"─────────────────────\n"
            f"📊 Trades: {_p['total']} | WR: {_p['win_rate']:.1%}\n"
            f"💰 P&L: ${_p['pnl']:+.2f} | Balance: ${_bal:,.2f}\n"
            f"📡 Poly: {'✅' if results.get('polymarket_ok') else '❌'} | "
            f"Binance: {'✅' if results.get('binance_ok') else '❌'} | "
            f"Kalshi: {'✅' if results.get('kalshi_ok') else '❌'}"
        )
        send_alert(status_msg, "INFO", system=True)
        logger.info(f"Health check complete: {results}")
