import os
import sqlite3
from pathlib import Path
from datetime import datetime

# Railway сам подставляет путь сюда, если диск подключен
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
DB_PATH = Path(VOLUME_PATH) / "tradeai.db"

def get_connection():
    # Создаем папку, если ее нет
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # Проверяем, можем ли мы реально писать в эту папку
    test_file = DB_PATH.parent / ".write_test"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as error:
        raise RuntimeError(
            f"Нет доступа на запись в папку базы: {DB_PATH.parent}. Причина: {error}"
        ) from error

    # Подключаемся к базе
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def init_db():
    print(f"[DATABASE] Путь к базе: {DB_PATH}", flush=True)
    print(f"[DATABASE] Папка существует: {DB_PATH.parent.exists()}", flush=True)
    
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
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
        ''')
        conn.commit()
    print("[DATABASE] SQLite успешно подключена.", flush=True)

def save_signal(symbol, direction, entry, stop_loss, take_profit):
    with get_connection() as conn:
        cursor = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO signals (timestamp, symbol, direction, entry, stop_loss, take_profit, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (timestamp, symbol, direction, entry, stop_loss, take_profit, "OPEN"))
        conn.commit()

def get_open_signals():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, direction, entry, stop_loss, take_profit FROM signals WHERE status = 'OPEN'")
        signals = cursor.fetchall()
        return signals

def update_signal_status(signal_id, status):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))
        conn.commit()

def get_stats():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM signals")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'WIN'")
        wins = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'LOSS'")
        losses = cursor.fetchone()[0]
        
        return total, wins, losses