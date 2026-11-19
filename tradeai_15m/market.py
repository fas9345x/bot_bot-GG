"""Public Bybit linear candles; never return a forming or stale last candle."""
import logging
import os
import time

import numpy as np
import pandas as pd
import requests
from bybit_guard import RateLimitCooldown, call_bybit, cooldown_remaining

logger = logging.getLogger(__name__)


def _is_rate_limited_response(response):
    status = getattr(response, "status_code", None)
    if status == 429:
        return True
    if status not in (None, 200):
        return False
    try:
        return str(response.json().get("retCode", "")) == "10006"
    except (ValueError, AttributeError):
        return False


def _is_testnet():
    value = os.getenv("BYBIT_TESTNET", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def get_klines(symbol="BTCUSDT", interval="15", limit=250):
    symbol = str(symbol).upper()
    interval = str(interval)
    try:
        minutes = int(interval)
        count = int(limit)
        if minutes not in {1, 3, 5, 15, 30, 60, 120, 240, 360, 720}:
            raise ValueError("Unsupported numeric interval")
        if not 1 <= count <= 1000:
            raise ValueError("Bybit limit must be between 1 and 1000")
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid candle request for %s: %s", symbol, exc)
        return None

    # Read prices from the same environment in which orders are placed.
    host = "https://api-testnet.bybit.com" if _is_testnet() else "https://api.bybit.com"
    if cooldown_remaining() > 0:
        logger.info("Skipping candle request for %s during Bybit rate-limit cooldown", symbol)
        return None
    try:
        response = call_bybit(
            lambda: requests.get(
                f"{host}/v5/market/kline",
                params={"category": "linear", "symbol": symbol, "interval": interval, "limit": count},
                headers={"User-Agent": "TradeAI/15m"},
                timeout=15,
            ),
            action=f"get_klines:{symbol}",
            response_checker=_is_rate_limited_response,
        )
        response.raise_for_status()
        data = response.json()
        if str(data.get("retCode")) != "0":
            raise ValueError(f"Bybit retCode={data.get('retCode')}: {data.get('retMsg', '')}")
        rows = data["result"]["list"]
        if not rows:
            raise ValueError("No candles returned")
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        for column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        if df.isna().any().any() or not np.isfinite(df.to_numpy(dtype=float)).all():
            raise ValueError("Missing or invalid candle values")
        df = df.sort_values("timestamp").reset_index(drop=True)
        interval_ms = minutes * 60_000
        # Server response time keeps candle closure independent of local clock skew.
        now_ms = int(data["time"])
        if abs(int(time.time() * 1000) - now_ms) > 120_000:
            raise ValueError("Exchange response is stale or system clock is inaccurate")
        df = df[df["timestamp"] + interval_ms <= now_ms].copy()
        if df.empty:
            raise ValueError("No fully closed candles")
        if df["timestamp"].duplicated().any():
            raise ValueError("Duplicate candle timestamps")
        if (df["timestamp"].diff().dropna() != interval_ms).any():
            raise ValueError("Gaps in candle history")
        if now_ms - (int(df["timestamp"].iloc[-1]) + interval_ms) >= interval_ms + 30_000:
            raise ValueError("Latest candle is stale")
        bad_prices = ((df[["open", "high", "low", "close"]] <= 0).any(axis=1)
                      | (df["low"] > df[["open", "close"]].min(axis=1))
                      | (df["high"] < df[["open", "close"]].max(axis=1))
                      | (df["high"] < df["low"])
                      | (df["volume"] < 0))
        if bad_prices.any():
            raise ValueError("Invalid OHLCV data")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.reset_index(drop=True)
    except (RateLimitCooldown, requests.RequestException, KeyError, TypeError, ValueError, OverflowError) as exc:
        logger.warning("Candle request failed for %s: %s", symbol, exc)
        return None
