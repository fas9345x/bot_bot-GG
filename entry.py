import pandas as pd

def get_entry_signal(df, trend):
    if trend == "НЕТ":
        return None
        
    last_price = df['close'].iloc[-1]
    recent_low = df['low'].tail(10).min()
    recent_high = df['high'].tail(10).max()
    
    signal = {}
    
    if trend == "LONG":
        entry_price = last_price
        stop_loss = recent_low * 0.999
        risk = entry_price - stop_loss
        take_profit = entry_price + (risk * 2)
        
        signal = {
            "direction": "LONG",
            "entry": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": 2.0
        }
        
    elif trend == "SHORT":
        entry_price = last_price
        stop_loss = recent_high * 1.001
        risk = stop_loss - entry_price
        take_profit = entry_price - (risk * 2)
        
        signal = {
            "direction": "SHORT",
            "entry": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": 2.0
        }
        
    return signal