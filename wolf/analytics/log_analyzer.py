"""
Wolf Trading Bot — Log Analyzer
Parses wolf_data.db to produce structured performance reports.
Wolf reads these reports every session to improve strategy parameters.
Jefe can request a report anytime: the output is Telegram-formatted.

Upgrades over v1:
  - Sharpe and Sortino ratio computed per strategy and overall
  - strategy_metrics block included in every report
  - _compute_sharpe / _compute_sortino helpers shared with risk_engine
"""
import sqlite3
import json
import time
import math
import logging
import os
import re
from datetime import datetime
from collections import defaultdict
import config

logger = logging.getLogger("wolf.analytics")

LOG_PATH    = "/data/.openclaw/workspace/wolf/wolf.log"
REPORT_PATH = "/data/.openclaw/workspace/wolf/analytics/last_report.json"

# Approximate number of trades per day — used to annualise Sharpe
TRADES_PER_DAY = 8


def _compute_sharpe(pnl_series: list[float], risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio from a list of per-trade PnL values."""
    n = len(pnl_series)
    if n < 5:
        return 0.0
    mean = sum(pnl_series) / n
    variance = sum((x - mean) ** 2 for x in pnl_series) / max(n - 1, 1)
    std = math.sqrt(variance) if variance > 0 else 1e-9
    daily_sharpe = (mean - risk_free) / std
    return round(daily_sharpe * math.sqrt(TRADES_PER_DAY * 365), 2)


def _compute_sortino(pnl_series: list[float]) -> float:
    """Annualised Sortino ratio (penalises downside variance only)."""
    if len(pnl_series) < 5:
        return 0.0
    mean = sum(pnl_series) / len(pnl_series)
    downside = [x for x in pnl_series if x < 0]
    if not downside:
        return 99.0  # No losses = infinite Sortino
    dvar = sum(x ** 2 for x in downside) / len(downside)
    dstd = math.sqrt(dvar) if dvar > 0 else 1e-9
    return round((mean / dstd) * math.sqrt(TRADES_PER_DAY * 365), 2)


class LogAnalyzer:
    def __init__(self):
        self.db_path = config.DB_PATH
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)

    # ── Main analysis ─────────────────────────────────────────────────────────

    def analyze_trades(self, hours: int = 24) -> dict:
        """Full trade analysis over the last N hours."""
        since = time.time() - hours * 3600

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Overall stats
            overall = conn.execute(
                """
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as total_pnl,
                       AVG(pnl) as avg_pnl,
                       MIN(pnl) as worst,
                       MAX(pnl) as best,
                       AVG(confidence) as avg_conf
                FROM paper_trades
                WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                  AND timestamp > ?
                """,
                (since,),
            ).fetchone()

            # Per-strategy stats (without PnL list — fetched separately)
            strats = conn.execute(
                """
                SELECT strategy,
                       COUNT(*) as total,
                       SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as pnl,
                       AVG(pnl) as avg_pnl,
                       AVG(confidence) as avg_conf,
                       AVG(entry_price) as avg_price,
                       MIN(pnl) as worst,
                       MAX(pnl) as best
                FROM paper_trades
                WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                  AND timestamp > ?
                GROUP BY strategy
                """,
                (since,),
            ).fetchall()

            # Per-strategy PnL lists for Sharpe/Sortino
            strat_pnls: dict[str, list[float]] = defaultdict(list)
            pnl_rows = conn.execute(
                """
                SELECT strategy, pnl FROM paper_trades
                WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                  AND pnl IS NOT NULL AND timestamp > ?
                ORDER BY strategy, timestamp
                """,
                (since,),
            ).fetchall()
            for row in pnl_rows:
                strat_pnls[row["strategy"]].append(float(row["pnl"]))

            # Win rate by price bucket
            price_buckets = conn.execute(
                """
                SELECT CAST(entry_price * 10 AS INTEGER) as bucket,
                       COUNT(*) as total,
                       SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as pnl
                FROM paper_trades
                WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                  AND timestamp > ?
                GROUP BY bucket ORDER BY bucket
                """,
                (since,),
            ).fetchall()

            # Win rate by hour of day (ET = UTC-4)
            hourly = conn.execute(
                """
                SELECT CAST((timestamp - 14400) / 3600 % 24 AS INTEGER) as hour_et,
                       COUNT(*) as total,
                       SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as pnl
                FROM paper_trades
                WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0
                  AND timestamp > ?
                GROUP BY hour_et ORDER BY hour_et
                """,
                (since,),
            ).fetchall()

            # Worst losses
            losses = conn.execute(
                """
                SELECT strategy, entry_price, side, pnl, reason, timestamp
                FROM paper_trades
                WHERE resolved=1 AND won=0 AND timestamp > ?
                ORDER BY pnl ASC LIMIT 20
                """,
                (since,),
            ).fetchall()

            # Wallet performance (copy trading)
            wallets = conn.execute(
                """
                SELECT SUBSTR(reason, INSTR(reason, '0x'), 12) as wallet_prefix,
                       COUNT(*) as total,
                       SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                       SUM(pnl) as pnl
                FROM paper_trades
                WHERE resolved=1 AND strategy='copy_trading' AND timestamp > ?
                GROUP BY wallet_prefix
                HAVING total >= 3
                ORDER BY pnl DESC LIMIT 10
                """,
                (since,),
            ).fetchall()

        total = overall["total"] or 0
        wins  = overall["wins"] or 0
        all_pnls = [p for pnls in strat_pnls.values() for p in pnls]

        result = {
            "period_hours":  hours,
            "generated_at":  time.time(),
            "overall": {
                "total_trades":      total,
                "wins":              wins,
                "losses":            total - wins,
                "win_rate":          wins / total if total else 0,
                "total_pnl":         round(overall["total_pnl"] or 0, 2),
                "avg_pnl_per_trade": round(overall["avg_pnl"] or 0, 2),
                "best_trade":        round(overall["best"] or 0, 2),
                "worst_trade":       round(overall["worst"] or 0, 2),
                "avg_confidence":    round(overall["avg_conf"] or 0, 3),
                "sharpe":            _compute_sharpe(all_pnls),
                "sortino":           _compute_sortino(all_pnls),
            },
            "by_strategy":        {},
            "price_buckets":      [],
            "hourly_performance": [],
            "worst_losses":       [],
            "wallet_performance": [],
            "lessons":            [],
        }

        # Per-strategy block with Sharpe/Sortino
        for s in strats:
            t = s["total"] or 0
            w = s["wins"] or 0
            strat_name = s["strategy"]
            pnls = strat_pnls.get(strat_name, [])
            result["by_strategy"][strat_name] = {
                "total":          t,
                "wins":           w,
                "win_rate":       round(w / t, 3) if t else 0,
                "pnl":            round(s["pnl"] or 0, 2),
                "avg_pnl":        round(s["avg_pnl"] or 0, 2),
                "avg_confidence": round(s["avg_conf"] or 0, 3),
                "avg_price":      round(s["avg_price"] or 0, 3),
                "best":           round(s["best"] or 0, 2),
                "worst":          round(s["worst"] or 0, 2),
                "sharpe":         _compute_sharpe(pnls),
                "sortino":        _compute_sortino(pnls),
            }

        for b in price_buckets:
            t = b["total"] or 0
            w = b["wins"] or 0
            lo = b["bucket"] * 0.1
            hi = lo + 0.1
            wr = w / t if t else 0
            result["price_buckets"].append({
                "range":    f"{lo:.1f}–{hi:.1f}",
                "total":    t,
                "win_rate": round(wr, 3),
                "pnl":      round(b["pnl"] or 0, 2),
                "flag":     (
                    "⚠️ weak"   if t >= 5 and wr < 0.55 else
                    "✅ strong" if t >= 5 and wr >= 0.75 else ""
                ),
            })

        for h in hourly:
            t = h["total"] or 0
            w = h["wins"] or 0
            result["hourly_performance"].append({
                "hour_et":  h["hour_et"],
                "total":    t,
                "win_rate": round(w / t, 3) if t else 0,
                "pnl":      round(h["pnl"] or 0, 2),
            })

        for loss in losses:
            result["worst_losses"].append({
                "strategy":    loss["strategy"],
                "entry_price": loss["entry_price"],
                "side":        loss["side"],
                "pnl":         round(loss["pnl"] or 0, 2),
                "reason":      (loss["reason"] or "")[:80],
            })

        for w in wallets:
            t = w["total"] or 0
            wi = w["wins"] or 0
            result["wallet_performance"].append({
                "wallet":   w["wallet_prefix"],
                "total":    t,
                "win_rate": round(wi / t, 3) if t else 0,
                "pnl":      round(w["pnl"] or 0, 2),
            })

        result["lessons"] = self._generate_lessons(result)

        with open(REPORT_PATH, "w") as f:
            json.dump(result, f, indent=2)

        return result

    # ── Lesson generation ─────────────────────────────────────────────────────

    def _generate_lessons(self, report: dict) -> list[str]:
        """Derive actionable lessons from the data."""
        lessons = []
        overall = report["overall"]
        strats  = report["by_strategy"]

        wr = overall["win_rate"]
        if wr >= 0.80:
            lessons.append(f"🟢 Overall WR {wr:.1%} — above target. Hold current filters.")
        elif wr >= 0.70:
            lessons.append(f"🟡 Overall WR {wr:.1%} — on track. Monitor closely.")
        else:
            lessons.append(f"🔴 Overall WR {wr:.1%} — below target. Tighten filters.")

        # Sharpe commentary
        sharpe = overall.get("sharpe", 0)
        if sharpe > 2.0:
            lessons.append(f"📈 Portfolio Sharpe {sharpe:.2f} — excellent risk-adjusted return.")
        elif sharpe > 1.0:
            lessons.append(f"📊 Portfolio Sharpe {sharpe:.2f} — solid.")
        elif sharpe > 0:
            lessons.append(f"⚠️ Portfolio Sharpe {sharpe:.2f} — marginal risk-adjusted return.")
        elif sharpe <= 0:
            lessons.append(f"🔴 Portfolio Sharpe {sharpe:.2f} — negative risk-adjusted return.")

        for strat, data in strats.items():
            if data["total"] < 5:
                continue
            swr = data["win_rate"]
            sharpe_s = data.get("sharpe", 0)
            if swr < 0.60:
                lessons.append(
                    f"⛔ {strat}: {swr:.1%} WR on {data['total']} trades — "
                    f"Sharpe {sharpe_s:.2f} — raise confidence floor or pause"
                )
            elif swr >= 0.80:
                lessons.append(
                    f"🚀 {strat}: {swr:.1%} WR | Sharpe {sharpe_s:.2f} — can relax size limits"
                )

        for bucket in report["price_buckets"]:
            if bucket["total"] >= 5 and bucket["win_rate"] < 0.50:
                lessons.append(
                    f"🚫 Price range {bucket['range']}: {bucket['win_rate']:.1%} WR — block this range"
                )
            elif bucket["total"] >= 5 and bucket["win_rate"] >= 0.80:
                lessons.append(
                    f"💡 Price range {bucket['range']}: {bucket['win_rate']:.1%} WR — high edge zone"
                )

        perf = report["hourly_performance"]
        if perf:
            best  = max(perf, key=lambda h: h["pnl"])
            worst = min(perf, key=lambda h: h["pnl"])
            if best["total"] >= 3:
                lessons.append(
                    f"⏰ Best hour: {best['hour_et']:02d}:00 ET "
                    f"({best['win_rate']:.1%} WR, ${best['pnl']:+.0f})"
                )
            if worst["total"] >= 3 and worst["pnl"] < -50:
                lessons.append(
                    f"🌙 Worst hour: {worst['hour_et']:02d}:00 ET "
                    f"(${worst['pnl']:+.0f}) — consider going dark"
                )

        return lessons

    # ── Log file parsing ──────────────────────────────────────────────────────

    def parse_log_errors(self, hours: int = 6) -> list[dict]:
        """Extract warnings and errors from wolf.log for the last N hours."""
        errors = []
        cutoff = time.time() - hours * 3600
        pattern = re.compile(
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[([^\]]+)\] (WARNING|ERROR|CRITICAL) — (.+)"
        )
        try:
            with open(LOG_PATH, "r") as f:
                for line in f:
                    m = pattern.match(line.strip())
                    if not m:
                        continue
                    ts_str, module, level, msg = m.groups()
                    try:
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
                    except Exception:
                        continue
                    if ts < cutoff:
                        continue
                    errors.append({"timestamp": ts_str, "module": module, "level": level, "message": msg})
        except FileNotFoundError:
            pass
        return errors

    # ── Telegram report ───────────────────────────────────────────────────────

    def format_telegram_report(self, hours: int = 24) -> str:
        report  = self.analyze_trades(hours)
        overall = report["overall"]
        strats  = report["by_strategy"]
        lessons = report["lessons"]

        wr_emoji = "🟢" if overall["win_rate"] >= 0.75 else ("🟡" if overall["win_rate"] >= 0.65 else "🔴")
        sharpe = overall.get("sharpe", 0)

        lines = [
            f"🐺 Wolf Performance Report ({hours}h)",
            f"{'─'*30}",
            f"{wr_emoji} Win Rate:  {overall['win_rate']:.1%} ({overall['wins']}/{overall['total_trades']})",
            f"💰 P&L:      ${overall['total_pnl']:+,.2f}",
            f"📊 Trades:   {overall['total_trades']} resolved",
            f"📈 Best:     ${overall['best_trade']:+.2f}  |  Worst: ${overall['worst_trade']:+.2f}",
            f"📐 Sharpe:   {sharpe:.2f}  |  Sortino: {overall.get('sortino', 0):.2f}",
            "",
            "Strategy Breakdown:",
        ]

        for strat, data in strats.items():
            wr_e = "🟢" if data["win_rate"] >= 0.75 else ("🟡" if data["win_rate"] >= 0.65 else "🔴")
            lines.append(
                f"  {wr_e} {strat}: {data['win_rate']:.1%} WR | "
                f"${data['pnl']:+.2f} | {data['total']}t | Sharpe {data.get('sharpe',0):.2f}"
            )

        total_trades = overall["total_trades"]
        if total_trades >= 10 and strats:
            dominant = max(strats.items(), key=lambda x: x[1]["total"])
            dom_pct = dominant[1]["total"] / total_trades
            lines.append("")
            if dom_pct > 0.80:
                lines.append(f"⚠️  Diversity: {dominant[0]} = {dom_pct:.0%} — other strategies underutilized")
            else:
                active = sum(1 for d in strats.values() if d["total"] > 0)
                lines.append(f"✅ Diversity: {active}/{len(strats)} strategies active")

        if lessons:
            lines += ["", "Lessons:"]
            for lesson in lessons[:5]:
                lines.append(f"  {lesson}")

        errors = self.parse_log_errors(hours=6)
        if errors:
            lines += ["", f"⚠️ Recent errors ({len(errors)}):"]
            for e in errors[-3:]:
                lines.append(f"  [{e['level']}] {e['module']}: {e['message'][:60]}")

        return "\n".join(lines)


# Singleton
analyzer = LogAnalyzer()
