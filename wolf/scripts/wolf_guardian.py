"""
wolf_guardian.py — Wolf's self-healing error scanner.

Runs as a background thread inside Wolf's main loop.
Scans wolf.log every 5 minutes for known error patterns,
auto-remediates what's safe, and alerts on what isn't.

DESIGN RULES:
- Never touches open positions or capital directly
- Never restarts Wolf (watchdog.sh owns that)
- Alerts Jefe for anything requiring human decision
- Remediations are logged + committed, not silent
"""

import re
import os
import time
import sqlite3
import threading
import logging
from datetime import datetime
from typing import NamedTuple

logger = logging.getLogger("wolf.guardian")

# ── Error signatures ───────────────────────────────────────────────────────────

class ErrorPattern(NamedTuple):
    name: str
    pattern: str          # regex against log line
    severity: str         # LOW / MEDIUM / HIGH / CRITICAL
    auto_fix: bool        # can we safely fix without human?
    fix_fn: str           # function name in this module (or "")
    description: str

PATTERNS = [
    ErrorPattern(
        # Only fires when slug lookup truly fails (not just retrying) — avoid matching timestamps
        name="price_lookup_fail",
        pattern=r"\[FORCE-EXIT\] Price lookup failed \d+x for ",
        severity="MEDIUM",
        auto_fix=True,
        fix_fn="_fix_price_lookup_fail",
        description="Market price lookup failed 3x — position closed at entry (pnl=$0, void)",
    ),
    ErrorPattern(
        # DB write failures — UNIQUE constraint errors are now handled in code, only real failures reach here
        name="db_write_fail",
        pattern=r"DB update FAILED after 3 attempts",
        severity="HIGH",
        auto_fix=False,
        fix_fn="",
        description="SQLite write failed after 3 retries — check disk space",
    ),
    ErrorPattern(
        # Only fires on actual unhandled strategy exceptions — not Kalshi expected-disabled messages
        name="strategy_exception",
        pattern=r"(ValueError|TypeError|KeyError|AttributeError).*strateg",
        severity="HIGH",
        auto_fix=False,
        fix_fn="",
        description="Unhandled exception in strategy — trade was skipped",
    ),
    ErrorPattern(
        # True rate limit — must be an HTTP response body or explicit error, not a timestamp
        name="api_rate_limit",
        pattern=r"HTTP 429|Too Many Requests|rate limit exceeded",
        severity="MEDIUM",
        auto_fix=False,
        fix_fn="",
        description="API rate limit hit — Wolf will back off automatically",
    ),
    ErrorPattern(
        name="position_cap_full",
        pattern=r"Max open positions reached",
        severity="LOW",
        auto_fix=False,
        fix_fn="",
        description="Position cap full — no new entries until slots free",
    ),
    ErrorPattern(
        name="telegram_fail",
        pattern=r"Telegram send error|TelegramError",
        severity="LOW",
        auto_fix=False,
        fix_fn="",
        description="Telegram alert failed — Wolf continues trading",
    ),
    ErrorPattern(
        name="kill_switch_triggered",
        pattern=r"KILL SWITCH|kill_switch.*triggered",
        severity="CRITICAL",
        auto_fix=False,
        fix_fn="",
        description="Kill switch triggered — trading halted until manual reset",
    ),
    ErrorPattern(
        name="dashboard_push_fail",
        pattern=r"dashboard.*push.*fail|Failed to push",
        severity="LOW",
        auto_fix=False,
        fix_fn="",
        description="Dashboard push failed — Wolf continues, data may lag",
    ),
]

# ── Patterns that are EXPECTED / KNOWN-DISABLED — never alert ────────────────
# Add patterns here for services that are intentionally off (e.g. Kalshi before live)
SUPPRESSED_PATTERNS = {
    "kalshi_down",   # Kalshi intentionally disabled — KALSHI_ENABLED=false
}

# ── State ──────────────────────────────────────────────────────────────────────

_last_scan_pos: int = 0          # byte offset in wolf.log
_scan_interval: int = 300        # seconds between scans
_alert_cooldown: dict = {}       # error_name → last alert unix time
_alert_cooldown_secs: int = 1800 # don't re-alert same error within 30 min
_scan_count: int = 0
_errors_found: list = []          # last scan results


# ── Auto-fix implementations ───────────────────────────────────────────────────

def _fix_price_lookup_fail(match_text: str, config) -> str:
    """Reload slug cache from DB — cheapest fix for stale conditionId→slug map."""
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        import market_resolver
        market_resolver._preload_slugs_from_db()
        return "Slug cache reloaded from DB"
    except Exception as e:
        return f"Slug reload failed: {e}"


