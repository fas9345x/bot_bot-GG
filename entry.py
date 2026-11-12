def get_entry_signal(df, trend):
    if trend == "НЕТ":
        return None
        
    last_row = df.iloc[-1]
    entry_price = last_row['close']
    
    signal = {}
    
    if trend == "LONG":
        # Стоп ставим за нижнюю границу канала
        stop_loss = last_row['KC_lower'] 
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
        # Стоп ставим за верхнюю границу канала
        stop_loss = last_row['KC_upper']
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