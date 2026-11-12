import pandas as pd
import ta

def analyze_trend(df):
    # 1. Считаем индикаторы для стратегии Keltner
    bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
    df['BB_upper'] = bb.bollinger_hband()
    df['BB_lower'] = bb.bollinger_lband()
    
    kc = ta.volatility.KeltnerChannel(high=df['high'], low=df['low'], close=df['close'], window=20, window_atr=10)
    df['KC_upper'] = kc.keltner_channel_hband()
    df['KC_lower'] = kc.keltner_channel_lband()
    
    df['HMA21'] = ta.trend.hull_moving_average(df['close'], window=21)

    # Считаем RSI и ADX только для отображения в сообщении
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    adx_indicator = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    df['ADX'] = adx_indicator.adx()

    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    
    # 2. Проверяем, было ли сжатие (Bollinger внутри Keltner)
    squeeze_on = (prev_row['BB_upper'] < prev_row['KC_upper']) and (prev_row['BB_lower'] > prev_row['KC_lower'])
    
    trend = "НЕТ"
    
    # 3. Если было сжатие, и цена пробила канал вверх/вниз
    if squeeze_on:
        if last_row['close'] > last_row['KC_upper'] and last_row['close'] > last_row['HMA21']:
            trend = "LONG"
        elif last_row['close'] < last_row['KC_lower'] and last_row['close'] < last_row['HMA21']:
            trend = "SHORT"
            
    return trend, last_row['close'], last_row['RSI'], last_row['ADX']