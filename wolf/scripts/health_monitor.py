#!/usr/bin/env python3
"""
Wolf Overnight Health Monitor
Runs every 30 min via cron. Checks Wolf process, detects stalls/errors,
auto-restarts if needed, alerts Jefe if anything is wrong.
"""
import sys, os, time, sqlite3, subprocess, requests

sys.path.insert(0, '/data/.openclaw/workspace/wolf')
WOLF_DIR  = '/data/.openclaw/workspace/wolf'
LOG_PATH  = f'{WOLF_DIR}/wolf.log'
DB_PATH   = f'{WOLF_DIR}/wolf_data.db'
WATCH_SH  = f'{WOLF_DIR}/watchdog.sh'

def load_env():
    creds = {}
    for p in ['/data/.openclaw/.env', f'{WOLF_DIR}/.env']:
        if os.path.exists(p):
            for line in open(p).read().splitlines():
                if '=' in line and not line.startswith('#'):
                    k, _, v = line.partition('=')
                    creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds

def send_alert(msg, creds):
    tok = creds.get('TELEGRAM_BOT_TOKEN','')
    cid = creds.get('TELEGRAM_CHAT_ID','')
    if not tok or not cid:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{tok}/sendMessage',
            json={'chat_id': cid, 'text': f'🐺 Wolf Monitor\n{msg}'},
            timeout=8,
        )
    except Exception:
        pass

def is_wolf_running():
    r = subprocess.run(['pgrep','-f','python3.*main.py'], capture_output=True)
    return r.returncode == 0

def get_last_log_age_secs():
    """How many seconds since last Wolf log line."""
    try:
        mtime = os.path.getmtime(LOG_PATH)
        return time.time() - mtime
    except:
        return 9999

def get_trade_count():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM paper_trades WHERE resolved=1')
        n = c.fetchone()[0]
        conn.close()
        return n
    except:
        return -1

def restart_wolf():
    subprocess.run(['pkill','-f','watchdog.sh'], capture_output=True)
    subprocess.run(['pkill','-f','python3.*main.py'], capture_output=True)
    time.sleep(3)
    subprocess.Popen(
        ['bash', WATCH_SH],
        cwd=WOLF_DIR,
        stdout=open('/tmp/watchdog.log','a'),
        stderr=subprocess.STDOUT,
    )
    time.sleep(5)
    return is_wolf_running()

def check_recent_errors():
    """Return last ERROR/CRITICAL lines from log in past 30 min."""
    if not os.path.exists(LOG_PATH):
        return []
    cutoff = time.time() - 1800
    errors = []
    try:
        lines = open(LOG_PATH).readlines()[-200:]
        for line in lines:
            if ('ERROR' in line or 'CRITICAL' in line) and 'suppress' not in line and 'Whale' not in line:
                errors.append(line.strip()[:120])
    except:
        pass
    return errors[-3:]

# ── Main check ────────────────────────────────────────────────────────────────
creds   = load_env()
issues  = []
actions = []

# 1. Is Wolf running?
if not is_wolf_running():
    issues.append('Wolf process not found')
    ok = restart_wolf()
    if ok:
        actions.append('⚡ Auto-restarted Wolf — now running')
    else:
        issues.append('RESTART FAILED — manual intervention needed')

# 2. Log stalled? (no output in >10 min)
log_age = get_last_log_age_secs()
if log_age > 600 and is_wolf_running():
    issues.append(f'Log stalled — no output in {log_age/60:.0f} min')
    ok = restart_wolf()
    actions.append('⚡ Restarted (log stall)' if ok else '❌ Restart failed')

# 3. Recent errors
errors = check_recent_errors()
if errors:
    issues.append(f'{len(errors)} recent error(s) in log')

# 4. Trade count sanity
trades = get_trade_count()

# ── Report ────────────────────────────────────────────────────────────────────
if issues:
    msg = f"⚠️ Issues detected at {time.strftime('%I:%M %p ET')}:\n"
    for i in issues: msg += f"  • {i}\n"
    if actions:
        msg += "\nActions taken:\n"
        for a in actions: msg += f"  {a}\n"
    if errors:
        msg += "\nLast errors:\n"
        for e in errors: msg += f"  {e[-80:]}\n"
    msg += f"\nTrades so far: {trades}"
    send_alert(msg, creds)
    print(msg)
else:
    status = f"✅ All clear @ {time.strftime('%I:%M %p ET')} — Wolf running | {trades} trades"
    print(status)

sys.exit(0)
