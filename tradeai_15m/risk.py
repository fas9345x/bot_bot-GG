"""Conservative planning only: a stop does not guarantee a maximum loss."""
import math
import os

DEFAULT_BALANCE_USDT = float(os.getenv("RISK_BALANCE_USDT", "12"))
DEFAULT_RISK_PERCENT = float(os.getenv("RISK_PER_TRADE_PCT", "0.5"))


def calculate_position_size(entry_price, stop_loss,
                            balance=DEFAULT_BALANCE_USDT,
                            risk_percent=DEFAULT_RISK_PERCENT):
    """Cap estimated loss and notional; balance is manually supplied, not live equity.

    Entry-price drift, fee and stop slippage buffers are assumptions. Actual loss
    can exceed this estimate due to gaps, liquidity, funding or liquidation.
    """
    entry = float(entry_price)
    stop = float(stop_loss)
    balance = float(balance)
    risk_percent = float(risk_percent)
    leverage = float(os.getenv("BYBIT_LEVERAGE", "3"))
    drift_pct = float(os.getenv("MAX_ENTRY_DEVIATION_PCT", "0.2"))
    values = (entry, stop, balance, risk_percent, leverage, drift_pct)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Параметры риска должны быть конечными числами.")
    if balance <= 0 or entry <= 0 or stop <= 0 or entry == stop:
        raise ValueError("Баланс и цены должны быть положительными; вход не равен стопу.")
    if not 0 < risk_percent <= 1:
        raise ValueError("RISK_PER_TRADE_PCT должен быть > 0 и <= 1%.")
    if not 1 <= leverage <= 10:
        raise ValueError("BYBIT_LEVERAGE должен быть от 1 до 10.")
    if not 0 <= drift_pct <= 1:
        raise ValueError("MAX_ENTRY_DEVIATION_PCT должен быть от 0 до 1%.")

    risk_usdt = balance * risk_percent / 100.0
    # 0.06% fee per side + 0.02% adverse stop slippage, as planning buffers.
    assumed_cost = 0.0014
    loss_per_unit = abs(entry - stop) + entry * (drift_pct / 100.0 + assumed_cost)
    risk_qty = risk_usdt / loss_per_unit
    # Reserve 5% of assumed equity rather than using all of it as margin.
    max_qty_by_margin = balance * leverage * 0.95 / entry
    qty = min(risk_qty, max_qty_by_margin)
    return {
        "qty": qty,
        "notional": qty * entry,
        "risk_usdt": risk_usdt,
        "estimated_loss_usdt": qty * loss_per_unit,
        "risk_percent": risk_percent,
    }
