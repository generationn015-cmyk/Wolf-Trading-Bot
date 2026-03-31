#!/usr/bin/env python3
"""
quarantine_tainted_data.py — Move corrupted/simulated/void trades out of paper_trades.

Idempotent: safe to run multiple times. Only moves rows not already quarantined.

Quarantine reasons:
  - simulated=1  → bootstrap/backtest data, never from live markets
  - void=1       → force-exited at entry price, $0 PnL, unreliable resolution

These rows MUST NOT influence Wolf's WR stats, learning engine, or gate calculations.
"""
import sys, os, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB = config.DB_PATH

def run():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ── 1. Create quarantine table if not exists ──────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS quarantine (
            id              INTEGER PRIMARY KEY,
            strategy        TEXT,
            venue           TEXT,
            market_id       TEXT,
            side            TEXT,
            size            REAL,
            entry_price     REAL,
            exit_price      REAL,
            pnl             REAL,
            won             INTEGER,
            resolved        INTEGER,
            simulated       INTEGER,
            void            INTEGER,
            confidence      REAL,
            reason          TEXT,
            slug            TEXT,
            timestamp       REAL,
            market_end      REAL,
            days_to_expiry  REAL,
            quarantine_reason TEXT,
            quarantined_at  REAL
        )
    """)

    # ── 2. Get columns in paper_trades ────────────────────────────────────────
    pt_cols = [row[1] for row in c.execute("PRAGMA table_info(paper_trades)").fetchall()]

    # ── 3. Identify tainted rows ──────────────────────────────────────────────
    tainted = c.execute("""
        SELECT * FROM paper_trades
        WHERE simulated=1 OR void=1
    """).fetchall()

    if not tainted:
        print("✅ No tainted rows found — paper_trades is already clean.")
        conn.close()
        return

    print(f"Found {len(tainted)} tainted row(s) to quarantine...")

    # Already-quarantined IDs (avoid duplicates)
    existing_ids = set(row[0] for row in c.execute("SELECT id FROM quarantine").fetchall())

    moved = 0
    skipped = 0
    now = time.time()

    for row in tainted:
        row_id = row['id'] if 'id' in row.keys() else row[0]
        if row_id in existing_ids:
            skipped += 1
            continue

        # Determine quarantine reason
        reasons = []
        if row['simulated']:
            reasons.append('simulated_bootstrap')
        if row['void']:
            reasons.append('void_force_exit')
        q_reason = '+'.join(reasons)

        # Build insert using quarantine table columns
        q_cols = ['strategy','venue','market_id','side','size','entry_price',
                  'exit_price','pnl','won','resolved','simulated','void',
                  'confidence','reason','slug','timestamp','market_end',
                  'days_to_expiry','quarantine_reason','quarantined_at']

        def safe(col, default=None):
            try:
                return row[col]
            except (IndexError, KeyError):
                return default

        vals = (
            safe('strategy'), safe('venue'), safe('market_id'), safe('side'),
            safe('size'), safe('entry_price'), safe('exit_price'), safe('pnl'),
            safe('won'), safe('resolved'), safe('simulated'), safe('void'),
            safe('confidence'), safe('reason'), safe('slug'), safe('timestamp'),
            safe('market_end'), safe('days_to_expiry'),
            q_reason, now,
        )

        c.execute(f"""
            INSERT INTO quarantine
            (strategy,venue,market_id,side,size,entry_price,exit_price,pnl,
             won,resolved,simulated,void,confidence,reason,slug,timestamp,
             market_end,days_to_expiry,quarantine_reason,quarantined_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, vals)
        moved += 1

    # ── 4. Delete tainted rows from paper_trades ──────────────────────────────
    if moved > 0:
        c.execute("DELETE FROM paper_trades WHERE simulated=1 OR void=1")
        conn.commit()

    # ── 5. Verify ─────────────────────────────────────────────────────────────
    remaining = c.execute(
        "SELECT simulated, void, COUNT(*) FROM paper_trades WHERE resolved=1 GROUP BY simulated, void"
    ).fetchall()
    quarantine_count = c.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
    clean_count = c.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE resolved=1"
    ).fetchone()[0]

    conn.close()

    print(f"✅ Quarantined: {moved} rows | Skipped (already quarantined): {skipped}")
    print(f"📦 Quarantine table total: {quarantine_count} rows")
    print(f"🟢 Clean paper_trades resolved: {clean_count} rows")
    print(f"   Breakdown: {remaining}")

if __name__ == '__main__':
    run()
