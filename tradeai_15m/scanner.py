"""Causal breakout96 signals and indicators for closed 15-minute candles.

The caller must supply *closed* candles in chronological order; this module
never looks at the clock or fetches market data. A different amount of past
history changes the initialisation of exponentially smoothed indicators, so
use the same history when comparing a live signal with a backtest signal.

Warning: the selected strategy is not profitable in this research: median
training return was -1.02%, and all four exploratory OOS returns were negative.
The OOS period was not a pristine holdout because an earlier candidate's OOS
had already been examined. Historical results do not establish future returns.
"""

import numpy as np
import pandas as pd


WARMUP_BARS = 97
ATR_MULTIPLIER = 2.0
REWARD_MULTIPLE = 2.0


def _wilder_average(values, window, start=0):
    """Wilder smoothing seeded with the arithmetic mean of the first window."""
    result = pd.Series(np.nan, index=values.index, dtype=float)
    seed_index = start + window - 1
    if len(values) <= seed_index:
        return result
    seed_and_rest = values.astype(float).copy()
    seed_and_rest.iloc[:seed_index] = np.nan
    seed_and_rest.iloc[seed_index] = values.iloc[start:seed_index + 1].mean()
    return seed_and_rest.ewm(alpha=1.0 / window, adjust=False).mean()


def _levels_from_values(direction, close, atr):
    """Compute the same close-based levels for scalars or pandas Series."""
    risk = ATR_MULTIPLIER * atr
    if direction == "LONG":
        return close - risk, close + REWARD_MULTIPLE * risk
    if direction == "SHORT":
        return close + risk, close - REWARD_MULTIPLE * risk
    raise ValueError("direction must be LONG or SHORT")


def _strategy_frame(df):
    """Compute the entire strategy once; row i uses only rows <= i.

    Required columns are high, low, close; open is checked when present.
    Prices must be finite, positive, and form valid OHLC candles. If a timestamp
    column or a DatetimeIndex is supplied, it must be strictly increasing.
    Missing time intervals are not filled or inferred. The returned frame has a
    new integer index and is independent of the input.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    if df.empty:
        return pd.DataFrame(columns=["high", "low", "close", "ema20", "ema50",
                                     "rsi", "atr", "direction"])
    required = {"high", "low", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")

    if "timestamp" in df.columns:
        timestamps = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        if timestamps.isna().any() or not timestamps.is_monotonic_increasing or timestamps.duplicated().any():
            raise ValueError("timestamps must be valid, unique and strictly increasing")
    elif isinstance(df.index, pd.DatetimeIndex):
        if df.index.hasnans or not df.index.is_monotonic_increasing or df.index.has_duplicates:
            raise ValueError("datetime index must be valid, unique and strictly increasing")

    columns = ["high", "low", "close"] + (["open"] if "open" in df.columns else [])
    frame = df.loc[:, columns].apply(pd.to_numeric, errors="coerce").reset_index(drop=True)
    values = frame.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("OHLC prices must be finite and positive")
    if ((frame["high"] < frame["low"]) | (frame["high"] < frame["close"]) |
            (frame["low"] > frame["close"])).any():
        raise ValueError("invalid high/low/close values")
    if "open" in frame and ((frame["open"] > frame["high"]) |
                            (frame["open"] < frame["low"])).any():
        raise ValueError("open must lie between low and high")

    close = frame["close"]
    frame["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    frame["ema50"] = close.ewm(span=50, adjust=False, min_periods=50).mean()
    delta = close.diff()
    gain = _wilder_average(delta.clip(lower=0), 14, start=1)
    loss = _wilder_average(-delta.clip(upper=0), 14, start=1)
    # Wilder RSI: 100 if there are gains but no losses; 50 if both are zero.
    rsi = 100.0 - 100.0 / (1.0 + gain / loss)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    frame["rsi"] = rsi
    previous_close = close.shift(1)
    true_range = pd.concat([frame["high"] - frame["low"],
                            (frame["high"] - previous_close).abs(),
                            (frame["low"] - previous_close).abs()], axis=1).max(axis=1)
    frame["atr"] = _wilder_average(true_range, 14)

    # shift(1) excludes the signal candle; rolling(96) requires all 96 prior
    # bars and includes the oldest of them. EMA/RSI are not signal filters.
    previous_high = frame["high"].shift(1).rolling(96).max()
    previous_low = frame["low"].shift(1).rolling(96).min()
    ready = ((np.arange(len(frame)) >= WARMUP_BARS - 1) &
             np.isfinite(frame["atr"]) & (frame["atr"] > 0))
    long_stop, long_target = _levels_from_values("LONG", close, frame["atr"])
    short_stop, short_target = _levels_from_values("SHORT", close, frame["atr"])
    long_ok = (ready & (close > previous_high) & np.isfinite(long_stop) &
               np.isfinite(long_target) & (long_stop > 0) & (long_target > 0))
    short_ok = (ready & (close < previous_low) & np.isfinite(short_stop) &
                np.isfinite(short_target) & (short_stop > 0) & (short_target > 0))
    frame["direction"] = np.select([long_ok, short_ok], ["LONG", "SHORT"], default="НЕТ")
    return frame


def analyze_trend(df):
    """Return (direction, close, RSI14, ATR14) for the last closed candle.

    Designed for 15-minute OHLC data. LONG requires a close strictly above
    the highest high of the preceding 96 closed candles; SHORT requires a close
    strictly below their lowest low. The current candle is not in the channel,
    so at least 97 candles are needed. RSI14 remains in the return tuple for
    display, but neither RSI nor EMA filters signals. Wilder RSI and ATR use
    an initial 14-observation SMA; ATR's first true range is high minus low.
    ATR and the 2-ATR stop and 2R target must be finite and positive.
    The selected strategy was not profitable in the research: median training
    return -1.02%, with negative exploratory OOS returns for all four coins.
    Only caller-supplied chronological candles are used; df is unchanged.
    For an empty frame, close, RSI and ATR are NaN and direction is 'НЕТ'.
    """
    frame = _strategy_frame(df)
    if frame.empty:
        return "НЕТ", float("nan"), float("nan"), float("nan")
    row = frame.iloc[-1]
    return row["direction"], float(row["close"]), float(row["rsi"]), float(row["atr"])
