"""Download public Bybit linear 15-minute bars, with no account or orders.

Example: python download_history.py --days 90 --out historical_data
Data is MAINNET public market history, regardless of the bot's Testnet setting.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from datetime import datetime, timezone
import time

import pandas as pd
import requests

URL = "https://api.bybit.com/v5/market/kline"
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
BAR_MS = 15 * 60 * 1000


def download(symbol, days, out, end_ms):
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    cursor = end_ms
    rows = []
    while cursor >= start_ms:
        params = {"category": "linear", "symbol": symbol, "interval": "15", "limit": 1000, "end": cursor, "start": start_ms}
        for attempt in range(4):
            try:
                r = requests.get(URL, params=params, timeout=30)
                r.raise_for_status()
                obj = r.json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        if obj.get("retCode") != 0:
            raise RuntimeError(f"Bybit returned {obj.get('retCode')}: {obj.get('retMsg')}")
        page = obj["result"]["list"]
        if not page:
            break
        rows.extend(page)
        earliest = min(int(row[0]) for row in page)
        if earliest > cursor:
            raise RuntimeError("Pagination did not advance")
        cursor = earliest - 1
    df = pd.DataFrame(rows, columns=COLUMNS)
    for column in COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="raise")
    df = df[(df.timestamp >= start_ms) & (df.timestamp + BAR_MS <= end_ms)]
    df = df.sort_values("timestamp").reset_index(drop=True)
    if df.empty or df.timestamp.duplicated().any() or (df.timestamp.diff().dropna() != BAR_MS).any():
        raise RuntimeError(f"{symbol}: no data, duplicate bars or gaps")
    df["timestamp"] = pd.to_datetime(df.timestamp, unit="ms", utc=True)
    path = out / f"{symbol}_15m.csv"
    df.to_csv(path, index=False)
    print(f"{symbol}: {len(df)} closed candles, {df.timestamp.iloc[0]} – {df.timestamp.iloc[-1]}", flush=True)
    return {"symbol": symbol, "rows": len(df), "first_bar_utc": str(df.timestamp.iloc[0]), "last_bar_utc": str(df.timestamp.iloc[-1]), "file": path.name}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--out", default="historical_data")
    parser.add_argument("--symbols", nargs="+", default=["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"])
    args = parser.parse_args()
    if not 1 <= args.days <= 3650:
        parser.error("days must be 1..3650")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc)
    end_ms = int(end.timestamp() * 1000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda s: download(s, args.days, out, end_ms), args.symbols))
    manifest = {"source": URL, "retrieved_at_utc": end.isoformat(), "venue": "Bybit mainnet linear USDT perpetual", "interval": "15", "days_requested": args.days, "records": records}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
