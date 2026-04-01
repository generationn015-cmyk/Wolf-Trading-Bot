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
        # Check primary wolf.log first, then fallback to /tmp/watchdog.log
        # (watchdog.log is used when Wolf was launched with nohup redirect)
        paths_to_check = [LOG_PATH, '/tmp/watchdog.log']
        for p in paths_to_check:
            if os.path.exists(p):
                age = time.time() - os.path.getmtime(p)
                if age <= STALL_SEC:
                    return False  # at least one log is fresh
        return True  # all logs stale
    except:
        return True

def restart_wolf():
    # Kill only main.py — watchdog will auto-restart it within 3s
    subprocess.run(['pkill','-f','python3.*main.py'], capture_output=True)
    time.sleep(8)
    return is_wolf_running()

STARTING_CAPITAL = 100.0  # paper starting balance

def get_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), ROUND(SUM(pnl),2) FROM paper_trades WHERE resolved=1 AND simulated=0 AND COALESCE(void,0)=0')
        total, wins, pnl = c.fetchone()
        open_pos = conn.execute('SELECT COUNT(*) FROM paper_trades WHERE resolved=0 AND simulated=0 AND COALESCE(void,0)=0').fetchone()[0]
        conn.close()
        total = total or 0; wins = wins or 0; pnl = float(pnl or 0.0)
        wr = wins/total if total else 0
        balance = STARTING_CAPITAL + pnl
        return total, wr, pnl, balance, open_pos
    except:
        return 0, 0, 0.0, STARTING_CAPITAL, 0

def check_critical_errors():
    """Return new CRITICAL lines since last check."""
    if not os.path.exists(LOG_PATH): return []
    try:
        lines = open(LOG_PATH).readlines()[-100:]
        return [l.strip()[:120] for l in lines
                if 'CRITICAL' in l and 'suppress' not in l][-2:]
    except:
        return []

def wolf_memory_mb():
    """Return Wolf process RSS memory in MB. Returns 0 if not running."""
    try:
        r = subprocess.run(['pgrep','-f','python3.*main.py'], capture_output=True, text=True)
        pid = r.stdout.strip().split()[0]
        rss = open(f'/proc/{pid}/status').read()
        for line in rss.splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) // 1024  # kB → MB
    except:
        pass
    return 0

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

        # Memory guard — proactive restart before kernel OOM kill (exit 137)
        mem_mb = wolf_memory_mb()
        if mem_mb > 800:  # Wolf should never need >800MB; normal is ~70MB
            issues.append(f'High memory: {mem_mb}MB — preemptive restart')
            ok = restart_wolf()
            actions.append(f'⚡ Memory restart — {"OK" if ok else "FAILED"}')

        if issues:
            total, wr, pnl, balance, open_pos = get_stats()
            lines = [f"🐺 Wolf Alert — {time.strftime('%I:%M %p ET')}"]
            lines.append("─────────────────────")
            for i in issues: lines.append(f"⚠️ {i}")
            if actions:
                for a in actions: lines.append(f"✅ {a}")
            if crits:
                lines.append("")
                lines.append("🚨 Critical:")
                for e in crits: lines.append(f"  {e[-80:]}")
            lines.append("")
            lines.append(f"📊 Trades: {total} | WR: {wr:.1%}")
            lines.append(f"💰 P&L: ${pnl:+.2f} | Balance: ${balance:,.2f}")
            lines.append(f"📈 Started: ${STARTING_CAPITAL:,.0f} | Open: {open_pos}")
            telegram_alert("\n".join(lines), creds)
            print(f'[{time.strftime("%H:%M")}] ALERT: {", ".join(issues)}')
        else:
            total, wr, pnl, balance, open_pos = get_stats()
            print(f'[{time.strftime("%H:%M")}] ✅ Wolf OK | {total}t {wr:.1%}WR ${pnl:+.2f} Bal:${balance:,.0f} Open:{open_pos}')

    except Exception as e:
        print(f'[{time.strftime("%H:%M")}] Monitor error: {e}')

    time.sleep(CHECK_SEC)