# ── DB Integrity Check ────────────────────────────────────────────────────────

_db_check_cooldown: dict = {}
_DB_CHECK_INTERVAL = 1800  # 30 min between same DB alert

def check_db_integrity(config) -> list[dict]:
    """
    Query the database directly for data integrity issues that log scanning misses.
    Returns list of alert dicts (same shape as scan_log results).
    """
    issues = []
    now = time.time()

    try:
        db_path = getattr(config, 'DB_PATH', None)
        if not db_path or not os.path.exists(db_path):
            return []

        conn = sqlite3.connect(db_path, timeout=5)
        c = conn.cursor()

        # ── 1. Strategy/sub_strategy with 0% WR on 5+ real trades ───────────
        rows = c.execute("""
            SELECT COALESCE(sub_strategy, strategy) as track_key,
                   COUNT(*) as t, SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as w
            FROM paper_trades WHERE resolved=1 AND simulated=0
            GROUP BY track_key HAVING t >= 5
        """).fetchall()
        for strat, t, w in rows:
            wr = (w or 0) / t
            if wr < 0.15:
                key = f"zero_wr_{strat}"
                if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                    _db_check_cooldown[key] = now
                    issues.append({
                        "name": f"zero_wr_{strat}",
                        "severity": "HIGH",
                        "description": f"{strat} has {wr:.0%} WR on {t} real trades — strategy may be broken",
                        "count": t,
                        "sample": f"{strat}: {w}/{t} wins ({wr:.0%} WR)",
                        "auto_fix": False,
                        "fix_fn": "",
                        "fixed": False,
                        "fix_result": "",
                    })

        # ── 1b. Rolling last-10-trade WR drop ────────────────────────────────
        strat_keys = c.execute("""
            SELECT DISTINCT COALESCE(sub_strategy, strategy)
            FROM paper_trades WHERE resolved=1 AND simulated=0
        """).fetchall()
        for (track_key,) in strat_keys:
            last10 = c.execute("""
                SELECT won FROM paper_trades
                WHERE resolved=1 AND simulated=0
                AND COALESCE(sub_strategy, strategy)=?
                ORDER BY timestamp DESC LIMIT 10
            """, (track_key,)).fetchall()
            if len(last10) >= 10:
                recent_wr = sum(r[0] for r in last10) / 10
                if recent_wr < 0.40:
                    key = f"rolling_wr_drop_{track_key}"
                    if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                        _db_check_cooldown[key] = now
                        issues.append({
                            "name": f"rolling_wr_drop_{track_key}",
                            "severity": "HIGH",
                            "description": f"{track_key} last-10-trade WR={recent_wr:.0%} — below 40% rolling threshold",
                            "count": 10,
                            "sample": f"{track_key}: {sum(r[0] for r in last10)}/10 wins rolling",
                            "auto_fix": False,
                            "fix_fn": "",
                            "fixed": False,
                            "fix_result": "",
                        })

        # ── 2. High void rate — force exits poisoning stats ───────────────────
        void_row = c.execute("""
            SELECT COUNT(*) as total, SUM(CASE WHEN void=1 THEN 1 ELSE 0 END) as voids
            FROM paper_trades WHERE resolved=1 AND simulated=0
        """).fetchone()
        if void_row and void_row[0] >= 5:
            total, voids = void_row
            void_pct = voids / total
            if void_pct > 0.20:  # >20% void rate is a problem
                key = "high_void_rate"
                if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                    _db_check_cooldown[key] = now
                    issues.append({
                        "name": "high_void_rate",
                        "severity": "HIGH",
                        "description": f"{void_pct:.0%} of real trades are void exits ({voids}/{total}) — price resolution broken for some markets",
                        "count": voids,
                        "sample": f"{voids} void trades out of {total} real resolved",
                        "auto_fix": False,
                        "fix_fn": "",
                        "fixed": False,
                        "fix_result": "",
                    })

        # ── 3. Report using simulated data — simulated vs real ratio check ────
        sim_row = c.execute("""
            SELECT
              SUM(CASE WHEN simulated=0 THEN 1 ELSE 0 END) as real_t,
              SUM(CASE WHEN simulated=1 THEN 1 ELSE 0 END) as sim_t
            FROM paper_trades WHERE resolved=1
        """).fetchone()
        if sim_row:
            real_t, sim_t = sim_row[0] or 0, sim_row[1] or 0
            if sim_t > 0 and real_t < 20:
                key = "simulated_data_dominant"
                if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                    _db_check_cooldown[key] = now
                    issues.append({
                        "name": "simulated_data_dominant",
                        "severity": "HIGH",
                        "description": f"DB has {sim_t} simulated + {real_t} real resolved trades — reports may show inflated WR/PnL",
                        "count": sim_t,
                        "sample": f"real={real_t} simulated={sim_t}",
                        "auto_fix": False,
                        "fix_fn": "",
                        "fixed": False,
                        "fix_result": "",
                    })

        # ── 3b. Stale open positions (>18h) ──────────────────────────────────
        stale = c.execute("""
            SELECT strategy, market_id, timestamp FROM paper_trades
            WHERE resolved=0 AND simulated=0 AND timestamp < ?
        """, (now - 18*3600,)).fetchall()
        if stale:
            key = "open_positions_stale"
            if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                _db_check_cooldown[key] = now
                issues.append({
                    "name": "open_positions_stale",
                    "severity": "HIGH",
                    "description": f"{len(stale)} open position(s) held >18h — force-exit may be imminent",
                    "count": len(stale),
                    "sample": f"{stale[0][0]} {stale[0][1][:20]}",
                    "auto_fix": False,
                    "fix_fn": "",
                    "fixed": False,
                    "fix_result": "",
                })

        # ── 3c. No new resolved trades in 24h ────────────────────────────────
        last_resolved = c.execute("""
            SELECT MAX(timestamp) FROM paper_trades WHERE resolved=1 AND simulated=0
        """).fetchone()[0] or 0
        if last_resolved > 0 and (now - last_resolved) > 86400:
            key = "no_new_trades_24h"
            if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                _db_check_cooldown[key] = now
                hours = (now - last_resolved) / 3600
                issues.append({
                    "name": "no_new_trades_24h",
                    "severity": "MEDIUM",
                    "description": f"No new resolved trades in {hours:.0f}h — Wolf may not be finding markets",
                    "count": 1,
                    "sample": f"Last resolved: {datetime.fromtimestamp(last_resolved).strftime('%Y-%m-%d %H:%M')}",
                    "auto_fix": False,
                    "fix_fn": "",
                    "fixed": False,
                    "fix_result": "",
                })

        # ── 4. Balance sanity check — detect runaway PnL ─────────────────────
        pnl_row = c.execute(
            "SELECT ROUND(SUM(pnl),2) FROM paper_trades WHERE resolved=1 AND simulated=0"
        ).fetchone()
        if pnl_row and pnl_row[0] is not None:
            real_pnl = pnl_row[0]
            starting = getattr(config, 'PAPER_STARTING_CAPITAL', 10000.0)
            balance = starting + real_pnl
            if balance > starting * 20:  # 20x starting capital is suspicious
                key = "runaway_balance"
                if now - _db_check_cooldown.get(key, 0) > _DB_CHECK_INTERVAL:
                    _db_check_cooldown[key] = now
                    issues.append({
                        "name": "runaway_balance",
                        "severity": "CRITICAL",
                        "description": f"Balance ${balance:,.0f} is {balance/starting:.0f}x starting — possible data error",
                        "count": 1,
                        "sample": f"PnL=${real_pnl:+,.2f} balance=${balance:,.2f}",
                        "auto_fix": False,
                        "fix_fn": "",
                        "fixed": False,
                        "fix_result": "",
                    })

        conn.close()

    except Exception as e:
        logger.warning(f"[GUARDIAN] DB integrity check error: {e}")

    return issues


