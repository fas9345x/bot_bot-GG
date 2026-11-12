import pandas as pd
import ta

def calculate_hma(series, length=21):
    """Ручной расчет Hull Moving Average, так как в ta 0.11.0 его нет"""
    half_length = int(length / 2)
    sqrt_length = int(length ** 0.5)
    
    wma_half = series.rolling(window=half_length).apply(lambda x: (x * range(1, half_length + 1)).sum() / sum(range(1, half_length + 1)), raw=True)
    wma_full = series.rolling(window=length).apply(lambda x: (x * range(1, length + 1)).sum() / sum(range(1, length + 1)), raw=True)
    
    raw_hma = 2 * wma_half - wma_full
    hma = raw_hma.rolling(window=sqrt_length).apply(lambda x: (x * range(1, sqrt_length + 1)).sum() / sum(range(1, sqrt_length + 1)), raw=True)
    return hma

def analyze_trend(df):
    # 1. Считаем индикаторы
    bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
    df['BB_upper'] = bb.bollinger_hband()
    df['BB_lower'] = bb.bollinger_lband()
    
    kc = ta.volatility.KeltnerChannel(high=df['high'], low=df['low'], close=df['close'], window=20, window_atr=10)
    df['KC_upper'] = kc.keltner_channel_hband()
    df['KC_lower'] = kc.keltner_channel_lband()
    
    # Используем нашу ручную функцию
    df['HMA21'] = calculate_hma(df['close'], 21)

    # RSI и ADX для отображения
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    adx_indicator = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    df['ADX'] = adx_indicator.adx()

    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    
    # Проверяем сжатие
    squeeze_on = (prev_row['BB_upper'] < prev_row['KC_upper']) and (prev_row['BB_lower'] > prev_row['KC_lower'])
    
    trend = "НЕТ"
    
    if squeeze_on:
        if last_row['close'] > last_row['KC_upper'] and last_row['close'] > last_row['HMA21']:
            trend = "LONG"
        elif last_row['close'] < last_row['KC_lower'] and last_row['close'] < last_row['HMA21']:
            trend = "SHORT"
            
    return trend, last_row['close'], last_row['RSI'], last_row['ADX']