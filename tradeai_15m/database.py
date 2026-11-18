import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone

# Railway volume mount path, with a local fallback for development.
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
DB_PATH = Path(VOLUME_PATH) / "tradeai.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    print(f"[DATABASE] SQLite path: {DB_PATH}", flush=True)
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                direction TEXT,
                entry REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT
            )
            """
        )
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        migrations = {
            "opened_at_ms": "INTEGER",
            "closed_at_ms": "INTEGER",
            "realized_pnl": "REAL",
            "exit_price": "REAL",
            "close_note": "TEXT",
            "signal_candle": "TEXT",
        }
        for name, sql_type in migrations.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {sql_type}")
        conn.commit()
    print("[DATABASE] SQLite ready.", flush=True)


def save_signal(symbol, direction, entry, stop_loss, take_profit, signal_candle=None, opened_at_ms=None):
    """Save a trade only after the exchange accepted the order and protection."""
    now_ms = int(opened_at_ms) if opened_at_ms is not None else int(time.time() * 1000)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO signals (
                timestamp, symbol, direction, entry, stop_loss, take_profit,
                status, opened_at_ms, signal_candle
            ) VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
            """,
            (timestamp, symbol, direction, entry, stop_loss, take_profit, now_ms, signal_candle),
        )
        conn.commit()
        return cursor.lastrowid


def was_signal_candle_traded(symbol, signal_candle):
    if not signal_candle:
        return False
    with get_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM signals WHERE symbol = ? AND signal_candle = ? LIMIT 1",
            (symbol, signal_candle),
        ).fetchone() is not None


def get_open_signals():
    """Legacy-compatible open-signal rows: id, symbol, direction, entry, SL, TP."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, direction, entry, stop_loss, take_profit
            FROM signals WHERE status = 'OPEN'
            """
        ).fetchall()
        return [tuple(row) for row in rows]


def get_open_signal_details():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, direction, entry, stop_loss, take_profit,
                   opened_at_ms
            FROM signals WHERE status = 'OPEN'
            ORDER BY opened_at_ms ASC, id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def update_signal_status(signal_id, status):
    """Compatibility helper. Prefer close_signal() for exchange-confirmed closes."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE signals SET status = ? WHERE id = ?",
            (status, signal_id),
        )
        conn.commit()


def close_signal(signal_id, status, realized_pnl=None, exit_price=None,
                 closed_at_ms=None, note=None):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE signals
            SET status = ?, realized_pnl = ?, exit_price = ?,
                closed_at_ms = ?, close_note = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (status, realized_pnl, exit_price, closed_at_ms, note, signal_id),
        )
        conn.commit()


def get_stats():
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        wins = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status = 'WIN'"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status = 'LOSS'"
        ).fetchone()[0]
        return total, wins, losses
