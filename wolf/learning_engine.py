"""
Wolf Trading Bot — Adaptive Learning Engine
Continuously analyzes trade outcomes to sharpen entry filters.
Tracks: which markets win/lose, which price ranges perform, which wallets nail it.
Adjusts confidence thresholds and wallet weights dynamically.
"""
import sqlite3
import os
import time
import logging
import config

logger = logging.getLogger("wolf.learning")


class LearningEngine:
    """
    Post-trade analysis loop. Runs periodically to extract lessons from losses
    and reinforce winning patterns. Writes learned adjustments back to runtime state.
    """

    def __init__(self):
        self.db_path = config.DB_PATH
        self.last_analysis = 0.0
        self.analysis_interval = 300  # Every 5 min

        # Learned adjustments — strategies read these at scan time
        self.min_confidence_overrides: dict[str, float] = {}  # per-strategy floor
        self.wallet_penalty: dict[str, float] = {}            # reduce weight on bad wallets
        self.bad_price_ranges: list[tuple] = []               # (low, high) → avoid
        self.lesson_log: list[str] = []                       # human-readable lessons
        self._state_path = os.path.join(os.path.dirname(config.DB_PATH), 'learning_state.json')
        self._load_state()

    def _load_state(self):
        """Load persisted learning state from disk — survives restarts."""
        try:
            if os.path.exists(self._state_path):
                import json
                state = json.loads(open(self._state_path).read())
                self.min_confidence_overrides = state.get('floors', {})
                self.wallet_penalty           = state.get('wallet_penalty', {})
                self.bad_price_ranges         = [tuple(r) for r in state.get('bad_ranges', [])]
                logger.info(f"📚 Learning state loaded: {len(self.min_confidence_overrides)} floors, "
                            f"{len(self.bad_price_ranges)} bad ranges")
        except Exception as e:
            logger.warning(f"Learning state load failed: {e}")

    def _save_state(self):
        """Persist learning state to disk so floors survive restarts."""
        try:
            import json
            state = {
                'floors':         self.min_confidence_overrides,
                'wallet_penalty': self.wallet_penalty,
                'bad_ranges':     [list(r) for r in self.bad_price_ranges],
                'saved_at':       time.time(),
            }
            open(self._state_path, 'w').write(json.dumps(state, indent=2))
        except Exception as e:
            logger.warning(f"Learning state save failed: {e}")

    def should_run(self) -> bool:
        return time.time() - self.last_analysis > self.analysis_interval

    def analyze(self) -> dict:
        """
        Pull resolved trades, find patterns in losses, update thresholds.
        Returns summary of lessons learned this cycle.
        """
        if not self.should_run():
            return {}

        self.last_analysis = time.time()
        lessons = {}

        try:
            with sqlite3.connect(self.db_path) as conn:
                # ── 1. Overall win rate by strategy ──────────────────────────
                rows = conn.execute("""
                    SELECT strategy,
                           COUNT(*) as total,
                           SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                           AVG(pnl) as avg_pnl,
                           AVG(entry_price) as avg_entry
                    FROM paper_trades WHERE resolved=1 AND simulated=0
                    GROUP BY strategy
                """).fetchall()

                for row in rows:
                    strat, total, wins, avg_pnl, avg_entry = row
                    if total < 10:  # Need at least 10 trades for meaningful stats
                        continue
                    wr = wins / total

                    # If strategy win rate below 70%, raise its confidence floor
                    if wr < 0.70:
                        old = self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE)
                        new_floor = min(0.85, old + 0.05)
                        self.min_confidence_overrides[strat] = new_floor
                        msg = f"[{strat}] WR={wr:.1%} < 70% — raising confidence floor to {new_floor:.2f}"
                        logger.info(f"📚 Lesson: {msg}")
                        self.lesson_log.append(msg)
                        lessons[strat] = {"action": "raised_confidence_floor", "new_floor": new_floor, "wr": wr}
                    elif wr >= 0.85:
                        # Performing well — can slightly relax floor to capture more opportunities
                        old = self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE)
                        new_floor = max(config.MIN_CONFIDENCE, old - 0.02)
                        self.min_confidence_overrides[strat] = new_floor
                        msg = f"[{strat}] WR={wr:.1%} ≥ 85% — relaxing confidence floor to {new_floor:.2f}"
                        logger.info(f"📚 Lesson: {msg}")
                        lessons[strat] = {"action": "relaxed_confidence_floor", "new_floor": new_floor, "wr": wr}

                # ── 2. Identify losing price ranges ──────────────────────────
                loss_rows = conn.execute("""
                    SELECT entry_price, COUNT(*) as cnt,
                           SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins
                    FROM paper_trades WHERE resolved=1 AND simulated=0
                    GROUP BY ROUND(entry_price, 1)
                    HAVING cnt >= 3
                """).fetchall()

                self.bad_price_ranges = []
                for price, cnt, wins in loss_rows:
                    wr = wins / cnt
                    if wr < 0.50:
                        # This price bucket is a loser — flag it
                        low = round(price - 0.05, 2)
                        high = round(price + 0.05, 2)
                        self.bad_price_ranges.append((low, high))
                        msg = f"Price range {low:.2f}–{high:.2f} has {wr:.0%} WR on {cnt} trades — flagged as weak"
                        logger.info(f"📚 Lesson: {msg}")

                # ── 3. Copy trading: identify wallets with poor outcomes ──────
                wallet_rows = conn.execute("""
                    SELECT reason, COUNT(*) as total,
                           SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins
                    FROM paper_trades
                    WHERE resolved=1 AND simulated=0 AND strategy='copy_trading' AND reason IS NOT NULL
                    GROUP BY reason
                    HAVING total >= 3
                """).fetchall()

                for reason, total, wins in wallet_rows:
                    wr = wins / total
                    # reason field stores wallet address for copy trades
                    if wr < 0.50 and reason:
                        self.wallet_penalty[reason] = 0.3  # reduce to 30% weight
                        msg = f"Wallet {reason[:12]}... WR={wr:.0%} — penalized"
                        logger.info(f"📚 Lesson: {msg}")

                # ── 4. Summarize current learned state ────────────────────────
                total_row = conn.execute(
                    "SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl) "
                    "FROM paper_trades WHERE resolved=1 AND simulated=0"
                ).fetchone()
                total_trades, total_wins, total_pnl = total_row
                overall_wr = total_wins / total_trades if total_trades else 0

                lessons["summary"] = {
                    "total_trades": total_trades,
                    "overall_wr": overall_wr,
                    "total_pnl": total_pnl,
                    "confidence_overrides": dict(self.min_confidence_overrides),
                    "bad_price_ranges": self.bad_price_ranges,
                    "wallet_penalties": len(self.wallet_penalty),
                }

                if total_trades and total_trades % 10 == 0:
                    logger.info(
                        f"📊 Learning summary: {total_trades} trades | "
                        f"WR={overall_wr:.1%} | PnL=${total_pnl:+.2f} | "
                        f"Adjustments: {len(self.min_confidence_overrides)} strategy floors, "
                        f"{len(self.bad_price_ranges)} bad price ranges, "
                        f"{len(self.wallet_penalty)} penalized wallets"
                    )

        except Exception as e:
            logger.warning(f"Learning analysis error: {e}")

        self._save_state()
        return lessons

    def get_confidence_floor(self, strategy: str) -> float:
        """Returns the learned confidence floor for a strategy."""
        return self.min_confidence_overrides.get(strategy, config.MIN_CONFIDENCE)

    def is_bad_price(self, price: float) -> bool:
        """Returns True if this price bucket has historically underperformed."""
        for low, high in self.bad_price_ranges:
            if low <= price <= high:
                return True
        return False

    def get_wallet_weight_multiplier(self, wallet_addr: str) -> float:
        """Returns weight multiplier for a wallet (1.0 = normal, 0.3 = penalized)."""
        return self.wallet_penalty.get(wallet_addr, 1.0)

    def ingest_analytics(self, report: dict):
        """
        Consume a structured analytics report and apply its lessons.
        Called by main loop after each hourly report.
        """
        for bucket in report.get("price_buckets", []):
            if bucket.get("total", 0) >= 5 and bucket["win_rate"] < 0.50:
                # Parse range string e.g. "0.1–0.2"
                try:
                    lo_str, hi_str = bucket["range"].split("–")
                    lo, hi = float(lo_str), float(hi_str)
                    entry = (lo, hi)
                    if entry not in self.bad_price_ranges:
                        self.bad_price_ranges.append(entry)
                        msg = f"Analytics blocked price range {bucket['range']} (WR {bucket['win_rate']:.1%})"
                        self.lesson_log.append(msg)
                        logger.info(f"📚 {msg}")
                except Exception:
                    pass

        for strat, data in report.get("by_strategy", {}).items():
            if data.get("total", 0) < 10:
                continue
            wr = data["win_rate"]
            if wr < 0.60:
                new_floor = min(0.85, self.min_confidence_overrides.get(strat, 0.65) + 0.03)
                self.min_confidence_overrides[strat] = new_floor
                msg = f"Analytics raised {strat} floor to {new_floor:.2f} (WR {wr:.1%})"
                self.lesson_log.append(msg)
                logger.info(f"📚 {msg}")

    def get_status(self) -> str:
        floors = self.min_confidence_overrides
        return (
            f"Learning engine: {len(self.lesson_log)} lessons | "
            f"Floors: {floors} | "
            f"Bad ranges: {len(self.bad_price_ranges)} | "
            f"Penalized wallets: {len(self.wallet_penalty)}"
        )


# Global singleton — imported by strategies
learning = LearningEngine()
