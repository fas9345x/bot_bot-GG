import sqlite3
import os
from datetime import datetime

DB_DIR = '/app/data'
DB_PATH = os.path.join(DB_DIR, 'tradeai.db')

def init_db():
    # Создаем папку, если ее нет, и даем полные права
    os.makedirs(DB_DIR, exist_ok=True)
    try:
        os.chmod(DB_DIR, 0o777)
    except Exception as e:
        print(f"Не удалось изменить права: {e}")

    conn = sqlite3.connect(DB_PATH)
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
    conn.close()

def save_signal(symbol, direction, entry, stop_loss, take_profit):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT INTO signals (timestamp, symbol, direction, entry, stop_loss, take_profit, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (timestamp, symbol, direction, entry, stop_loss, take_profit, "OPEN"))
    conn.commit()
    conn.close()

def get_open_signals():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, symbol, direction, entry, stop_loss, take_profit FROM signals WHERE status = 'OPEN'")
    signals = cursor.fetchall()
    conn.close()
    return signals

def update_signal_status(signal_id, status):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM signals")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'WIN'")
    wins = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'LOSS'")
    losses = cursor.fetchone()[0]
    conn.close()
    return total, wins, losses