# ── Core scan ─────────────────────────────────────────────────────────────────

def scan_log(log_path: str, config) -> list[dict]:
    """
    Scan new lines in wolf.log since last scan.
    Returns list of {name, severity, count, sample, fixed} dicts.
    """
    global _last_scan_pos

    if not os.path.exists(log_path):
        return []

    results: dict[str, dict] = {}

    try:
        with open(log_path, "rb") as f:
            f.seek(_last_scan_pos)
            chunk = f.read()
            _last_scan_pos = f.tell()

        lines = chunk.decode("utf-8", errors="replace").splitlines()

        for line in lines:
            for ep in PATTERNS:
                if re.search(ep.pattern, line, re.IGNORECASE):
                    if ep.name not in results:
                        results[ep.name] = {
                            "name": ep.name,
                            "severity": ep.severity,
                            "description": ep.description,
                            "count": 0,
                            "sample": line.strip()[-120:],
                            "auto_fix": ep.auto_fix,
                            "fix_fn": ep.fix_fn,
                            "fixed": False,
                            "fix_result": "",
                        }
                    results[ep.name]["count"] += 1

    except Exception as e:
        logger.warning(f"[GUARDIAN] Log scan error: {e}")
        return []

    return list(results.values())


def _should_alert(error_name: str) -> bool:
    # Never alert on suppressed/known-disabled patterns
    if error_name in SUPPRESSED_PATTERNS:
        return False
    now = time.time()
    last = _alert_cooldown.get(error_name, 0)
    if now - last > _alert_cooldown_secs:
        _alert_cooldown[error_name] = now
        return True
    return False


