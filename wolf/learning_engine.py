"""
Wolf Trading Bot — Adaptive Learning Engine
Continuously analyzes trade outcomes to sharpen entry filters.
Tracks: which markets win/lose, which price ranges perform, which wallets nail it.
Adjusts confidence thresholds and wallet weights dynamically.

Upgrades over v1:
  - get_calibrated_confidence(): Bayesian blend of signal confidence with
    historical win rate. Self-improving — gets smarter with every trade.
  - Rolling WR pause threshold raised to 30% (was 25%) — avoids false pauses
    on small sample variance.
  - Lesson dedup is per-strategy (was accidentally using last strat's hash key).
"""
import sqlite3
import os
import time
import logging
import math
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
        self.min_confidence_overrides: dict[str, float] = {}
        self.wallet_penalty:           dict[str, float] = {}
        self.bad_price_ranges:         list[tuple]      = []
        self.paused_strategies:        set[str]         = set()
        self.lesson_log:               list[str]        = []
        self._last_lesson_hash:        dict[str, int]   = {}
        self._state_path = os.path.join(os.path.dirname(config.DB_PATH), "learning_state.json")
        self._load_state()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_state(self):
        """Load persisted learning state from disk — survives restarts."""
        try:
            if os.path.exists(self._state_path):
                import json
                state = json.loads(open(self._state_path).read())
                self.min_confidence_overrides = state.get("floors", {})
                self.wallet_penalty           = state.get("wallet_penalty", {})
                self.bad_price_ranges         = [tuple(r) for r in state.get("bad_ranges", [])]
                self.paused_strategies        = set(state.get("paused", []))
                logger.info(
                    f"📚 Learning state loaded: {len(self.min_confidence_overrides)} floors, "
                    f"{len(self.bad_price_ranges)} bad ranges"
                )
        except Exception as e:
            logger.warning(f"Learning state load failed: {e}")

    def save_state(self):
        return self._save_state()

    def _save_state(self):
        """Persist learning state to disk so floors survive restarts."""
        try:
            import json
            state = {
                "floors":         self.min_confidence_overrides,
                "wallet_penalty": self.wallet_penalty,
                "bad_ranges":     [list(r) for r in self.bad_price_ranges],
                "paused":         list(self.paused_strategies),
                "saved_at":       time.time(),
            }
            open(self._state_path, "w").write(json.dumps(state, indent=2))
        except Exception as e:
            logger.warning(f"Learning state save failed: {e}")

    # ── Calibrated confidence (NEW) ───────────────────────────────────────────

    def get_calibrated_confidence(
        self,
        strategy: str,
        raw_conf: float,
        entry_price: float,
        price_band: float = 0.06,
    ) -> float:
        """
        Bayesian blend of raw signal confidence with historical win rate.

        As Wolf accumulates trade history, this replaces the hard-coded
        confidence values with data-driven estimates. The weight given to
        history grows with sqrt(n), capped at 80% to always preserve some
        weight for the live signal.

        Args:
            strategy:    Strategy name (e.g. "value_bet")
            raw_conf:    Raw signal confidence from strategy logic [0, 1]
            entry_price: Entry price — history is filtered to ±price_band
            price_band:  Width of price bucket to compare against

        Returns:
            Calibrated confidence [0.50, 0.99]
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) as n,
                        AVG(CASE WHEN won=1 THEN 1.0 ELSE 0.0 END) as wr
                    FROM paper_trades
                    WHERE strategy = ?
                      AND resolved = 1
                      AND simulated = 0
                      AND COALESCE(void, 0) = 0
                      AND ABS(entry_price - ?) < ?
                    """,
                    (strategy, entry_price, price_band),
                ).fetchone()

            if row is None:
                return raw_conf

            n, hist_wr = row
            if not n or n < 10 or hist_wr is None:
                # Not enough history — trust raw signal entirely
                return raw_conf

            # Bayesian blend: history weight grows with sqrt(n), caps at 80%
            # At n=10: weight=15%  At n=50: weight=35%  At n=400: weight=80%
            hist_weight = min(0.80, (n ** 0.5) / 25.0)
            signal_weight = 1.0 - hist_weight

            blended = raw_conf * signal_weight + hist_wr * hist_weight
            calibrated = max(0.50, min(0.99, blended))

            if abs(calibrated - raw_conf) > 0.05:
                logger.debug(
                    f"[CALIBRATE] {strategy}@{entry_price:.2f}: "
                    f"{raw_conf:.3f} → {calibrated:.3f} "
                    f"(hist_wr={hist_wr:.1%} n={n} weight={hist_weight:.0%})"
                )

            return round(calibrated, 3)

        except Exception as e:
            logger.debug(f"Calibration error for {strategy}: {e}")
            return raw_conf

    # ── Analysis loop ─────────────────────────────────────────────────────────

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
        lessons: dict = {}

        try:
            with sqlite3.connect(self.db_path) as conn:
                # ── 1. Win rate by strategy (with rolling 10-trade window) ────
                rows = conn.execute(
                    """
                    SELECT
                        COALESCE(sub_strategy, strategy) as track_key,
                        COUNT(*) as total,
                        SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                        AVG(pnl) as avg_pnl,
                        AVG(entry_price) as avg_entry
                    FROM paper_trades
                    WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                    GROUP BY COALESCE(sub_strategy, strategy)
                    """
                ).fetchall()

                for row in rows:
                    strat, total, wins, avg_pnl, avg_entry = row
                    if total < 5:
                        continue
                    wr = wins / total

                    # Rolling last-10 WR
                    last10 = conn.execute(
                        """
                        SELECT won FROM paper_trades
                        WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                          AND COALESCE(sub_strategy, strategy)=?
                        ORDER BY timestamp DESC LIMIT 10
                        """,
                        (strat,),
                    ).fetchall()
                    rolling_wr = (
                        sum(r[0] for r in last10) / len(last10)
                        if len(last10) >= 10
                        else None
                    )

                    # Pause if rolling WR collapses (raised threshold: 30% was 25%)
                    if rolling_wr is not None and rolling_wr < 0.30 and total >= 10:
                        if strat not in self.paused_strategies:
                            self.paused_strategies.add(strat)
                            msg = (
                                f"[{strat}] PAUSED — rolling WR={rolling_wr:.0%} "
                                f"on last 10 trades (total={total})"
                            )
                            logger.warning(f"📚 {msg}")
                            self.lesson_log.append(msg)
                            lessons[strat] = {"action": "paused", "rolling_wr": rolling_wr}
                    elif strat in self.paused_strategies and (
                        rolling_wr is None or rolling_wr >= 0.55
                    ):
                        self.paused_strategies.discard(strat)
                        msg = (
                            f"[{strat}] UNPAUSED — rolling WR {rolling_wr:.0%}"
                            if rolling_wr
                            else f"[{strat}] UNPAUSED"
                        )
                        logger.info(f"📚 {msg}")
                        self.lesson_log.append(msg)

                    # Adjust confidence floor
                    if wr < 0.65 and total >= 10:
                        old = self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE)
                        step = 0.08 if wr < 0.40 else 0.05
                        new_floor = min(config.MIN_CONFIDENCE + 0.05, old + step)
                        self.min_confidence_overrides[strat] = new_floor
                        msg = (
                            f"[{strat}] WR={wr:.1%} < 65% — "
                            f"raising confidence floor {old:.2f}→{new_floor:.2f}"
                        )
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(strat) != _lh:
                            self._last_lesson_hash[strat] = _lh
                            logger.info(f"📚 Lesson: {msg}")
                        self.lesson_log.append(msg)
                        lessons[strat] = {
                            "action": "raised_confidence_floor",
                            "new_floor": new_floor,
                            "wr": wr,
                        }
                    elif wr >= 0.80:
                        old = self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE)
                        new_floor = max(config.MIN_CONFIDENCE, old - 0.02)
                        self.min_confidence_overrides[strat] = new_floor
                        msg = f"[{strat}] WR={wr:.1%} ≥ 80% — relaxing floor to {new_floor:.2f}"
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(strat) != _lh:
                            self._last_lesson_hash[strat] = _lh
                            logger.info(f"📚 Lesson: {msg}")
                        lessons[strat] = {
                            "action": "relaxed_confidence_floor",
                            "new_floor": new_floor,
                            "wr": wr,
                        }

                # ── 2. Losing price ranges ────────────────────────────────────
                loss_rows = conn.execute(
                    """
                    SELECT entry_price, COUNT(*) as cnt,
                           SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins
                    FROM paper_trades
                    WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                    GROUP BY ROUND(entry_price, 1)
                    HAVING cnt >= 30
                    """
                ).fetchall()

                self.bad_price_ranges = []
                for price, cnt, wins in loss_rows:
                    if cnt and wins is not None and (wins / cnt) < 0.40:
                        low  = round(price - 0.05, 2)
                        high = round(price + 0.05, 2)
                        self.bad_price_ranges.append((low, high))
                        msg = f"Price range {low:.2f}–{high:.2f}: {wins/cnt:.0%} WR on {cnt} trades"
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(f"range_{price}") != _lh:
                            self._last_lesson_hash[f"range_{price}"] = _lh
                            logger.info(f"📚 Lesson: {msg}")

                # ── 3. Copy trading wallet penalties ──────────────────────────
                wallet_rows = conn.execute(
                    """
                    SELECT reason, COUNT(*) as total,
                           SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins
                    FROM paper_trades
                    WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                      AND strategy='copy_trading' AND reason IS NOT NULL
                    GROUP BY reason
                    HAVING total >= 3
                    """
                ).fetchall()

                for reason, total, wins in wallet_rows:
                    if total and wins is not None and (wins / total) < 0.50 and reason:
                        self.wallet_penalty[reason] = 0.3
                        msg = f"Wallet {reason[:12]}… WR={wins/total:.0%} — penalized"
                        _lh = hash(msg)
                        if self._last_lesson_hash.get(reason) != _lh:
                            self._last_lesson_hash[reason] = _lh
                            logger.info(f"📚 Lesson: {msg}")

                # ── 4. Summary ────────────────────────────────────────────────
                total_row = conn.execute(
                    "SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), SUM(pnl) "
                    "FROM paper_trades WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0"
                ).fetchone()
                total_trades, total_wins, total_pnl = total_row
                overall_wr = total_wins / total_trades if total_trades else 0

                lessons["summary"] = {
                    "total_trades": total_trades,
                    "overall_wr":   overall_wr,
                    "total_pnl":    total_pnl,
                    "confidence_overrides": dict(self.min_confidence_overrides),
                    "bad_price_ranges":     self.bad_price_ranges,
                    "wallet_penalties":     len(self.wallet_penalty),
                }

                if total_trades and total_trades % 10 == 0:
                    logger.info(
                        f"📊 Learning: {total_trades} trades | "
                        f"WR={overall_wr:.1%} | PnL=${total_pnl:+.2f} | "
                        f"{len(self.min_confidence_overrides)} floors | "
                        f"{len(self.bad_price_ranges)} bad ranges | "
                        f"{len(self.wallet_penalty)} penalized wallets"
                    )

        except Exception as e:
            logger.warning(f"Learning analysis error: {e}")

        self._save_state()
        return lessons

    # ── Public query API ──────────────────────────────────────────────────────

    def is_strategy_paused(self, strategy: str) -> bool:
        return strategy in self.paused_strategies

    def get_confidence_floor(self, strategy: str) -> float:
        return self.min_confidence_overrides.get(strategy, config.MIN_CONFIDENCE)

    def is_bad_price(self, price: float) -> bool:
        for low, high in self.bad_price_ranges:
            if low <= price <= high:
                return True
        return False

    def get_wallet_weight_multiplier(self, wallet_addr: str) -> float:
        return self.wallet_penalty.get(wallet_addr, 1.0)

    def ingest_analytics(self, report: dict):
        """Consume a structured analytics report and apply its lessons."""
        for bucket in report.get("price_buckets", []):
            if bucket.get("total", 0) >= 5 and bucket["win_rate"] < 0.50:
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
                new_floor = min(
                    config.MIN_CONFIDENCE + 0.05,
                    self.min_confidence_overrides.get(strat, config.MIN_CONFIDENCE) + 0.03,
                )
                self.min_confidence_overrides[strat] = new_floor
                msg = f"Analytics raised {strat} floor to {new_floor:.2f} (WR {wr:.1%})"
                self.lesson_log.append(msg)
                logger.info(f"📚 {msg}")

    def get_status(self) -> str:
        return (
            f"Learning engine: {len(self.lesson_log)} lessons | "
            f"Floors: {len(self.min_confidence_overrides)} | "
            f"Bad ranges: {len(self.bad_price_ranges)} | "
            f"Penalized wallets: {len(self.wallet_penalty)}"
        )


# Global singleton — imported by strategies
learning = LearningEngine()
