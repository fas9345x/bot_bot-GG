"""Offline, hypothetical backtesting of the shared closed-candle strategy."""

import math
from numbers import Integral

from entry import _signal_from_values
from scanner import WARMUP_BARS, _strategy_frame


def run_backtest(df, initial_balance=1000.0, risk_fraction=0.005,
                 max_leverage=3.0, commission_rate=0.0006,
                 slippage_rate=0.0002, start_index=0):
    """Simulate one position at a time using chronological, closed OHLC bars.

    Designed for 15m bars. start_index is the integer position of the first
    bar eligible to generate a signal (default 0, allowed through len(df)).
    Earlier bars are used only for indicator warm-up; the simulation starts
    flat at initial_balance, with no pending orders and no equity observations
    before start_index. Thus the first possible entry is at start_index + 1.
    This permits a clean out-of-sample boundary without discarding history.
    Signal decisions use only the bar's close and prior history, with the exact
    strategy and fixed SL/TP used by get_entry_signal.
    A flat-at-close signal is eligible only at the next bar's open; it expires
    if that raw open or the adverse slipped fill is at/outside the signal's
    stop/target. Open positions can exit in the entry bar. At a later bar open,
    gaps through a stop or target fill at that open (then adverse slippage).
    Otherwise touches fill at the respective level; simultaneous touches choose
    the stop. SL and TP are simulated as market executions, not limit orders.
    Remaining positions close at the last close, with adverse slippage. A new
    signal may be accepted at the close of a bar in which a position exited.

    Defaults: 1,000 USDT, 0.5% of equity at entry, 3x max notional, commission
    0.06% and adverse slippage 0.02% on each side. Size is the smaller of:
    risk budget / (distance from actual fill to the slipped stop + estimated
    entry and stop-exit commissions), and the 3x notional cap measured against
    equity after paying the entry fee. A stop gap can exceed that planned risk.
    Fractional quantities, immediate fills and unlimited liquidity are assumed.
    There is no funding, liquidation, interest, exchange rounding, spread or
    volume model. Balance can therefore go negative in a gap and the simulation
    stops opening trades once balance is non-positive. All results are
    hypothetical, and the unvalidated strategy does not guarantee returns.

    Win/loss classification and profit factor use *net* realized trade PnL;
    zero PnL is breakeven, win_rate includes breakevens in its denominator.
    Profit factor is infinity for gains with no losses, otherwise 0 when there
    are neither gains nor losses. Max drawdown is based on bar-close equity,
    including unrealized PnL and paid entry fees, not intrabar extremes or a
    hypothetical liquidation fee. equity_curve contains initial equity plus
    one value per input bar from start_index onward; trade signal/entry/exit
    indices are positions in the original full input, not OOS-relative. The
    input frame is not modified or reordered.
    """
    initial_balance = float(initial_balance)
    risk_fraction = float(risk_fraction)
    max_leverage = float(max_leverage)
    commission_rate = float(commission_rate)
    slippage_rate = float(slippage_rate)
    if not all(map(math.isfinite, (initial_balance, risk_fraction, max_leverage,
                                   commission_rate, slippage_rate))):
        raise ValueError("backtest parameters must be finite")
    if initial_balance <= 0 or not 0 < risk_fraction <= 1 or max_leverage <= 0:
        raise ValueError("initial_balance and max_leverage must be positive; risk_fraction must be in (0, 1]")
    if not 0 <= commission_rate < 1 or not 0 <= slippage_rate < 1:
        raise ValueError("commission_rate and slippage_rate must be in [0, 1)")
    if isinstance(start_index, bool) or not isinstance(start_index, Integral) or not 0 <= start_index <= len(df):
        raise ValueError("start_index must be an integer in [0, len(df)]")
    start_index = int(start_index)
    if not df.empty and "open" not in df.columns:
        raise ValueError("missing required column: open")

    # All indicator work is vectorized once, rather than recomputed for every
    # history prefix. Per-bar execution below is O(n), without look-ahead.
    frame = _strategy_frame(df)
    if frame.empty:
        opens = highs = lows = closes = atrs = directions = []
    else:
        opens = frame["open"].to_numpy(dtype=float)
        highs = frame["high"].to_numpy(dtype=float)
        lows = frame["low"].to_numpy(dtype=float)
        closes = frame["close"].to_numpy(dtype=float)
        atrs = frame["atr"].to_numpy(dtype=float)
        directions = frame["direction"].to_numpy()

    balance = initial_balance
    position = None
    pending = None
    trades = []
    equity_curve = [initial_balance]
    peak_equity = initial_balance
    max_drawdown = 0.0
    skipped_signals = 0
    unfilled_last_bar_signals = 0
    total_fees = 0.0
    total_slippage = 0.0

    def close_position(pos, index, reference_price, reason):
        nonlocal balance, total_fees, total_slippage
        sign = pos["sign"]
        exit_price = reference_price * (1.0 - sign * slippage_rate)
        exit_fee = pos["quantity"] * exit_price * commission_rate
        gross_pnl = sign * (exit_price - pos["entry_price"]) * pos["quantity"]
        net_pnl = gross_pnl - pos["entry_fee"] - exit_fee
        balance += gross_pnl - exit_fee
        total_fees += exit_fee
        exit_slippage = sign * (reference_price - exit_price) * pos["quantity"]
        total_slippage += exit_slippage
        trades.append({
            "direction": pos["direction"],
            "signal_index": pos["signal_index"], "entry_index": pos["entry_index"],
            "exit_index": index, "signal_entry": pos["signal_entry"],
            "entry_reference": pos["entry_reference"], "entry_price": pos["entry_price"],
            "stop_loss": pos["stop_loss"], "take_profit": pos["take_profit"],
            "exit_reference": reference_price, "exit_price": exit_price,
            "exit_reason": reason, "quantity": pos["quantity"],
            "entry_notional": pos["quantity"] * pos["entry_price"],
            "planned_risk": pos["planned_risk"],
            "entry_fee": pos["entry_fee"], "exit_fee": exit_fee,
            "slippage_cost": pos["entry_slippage"] + exit_slippage,
            "gross_pnl": gross_pnl, "net_pnl": net_pnl,
            "balance_before": pos["balance_before"], "balance_after": balance,
        })

    for i in range(start_index, len(frame)):
        # Native Python floats keep numeric result and trade fields serializable.
        open_price, high, low, close = map(float, (opens[i], highs[i], lows[i], closes[i]))
        if pending is not None:
            signal = pending
            pending = None
            sign = 1 if signal["direction"] == "LONG" else -1
            stop, target = signal["stop_loss"], signal["take_profit"]
            entry_price = open_price * (1.0 + sign * slippage_rate)
            low_level, high_level = min(stop, target), max(stop, target)
            if (balance <= 0 or not low_level < open_price < high_level or
                    not low_level < entry_price < high_level):
                skipped_signals += 1
            else:
                stop_fill = stop * (1.0 - sign * slippage_rate)
                risk_per_unit = (sign * (entry_price - stop_fill) +
                                 commission_rate * (entry_price + stop_fill))
                risk_quantity = balance * risk_fraction / risk_per_unit
                # Ensure the cap also holds after the entry commission is paid.
                capped_quantity = (max_leverage * balance /
                                   (entry_price * (1.0 + max_leverage * commission_rate)))
                quantity = min(risk_quantity, capped_quantity)
                before = balance
                entry_fee = quantity * entry_price * commission_rate
                balance -= entry_fee
                entry_slippage = sign * (entry_price - open_price) * quantity
                total_fees += entry_fee
                total_slippage += entry_slippage
                position = {
                    "direction": signal["direction"], "sign": sign,
                    "signal_index": signal["signal_index"], "entry_index": i,
                    "signal_entry": signal["entry"], "entry_reference": open_price,
                    "entry_price": entry_price, "stop_loss": stop,
                    "take_profit": target, "quantity": quantity,
                    "planned_risk": quantity * risk_per_unit,
                    "entry_fee": entry_fee, "entry_slippage": entry_slippage,
                    "balance_before": before,
                }

        if position is not None:
            sign = position["sign"]
            stop, target = position["stop_loss"], position["take_profit"]
            # The open is observed before the high/low, so gap priority precedes
            # the conservative same-bar stop/target ordering.
            if sign * (open_price - stop) <= 0:
                close_position(position, i, open_price, "stop_gap")
                position = None
            elif sign * (open_price - target) >= 0:
                close_position(position, i, open_price, "target_gap")
                position = None
            elif (low <= stop if sign == 1 else high >= stop):
                close_position(position, i, stop, "stop")
                position = None
            elif (high >= target if sign == 1 else low <= target):
                close_position(position, i, target, "target")
                position = None
            elif i == len(frame) - 1:
                close_position(position, i, close, "end_of_data")
                position = None

        equity = balance
        if position is not None:
            equity += position["sign"] * (close - position["entry_price"]) * position["quantity"]
        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)

        if position is None and balance > 0 and directions[i] in ("LONG", "SHORT"):
            signal = _signal_from_values(directions[i], closes[i], atrs[i])
            if signal is not None:
                if i + 1 < len(frame):
                    signal["signal_index"] = i
                    pending = signal
                else:
                    unfilled_last_bar_signals += 1

    wins = sum(trade["net_pnl"] > 0 for trade in trades)
    losses = sum(trade["net_pnl"] < 0 for trade in trades)
    breakeven = len(trades) - wins - losses
    winning_pnl = sum(trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0)
    losing_pnl = -sum(trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0)
    profit_factor = winning_pnl / losing_pnl if losing_pnl else (float("inf") if winning_pnl else 0.0)
    return {
        "total": len(trades), "wins": wins, "losses": losses,
        "win_rate": 100.0 * wins / len(trades) if trades else 0.0,
        "profit_factor": profit_factor, "final_balance": balance,
        "breakeven": breakeven, "initial_balance": initial_balance,
        "net_profit": balance - initial_balance,
        "return_pct": 100.0 * (balance / initial_balance - 1.0),
        "max_drawdown_pct": 100.0 * max_drawdown,
        "winning_pnl": winning_pnl, "losing_pnl": losing_pnl,
        "total_fees": total_fees, "total_slippage": total_slippage,
        "skipped_signals": skipped_signals,
        "unfilled_last_bar_signals": unfilled_last_bar_signals,
        "trades": trades, "equity_curve": equity_curve,
        "assumptions": {
            "timeframe": "15m intended; caller supplies closed chronological bars",
            "risk_fraction": risk_fraction, "max_leverage": max_leverage,
            "commission_rate_per_side": commission_rate,
            "slippage_rate_per_side": slippage_rate, "warmup_bars": WARMUP_BARS,
            "start_index": start_index,
            "funding": "not modeled", "liquidation": "not modeled",
            "liquidity_and_exchange_rounding": "not modeled",
        },
    }
