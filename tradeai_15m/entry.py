"""Entry signals built from the same causal strategy used by the backtest."""

import math

from scanner import REWARD_MULTIPLE, _levels_from_values, _strategy_frame


def _signal_from_values(direction, close, atr):
    """Build fixed signal-close levels; used identically by both callers."""
    if direction not in {"LONG", "SHORT"}:
        return None
    close, atr = float(close), float(atr)
    if not (math.isfinite(close) and math.isfinite(atr) and close > 0 and atr > 0):
        return None
    stop_loss, take_profit = _levels_from_values(direction, close, atr)
    if not (math.isfinite(stop_loss) and math.isfinite(take_profit) and
            stop_loss > 0 and take_profit > 0):
        return None
    return {"direction": direction, "entry": close, "stop_loss": stop_loss,
            "take_profit": take_profit, "risk_reward": REWARD_MULTIPLE}


def get_entry_signal(df, trend):
    """Return the last closed-bar signal or None; never modify df.

    The supplied trend must match scanner's breakout96 signal: the close must
    strictly exceed the high or fall below the low of the previous 96 closed
    candles, excluding the current candle. At least 97 closed bars are needed;
    EMA and RSI do not filter signals. Reference entry is the signal close; SL
    is 2 Wilder ATR14 away and TP is 2R away, with positive finite levels.
    These levels are fixed before the next-bar fill; actual fill and R:R may
    differ. The research did not find this strategy profitable: median train
    return was -1.02% and all four exploratory OOS returns were negative.
    """
    if trend not in {"LONG", "SHORT"}:
        return None
    frame = _strategy_frame(df)
    if frame.empty:
        return None
    last = frame.iloc[-1]
    if last["direction"] != trend:
        return None
    return _signal_from_values(trend, last["close"], last["atr"])
