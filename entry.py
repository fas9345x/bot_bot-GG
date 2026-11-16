def get_entry_signal(df, trend):
    if trend == "НЕТ":
        return None
        
    last = df.iloc[-1]
    entry_price = last['close']
    
    if trend == "LONG":
        # Импульс 15м: SL 3%, TP 6% (риск/прибыль 1:2)
        stop_loss = entry_price * 0.97  # -3%
        take_profit = entry_price * 1.06  # +6%
        
        return {
            "direction": "LONG",
            "entry": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": 2.0
        }
        
    elif trend == "SHORT":
        stop_loss = entry_price * 1.03  # +3%
        take_profit = entry_price * 0.94  # -6%
        
        return {
            "direction": "SHORT",
            "entry": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": 2.0
        }
        
    return None