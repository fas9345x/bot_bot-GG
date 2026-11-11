def calculate_position_size(entry_price, stop_loss, balance=27.0, risk_percent=0.5):
    # Сумма, которой мы рискуем (0.5% от 27$ = 0.135$)
    risk_amount = balance * (risk_percent / 100)
    
    # Разница между входом и стопом
    price_difference = abs(entry_price - stop_loss)
    
    if price_difference == 0:
        return 0, 0
        
    # Размер позиции в монетах
    position_size_coins = risk_amount / price_difference
    
    # Размер позиции в долларах
    position_size_usd = position_size_coins * entry_price
    
    return position_size_coins, position_size_usd