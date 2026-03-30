#!/usr/bin/env python3
"""
Wolf Native Health Monitor — ZERO API COST
Runs as a background Python process alongside Wolf.
No Claude, no OpenClaw, no LLM — pure Python.

Checks every 5 minutes:
- Is Wolf process alive? If not, restart it.
- Is Wolf log updating? If stalled >10min, restart.
- Any CRITICAL errors in log? Alert Jefe directly via Telegram Bot API.

Sends Telegram alerts via direct Bot API call — no tokens, no Claude.
"""
import os, sys, time, subprocess, requests, sqlite3

WOLF_DIR  = '/data/.openclaw/workspace/wolf'
LOG_PATH  = f'{WOLF_DIR}/wolf.log'
DB_PATH   = f'{WOLF_DIR}/wolf_data.db'
WATCH_SH  = f'{WOLF_DIR}/watchdog.sh'
CHECK_SEC = 300   # check every 5 minutes
STALL_SEC = 600   # restart if log silent for 10 minutes
STATE_FILE = '/tmp/wolf_monitor_state.json'

sys.path.insert(0, WOLF_DIR)

def load_creds():
    creds = {}
    for p in ['/data/.openclaw/.env', f'{WOLF_DIR}/.env']:
        if not os.path.exists(p): continue
        for line in open(p).read().splitlines():
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds

def telegram_alert(msg, creds):
    tok = creds.get('TELEGRAM_BOT_TOKEN','')
    cid = creds.get('TELEGRAM_CHAT_ID','')
    if not tok or not cid:
        return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{tok}/sendMessage',
            json={'chat_id': cid, 'text': f'🐺 Wolf Alert\n{msg}'},
            timeout=8,
        )
        return r.ok
    except:
        return False

def is_wolf_running():
    r = subprocess.run(['pgrep','-f','python3.*main.py'], capture_output=True)
    return r.returncode == 0

def log_stalled():
    try:
        age = time.time() - os.path.getmtime(LOG_PATH)
        return age > STALL_SEC
    except:
        return True

def restart_wolf():
    subprocess.run(['pkill','-f','watchdog.sh'], capture_output=True)
    subprocess.run(['pkill','-f','python3.*main.py'], capture_output=True)
    time.sleep(3)
    subprocess.Popen(
        ['bash', WATCH_SH], cwd=WOLF_DIR,
        stdout=open('/tmp/watchdog.log','a'),
        stderr=subprocess.STDOUT,
    )
    time.sleep(6)
    return is_wolf_running()

def get_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), ROUND(SUM(pnl),2) FROM paper_trades WHERE resolved=1')
        total, wins, pnl = c.fetchone()
        conn.close()
        total = total or 0; wins = wins or 0; pnl = pnl or 0.0
        wr = wins/total if total else 0
        return total, wr, pnl
    except:
        return 0, 0, 0.0

def check_critical_errors():
    """Return new CRITICAL lines since last check."""
    if not os.path.exists(LOG_PATH): return []
    try:
        lines = open(LOG_PATH).readlines()[-100:]
        return [l.strip()[:120] for l in lines
                if 'CRITICAL' in l and 'suppress' not in l][-2:]
    except:
        return []

# ── Main loop ─────────────────────────────────────────────────────────────────
print(f'🐺 Wolf native monitor started — checking every {CHECK_SEC//60}min — ZERO API cost')
creds       = load_creds()
restart_count = 0

while True:
    try:
        issues  = []
        actions = []

        if not is_wolf_running():
            issues.append('Wolf process died')
            ok = restart_wolf()
            restart_count += 1
            actions.append(f'⚡ Restarted (attempt #{restart_count}) — {"OK" if ok else "FAILED"}')
            if not ok:
                telegram_alert(f'❌ Wolf restart FAILED (attempt #{restart_count})\nManual check needed.', creds)

        elif log_stalled():
            issues.append(f'Log stalled >{STALL_SEC//60}min')
            ok = restart_wolf()
            restart_count += 1
            actions.append(f'⚡ Restarted (log stall) — {"OK" if ok else "FAILED"}')

        crits = check_critical_errors()
        if crits:
            issues.append(f'{len(crits)} CRITICAL error(s)')

        if issues:
            total, wr, pnl = get_stats()
            msg = f"⚠️ {time.strftime('%I:%M %p ET')}\n"
            for i in issues: msg += f"• {i}\n"
            if actions:
                msg += "\nActions:\n"
                for a in actions: msg += f"  {a}\n"
            if crits:
                msg += "\nCritical errors:\n"
                for e in crits: msg += f"  {e[-80:]}\n"
            msg += f"\n📊 {total} trades | {wr:.1%} WR | ${pnl:+.2f}"
            telegram_alert(msg, creds)
            print(f'[{time.strftime("%H:%M")}] ALERT: {", ".join(issues)}')
        else:
            total, wr, pnl = get_stats()
            print(f'[{time.strftime("%H:%M")}] ✅ Wolf OK | {total}t {wr:.1%}WR ${pnl:+.2f}')

    except Exception as e:
        print(f'[{time.strftime("%H:%M")}] Monitor error: {e}')

    time.sleep(CHECK_SEC)
