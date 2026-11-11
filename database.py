DB_PATH = '/app/data/tradeai.db'
from datetime import datetime

DB_PATH = '/app/data/tradeai.db'

def init_db():
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