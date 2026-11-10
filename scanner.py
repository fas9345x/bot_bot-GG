import pandas as pd
import ta

def analyze_trend(df):
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # Считаем ADX (Индекс силы тренда)
    adx_indicator = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    df['ADX'] = adx_indicator.adx()
    
    last_row = df.iloc[-1]
    
    trend = "НЕТ"
    # Главное условие: ADX > 25 означает, что на рынке есть сильный тренд. Если меньше - это флэт.
    if last_row['ADX'] > 25:
        if last_row['EMA20'] > last_row['EMA50'] and last_row['RSI'] > 50:
            trend = "LONG"
        elif last_row['EMA20'] < last_row['EMA50'] and last_row['RSI'] < 50:
            trend = "SHORT"
        
    return trend, last_row['close'], last_row['RSI'], last_row['ADX']