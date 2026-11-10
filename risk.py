def calculate_position_size(entry_price, stop_loss, balance=1000.0, risk_percent=0.5):
    risk_amount = balance * (risk_percent / 100)
    price_difference = abs(entry_price - stop_loss)
    
    if price_difference == 0:
        return 0, 0
        
    position_size_coins = risk_amount / price_difference
    position_size_usd = position_size_coins * entry_price
    
    return position_size_coins, position_size_usd