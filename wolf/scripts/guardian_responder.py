"""
Wolf Guardian Auto-Responder
Monitors wolf_guardian_alerts.json and takes automated corrective action.
Runs as a background thread inside main.py.
"""
import json, os, time, logging, sqlite3
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger("wolf.guardian_responder")

ALERT_FILE   = os.path.join(os.path.dirname(config.DB_PATH), "guardian_alerts.json")
HANDLED_FILE = os.path.join(os.path.dirname(config.DB_PATH), "guardian_handled.json")
CHECK_INTERVAL = 60  # seconds

def _load_json(path):
    try:
        return json.loads(open(path).read()) if os.path.exists(path) else {}
    except Exception:
        return {}

def _save_json(path, data):
    open(path, 'w').write(json.dumps(data, indent=2))

def _void_stale_positions():
    """Void any open positions in expired markets."""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        voided = conn.execute('''
            UPDATE paper_trades SET resolved=1, void=1, pnl=0, exit_price=entry_price, won=0
            WHERE resolved=0 AND simulated=0 AND void=0
            AND market_end > 0 AND market_end < ?
        ''', (time.time(),)).rowcount
        conn.commit()
        conn.close()
        if voided:
            logger.info(f"[AUTO-HEAL] Voided {voided} expired-market positions")
        return voided
    except Exception as e:
        logger.warning(f"[AUTO-HEAL] void_stale error: {e}")
        return 0

def _clear_bad_learning_ranges():
    """Clear bad_price_ranges from learning state if they're covering too much of the price spectrum."""
    try:
        state_path = os.path.join(os.path.dirname(config.DB_PATH), 'learning_state.json')
        if not os.path.exists(state_path):
            return
        state = json.loads(open(state_path).read())
        ranges = state.get('bad_ranges', [])
        # If bad ranges cover more than 20% of the 0.05-0.95 spectrum, they're over-fit — clear them
        total_coverage = sum(abs(h - l) for l, h in ranges)
        if total_coverage > 0.18:  # > 18% of spectrum blocked
            state['bad_ranges'] = []
            open(state_path,'w').write(json.dumps(state, indent=2))
            logger.info(f"[AUTO-HEAL] Cleared {len(ranges)} over-fit bad_price_ranges (coverage={total_coverage:.2f})")
    except Exception as e:
        logger.warning(f"[AUTO-HEAL] clear_bad_ranges error: {e}")

def _check_open_position_count():
    """If open positions are 0 for extended period, log a diagnostic."""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        count = conn.execute(
            'SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND simulated=0 AND void=0'
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return -1

def run_responder_loop():
    """Main loop — run as a daemon thread."""
    logger.info("[AUTO-HEAL] Guardian responder started")
    last_healing = 0

    while True:
        try:
            now = time.time()
            # Run healing sweep every CHECK_INTERVAL seconds
            if now - last_healing >= CHECK_INTERVAL:
                last_healing = now
                # 1. Void any expired-market positions
                _void_stale_positions()
                # 2. Clear over-fitted learning ranges
                _clear_bad_learning_ranges()
                # 3. Log position count for visibility
                open_count = _check_open_position_count()
                if open_count == 0:
                    logger.debug("[AUTO-HEAL] 0 open positions — strategies scanning for new entries")
        except Exception as e:
            logger.warning(f"[AUTO-HEAL] Responder loop error: {e}")

        time.sleep(30)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_responder_loop()
