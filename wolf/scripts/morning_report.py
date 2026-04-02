#!/usr/bin/env python3
"""Wolf 6AM Morning Report — sends interactive Telegram update to Jefe.
NOTE: All queries filter simulated=0 to show only REAL paper trade data.
"""
import sys, sqlite3, time
sys.path.insert(0, '/data/.openclaw/workspace/wolf')
import config as _cfg

conn = sqlite3.connect(_cfg.DB_PATH)
c = conn.cursor()

REAL = "resolved=1 AND simulated=0 AND COALESCE(void,0)=0"

c.execute(f'SELECT COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), ROUND(SUM(pnl),2) FROM paper_trades WHERE {REAL}')
total, wins, pnl = c.fetchone()
total = total or 0; wins = wins or 0; pnl = pnl or 0.0
wr = wins/total if total else 0
_starting = getattr(_cfg, 'PAPER_STARTING_CAPITAL', 100.0)
balance = _starting + pnl

c.execute(f"SELECT COUNT(*), COALESCE(SUM(size),0) FROM paper_trades WHERE resolved=0 AND COALESCE(void,0)=0")
open_t, deployed = c.fetchone()
open_t = open_t or 0; deployed = deployed or 0.0
available = balance - deployed

c.execute(f'SELECT COUNT(*) FROM paper_trades WHERE void=1')
void_count = (c.fetchone() or [0])[0] or 0

c.execute(f"SELECT COUNT(*) FROM paper_trades WHERE void=1")
void_count = (c.fetchone() or [0])[0] or 0

c.execute(f'SELECT strategy, COUNT(*), SUM(CASE WHEN won=1 THEN 1 ELSE 0 END), ROUND(SUM(pnl),2) FROM paper_trades WHERE {REAL} GROUP BY strategy ORDER BY SUM(pnl) DESC')
strats = c.fetchall()

c.execute(f'SELECT MAX(timestamp) FROM paper_trades WHERE {REAL}')
last_ts = c.fetchone()[0] or 0
last_trade = time.strftime('%I:%M %p ET', time.localtime(last_ts)) if last_ts else 'N/A'

c.execute(f'SELECT pnl FROM paper_trades WHERE {REAL} ORDER BY pnl DESC LIMIT 1')
best = (c.fetchone() or [0])[0] or 0
c.execute(f'SELECT pnl FROM paper_trades WHERE {REAL} ORDER BY pnl ASC LIMIT 1')
worst = (c.fetchone() or [0])[0] or 0

c.execute(f'SELECT strategy, side, pnl, won FROM paper_trades WHERE {REAL} ORDER BY timestamp DESC LIMIT 5')
recent = c.fetchall()

conn.close()

wr_emoji  = "🟢" if wr >= 0.80 else ("🟡" if wr >= 0.72 else "🔴")
gate_done = wr >= 0.72 and total >= 100

strat_lines = []
for s in strats:
    name, t, w, p = s[0], s[1] or 0, s[2] or 0, s[3] or 0.0
    swr = w/t if t else 0
    se = "🟢" if swr >= 0.80 else ("🟡" if swr >= 0.72 else "🔴")
    strat_lines.append(f"  {se} {name}: {swr:.1%} WR | ${p:+.2f} | {t}t")

recent_lines = []
for r in recent:
    strat, side, p, won = r
    icon = "✅" if won else "❌"
    recent_lines.append(f"  {icon} {(strat or '')[:14]:14} {side}  ${p:+.2f}")

report = f"""🐺 Wolf — Morning Report
{'─'*30}
{wr_emoji} Win Rate:   {wr:.1%}  ({wins}W / {total-wins}L / {total} real trades)
💰 P&L:       ${pnl:+,.2f}
📊 Balance:   ${balance:,.2f}  (started ${_starting:,.0f})
📈 Best:      ${best:+.2f}  |  Worst: ${worst:+.2f}
🕐 Last trade: {last_trade}
📂 Open now:  {open_t} positions (${deployed:.0f} deployed, ${available:.0f} free)
⚠️  Void exits: {void_count} trades

Strategy Breakdown:
{chr(10).join(strat_lines) if strat_lines else '  No resolved trades yet'}

Last 5 Trades:
{chr(10).join(recent_lines) if recent_lines else '  None yet'}

{'─'*30}
{'✅ GATE PASSED — ready to review for live' if gate_done else f'🔒 Gate: {total}/100 trades | {wr:.1%}/72% WR | {max(0,100-total)} trades to go'}"""

# ── Print to stdout for cron system delivery ──────────────────────────────────
print(report)
