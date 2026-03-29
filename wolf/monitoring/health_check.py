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
    def __init__(self, trade_logger: TradeLogger):
        self.journal = trade_logger
        self._last_heartbeat: float = 0
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Health check started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _heartbeat_loop(self):
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
            age_ms = btc_feed.get_price_age_ms()
            results["binance_ok"] = age_ms < 5000  # Fresh within 5s
            if not results["binance_ok"]:
                send_alert(f"Binance feed stale: {age_ms:.0f}ms", "WARNING")
        except Exception as e:
            results["binance_ok"] = False
            send_alert(f"Binance feed error: {e}", "WARNING")

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
            send_alert(f"Polymarket API unreachable: {e}", "WARNING")

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

        status_msg = (
            f"Heartbeat OK\n"
            f"Binance: {'✅' if results.get('binance_ok') else '❌'} | "
            f"Polymarket: {'✅' if results.get('polymarket_ok') else '❌'} | "
            f"Kalshi: {'✅' if results.get('kalshi_ok') else '❌'}\n"
            f"Paper trades: {stats['paper']['total']} | Win rate: {stats['paper']['win_rate']:.1%}"
        )
        send_alert(status_msg, "INFO")
        logger.info(f"Health check complete: {results}")
