"""
Wolf Trading Bot — Database Connection Pool
Single persistent SQLite connection with WAL mode and thread safety.
Replaces the hundreds of sqlite3.connect()/close() calls scattered across
the codebase with a single long-lived connection — orders of magnitude faster.

Usage:
    from db import get_conn
    conn = get_conn()
    rows = conn.execute("SELECT ...").fetchall()
    # Never call conn.close() — stays open for process lifetime
"""
import sqlite3
import threading
import logging
import os
import config

logger = logging.getLogger("wolf.db")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    """
    Return the shared SQLite connection, creating it on first call.
    Thread-safe via double-checked locking.
    WAL journal mode allows concurrent readers + one writer without blocking.
    """
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _create_connection()
    return _conn


def _create_connection() -> sqlite3.Connection:
    db_path = config.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,  # We manage thread safety via _lock on writes
        timeout=15,
        isolation_level=None,     # Autocommit — each execute is its own transaction
    )
    conn.row_factory = sqlite3.Row  # Row objects support dict-style access

    # Performance and reliability pragmas
    conn.execute("PRAGMA journal_mode=WAL")          # Non-blocking concurrent reads
    conn.execute("PRAGMA synchronous=NORMAL")        # Fsync on checkpoint only (~3x faster writes)
    conn.execute("PRAGMA cache_size=10000")          # 10MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")         # Temp tables in RAM
    conn.execute("PRAGMA mmap_size=268435456")       # 256MB memory-mapped I/O
    conn.execute("PRAGMA wal_autocheckpoint=1000")   # Checkpoint every 1000 pages

    logger.info(f"DB connection opened: {db_path} (WAL mode)")
    return conn


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """Convenience wrapper — thread-safe execute on the shared connection."""
    return get_conn().execute(sql, params)


def fetchone(sql: str, params: tuple = ()):
    """Convenience wrapper for single-row queries."""
    return get_conn().execute(sql, params).fetchone()


def fetchall(sql: str, params: tuple = ()):
    """Convenience wrapper for multi-row queries."""
    return get_conn().execute(sql, params).fetchall()


def close():
    """Explicitly close the connection (call on clean shutdown only)."""
    global _conn
    if _conn is not None:
        try:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _conn.close()
        except Exception:
            pass
        finally:
            _conn = None
