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
        self.analysis_interval = 120  # Every 2 min — faster feedback loop

        # Learned adjustments — strategies read these at scan time
        self.min_confidence_overrides: dict[str, float] = {}  # per-strategy floor
        self.wallet_penalty: dict[str, float] = {}            # reduce weight on bad wallets
        self.bad_price_ranges: list[tuple] = []               # (low, high) → avoid
        self.paused_strategies: set[str] = set()              # strategies suspended due to WR collapse
        self.lesson_log: list[str] = []                       # human-readable lessons
        self._last_lesson_hash: dict[str, int] = {}
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
                self.paused_strategies        = set(state.get('paused', []))
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
                'paused':         list(self.paused_strategies),
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
        if not hasattr(self, '_last_lesson_hash'):
            self._last_lesson_hash = {}

        try:
            with sqlite3.connect(self.db_path) as conn:
                # ── 1. Overall win rate by strategy ──────────────────────────
                # Track both strategy-level AND sub_strategy-level (btc_scalper sub-modes)
                rows = conn.execute("""
                    SELECT
                        COALESCE(sub_strategy, strategy) as track_key,
                        COUNT(*) as total,
                        SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl) as avg_pnl,
                        AVG(entry_price) as avg_entry
                    FROM paper_trades WHERE resolved=1 AND simulated=0
                    GROUP BY COALESCE(sub_strategy, strategy)
                """).fetchall()

                for row in rows:
                    strat, total, wins, avg_pnl, avg_entry = row
                    if total < 5:  # Lowered from 10 — act faster on early data
                        continue
                    wr = wins / total

                    # ── Rolling last-10-trade WR for this strategy ───────────
                    last10 = conn.execute("""
                        SELECT won FROM paper_trades
                        WHERE resolved=1 AND simulated=0 AND COALESCE(sub_strategy,strategy)=?
                        ORDER BY timestamp DESC LIMIT 10
                    """, (strat,)).fetchall()
                    rolling_wr = sum(r[0] for r in last10) / len(last10) if len(last10) >= 10 else None

                    # ── Pause strategy if rolling WR collapses below 25% ─────
                    if rolling_wr is not None and rolling_wr < 0.25 and total >= 10:
                        if strat not in self.paused_strategies:
                            self.paused_strategies.add(strat)
                            msg = f"[{strat}] PAUSED — rolling WR={rolling_wr:.0%} on last 10 trades (total={total})"
                            logger.warning(f"📚 {msg}")
                            self.lesson_log.append(msg)
                            lessons[strat] = {"action": "paused", "rolling_wr": rolling_wr, "total": total}
                    # ── Unpause if rolling WR recovers above 55% ─────────────
                    elif strat in self.paused_strategies and (rolling_wr is None or rolling_wr >= 0.55):
                        self.paused_strategies.discard(strat)
                        msg = f"[{strat}] UNPAUSED — rolling WR recovered to {rolling_wr:.0%}" if rolling_wr else f"[{strat}] UNPAUSED — insufficient data"
                        logger.info(f"📚 {msg}")
                        self.lesson_log.append(msg)

                    # If strategy win rate below 65%, raise its confidence floor aggressively
                    # Guard: need ≥10 trades before raising floor (prevents false positives from tiny samples)
                    if wr < 0.65 and total >= 10:
                        old = self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE)
                        # Larger step-up the further below target we are
                        step = 0.08 if wr < 0.40 else 0.05
                        new_floor = min(0.92, old + step)
                        self.min_confidence_overrides[strat] = new_floor
                        msg = f"[{strat}] WR={wr:.1%} < 65% — raising confidence floor {old:.2f}→{new_floor:.2f}"
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(strat) != _lh:
                            self._last_lesson_hash[strat] = _lh
                            logger.info(f"📚 Lesson: {msg}")
                        self.lesson_log.append(msg)
                        lessons[strat] = {"action": "raised_confidence_floor", "new_floor": new_floor, "wr": wr}
                    elif wr >= 0.80:
                        # Performing well — relax floor to capture more opportunities
                        old = self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE)
                        new_floor = max(config.MIN_CONFIDENCE, old - 0.02)
                        self.min_confidence_overrides[strat] = new_floor
                        msg = f"[{strat}] WR={wr:.1%} ≥ 80% — relaxing confidence floor to {new_floor:.2f}"
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(strat) != _lh:
                            self._last_lesson_hash[strat] = _lh
                            logger.info(f"📚 Lesson: {msg}")
                        lessons[strat] = {"action": "relaxed_confidence_floor", "new_floor": new_floor, "wr": wr}

                # ── 2. Identify losing price ranges ──────────────────────────
                loss_rows = conn.execute("""
                    SELECT entry_price, COUNT(*) as cnt,
                           SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins
                    FROM paper_trades WHERE resolved=1 AND simulated=0
                    GROUP BY ROUND(entry_price, 1)
                    HAVING cnt >= 8  -- lowered from 15 — act faster on bad price ranges
                """).fetchall()

                self.bad_price_ranges = []
                for price, cnt, wins in loss_rows:
                    wr = wins / cnt
                    if wr < 0.40:  # Only block truly bad ranges (40% WR floor)
                        # This price bucket is a loser — flag it
                        low = round(price - 0.05, 2)
                        high = round(price + 0.05, 2)
                        self.bad_price_ranges.append((low, high))
                        msg = f"Price range {low:.2f}–{high:.2f} has {wr:.0%} WR on {cnt} trades — flagged as weak"
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(strat) != _lh:
                            self._last_lesson_hash[strat] = _lh
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
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(strat) != _lh:
                            self._last_lesson_hash[strat] = _lh
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

    def is_strategy_paused(self, strategy: str) -> bool:
        """Returns True if the learning engine has suspended this strategy due to WR collapse."""
        return strategy in self.paused_strategies

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
