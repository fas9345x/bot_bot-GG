import pandas as pd
from scanner import analyze_trend

def run_backtest(df):
    balance = 1000.0
    initial_balance = balance
    trades = []
    in_position = False
    position_type = None
    entry_price = 0
    sl = 0
    tp = 0
    
    for i in range(50, len(df)):
        current_candle = df.iloc[i]
        high = current_candle['high']
        low = current_candle['low']
        close = current_candle['close']
        
        if in_position:
            if position_type == "LONG":
                if low <= sl:
                    balance -= 5
                    trades.append("Loss")
                    in_position = False
                elif high >= tp:
                    balance += 10
                    trades.append("Win")
                    in_position = False
            elif position_type == "SHORT":
                if high >= sl:
                    balance -= 5
                    trades.append("Loss")
                    in_position = False
                elif low <= tp:
                    balance += 10
                    trades.append("Win")
                    in_position = False
        else:
            sub_df = df.iloc[:i+1]
            # ТУТ ИЗМЕНЕНИЕ: принимаем 4 значения (тренд, цена, rsi, adx)
            trend, _, _, _ = analyze_trend(sub_df)
            
            if trend in ["LONG", "SHORT"]:
                in_position = True
                position_type = trend
                entry_price = close
                
                if trend == "LONG":
                    sl = low * 0.999
                    risk = entry_price - sl
                    tp = entry_price + (risk * 2)
                else:
                    sl = high * 1.001
                    risk = sl - entry_price
                    tp = entry_price - (risk * 2)
    
    total_trades = len(trades)
    wins = trades.count("Win")
    losses = trades.count("Loss")
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    profit_factor = (wins * 10) / (losses * 5) if losses > 0 else 0
    
    return {
        "total": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "final_balance": balance
    }