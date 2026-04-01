"""
Lighter Trading Engine — Main Entry Point
Separate from Wolf. Runs autonomously on Lighter.xyz perpetual futures.
"""
import asyncio
import logging
import signal
import sys
import time

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("lighter.main")

shutdown = False

def handle_signal(signum, frame):
    global shutdown
    logger.info(f"Signal {signum} received — shutting down gracefully")
    shutdown = True

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

async def main():
    logger.info(f"🚀 {config.ENGINE_NAME} Engine starting (paper={config.PAPER_MODE})")
    logger.info(f"   Markets: {config.MARKETS}")
    logger.info(f"   Starting capital: ${config.PAPER_STARTING_CAPITAL}")
    logger.info(f"   Risk: {config.MAX_RISK_PER_TRADE_PCT*100}%/trade, max {config.MAX_LEVERAGE}x leverage")

    # TODO: Initialize strategies, feeds, risk manager
    # from strategies.candle_reversal import CandleReversalStrategy
    # from strategies.pivot_reversion import PivotReversionStrategy
    # from strategies.keltner_reversion import KeltnerReversionStrategy
    # from strategies.funding_arb import FundingArbStrategy
    # from strategies.market_maker import MarketMakerStrategy
    # from strategies.trend_following import TrendFollowStrategy
    # from risk.risk_manager import RiskManager
    # from feeds.lighter_feed import LighterFeed

    cycle = 0
    while not shutdown:
        cycle += 1
        logger.info(f"Cycle {cycle} — scanning {len(config.MARKETS)} markets")

        # TODO: Run each strategy scan
        # TODO: Pass through risk manager
        # TODO: Execute approved orders

        await asyncio.sleep(config.CYCLE_INTERVAL)

    logger.info("Lighter Engine stopped.")

if __name__ == "__main__":
    asyncio.run(main())