def _run_fix(error: dict, config) -> None:
    fn_name = error.get("fix_fn", "")
    if not fn_name:
        return
    fn = globals().get(fn_name)
    if callable(fn):
        result = fn(error.get("sample", ""), config)
        error["fixed"] = True
        error["fix_result"] = result
        logger.info(f"[GUARDIAN] Auto-fixed '{error['name']}': {result}")


def _build_alert_text(errors: list[dict], config) -> str:
    lines = ["🛡️ <b>Wolf Guardian — Scan Report</b>"]
    mode = "PAPER" if config.PAPER_MODE else "LIVE"
    lines.append(f"Mode: {mode} | {datetime.now().strftime('%H:%M ET')}")
    lines.append("")

    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    errors_sorted = sorted(errors, key=lambda e: sev_order.get(e["severity"], 9))

    for e in errors_sorted:
        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(e["severity"], "⚪")
        lines.append(f"{icon} <b>{e['name']}</b> ({e['count']}x) — {e['description']}")
        if e.get("fixed"):
            lines.append(f"   ✅ Auto-fixed: {e['fix_result']}")
        elif e["severity"] in ("CRITICAL", "HIGH"):
            lines.append(f"   ⚠️ Needs review")

    return "\n".join(lines)


# ── Main guardian loop (runs in thread) ───────────────────────────────────────

def guardian_loop(log_path: str, config) -> None:
    global _scan_count, _errors_found
    logger.info("[GUARDIAN] Started — scanning wolf.log every 5 min")

    # Give Wolf 60s to warm up before first scan
    time.sleep(60)

    while True:
        try:
            errors = scan_log(log_path, config)
            # Run DB integrity check every scan — catches what log scanning misses
            db_issues = check_db_integrity(config)
            # Filter out suppressed/expected-disabled patterns before processing
            errors = [e for e in errors + db_issues if e["name"] not in SUPPRESSED_PATTERNS]
            _scan_count += 1

            if not errors:
                logger.debug(f"[GUARDIAN] Scan #{_scan_count}: clean")
                time.sleep(_scan_interval)
                continue

            _errors_found = errors

            # Run auto-fixes
            for e in errors:
                if e["auto_fix"]:
                    _run_fix(e, config)

            # HIGH/CRITICAL always alert immediately — no cooldown
            # MEDIUM/LOW respect the 30-min cooldown to avoid spam
            alertable = [
                e for e in errors
                if e["severity"] in ("CRITICAL", "HIGH")
                   or (e["severity"] in ("MEDIUM", "LOW") and _should_alert(e["name"]))
            ]

            if alertable:
                try:
                    import sys, os as _os
                    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
                    from alerts.telegram_alerts import send_alert
                    msg = _build_alert_text(alertable, config)
                    send_alert(msg, level="WARNING", system=True)
                    logger.info(f"[GUARDIAN] Alerted on {len(alertable)} error(s)")
                except Exception as ae:
                    logger.warning(f"[GUARDIAN] Alert send failed: {ae}")

            # Always log summary (suppressed patterns already excluded from errors list)
            names = ", ".join(e["name"] for e in errors)
            severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
            for e in errors:
                severity_counts[e.get("severity", "LOW")] = severity_counts.get(e.get("severity","LOW"), 0) + 1
            logger.info(f"[GUARDIAN] Scan #{_scan_count}: {len(errors)} issue(s) — {names}")

        except Exception as ex:
            logger.warning(f"[GUARDIAN] Scan loop exception: {ex}")

        time.sleep(_scan_interval)


def start(log_path: str, config) -> threading.Thread:
    """Spawn guardian as a daemon thread. Call from main.py after Wolf boots."""
    t = threading.Thread(
        target=guardian_loop,
        args=(log_path, config),
        daemon=True,
        name="wolf-guardian",
    )
    t.start()
    return t


# ── Status for audit/dashboard ────────────────────────────────────────────────

def get_status() -> dict:
    return {
        "scan_count": _scan_count,
        "last_errors": _errors_found,
        "error_count": len(_errors_found),
        "healthy": len([e for e in _errors_found if e["severity"] in ("CRITICAL","HIGH")]) == 0,
    }
