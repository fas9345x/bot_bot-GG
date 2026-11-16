def calculate_position_size(entry_price, stop_loss, balance=14.40, risk_percent=50.0):
    # Используем 50% депозита на сделку (остальное — резерв)
    position_size_usd = balance * (risk_percent / 100)
    position_size_coins = position_size_usd / entry_price
    
    return position_size_coins, position_size_usd