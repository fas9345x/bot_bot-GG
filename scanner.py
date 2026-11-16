import pandas as pd
import ta

def analyze_trend(df):
    # 1. EMA 20 и 50 для среднесрочного тренда
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # 2. RSI(14) — стандартный
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 3. MACD для импульса
    macd = ta.trend.macd(df['close'], window_slow=26, window_fast=12, window_sign=9)
    df['MACD'] = macd
    df['MACD_SIGNAL'] = ta.trend.macd_signal(df['close'], window_slow=26, window_fast=12, window_sign=9)
    
    # 4. Объем — средний за 20 свечей
    df['VOL_AVG'] = df['volume'].rolling(window=20).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    trend = "НЕТ"
    
    # LONG: EMA20 > EMA50 (тренд вверх) + MACD пересек сигнал снизу вверх + RSI 50-70 + объем
    if last['EMA20'] > last['EMA50']:
        if prev['MACD'] <= prev['MACD_SIGNAL'] and last['MACD'] > last['MACD_SIGNAL']:
            if 50 < last['RSI'] < 70 and last['volume'] > last['VOL_AVG']:
                trend = "LONG"
    
    # SHORT: EMA20 < EMA50 (тренд вниз) + MACD пересек сигнал сверху вниз + RSI 30-50 + объем
    elif last['EMA20'] < last['EMA50']:
        if prev['MACD'] >= prev['MACD_SIGNAL'] and last['MACD'] < last['MACD_SIGNAL']:
            if 30 < last['RSI'] < 50 and last['volume'] > last['VOL_AVG']:
                trend = "SHORT"
            
    return trend, last['close'], last['RSI']