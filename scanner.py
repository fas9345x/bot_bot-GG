   import pandas as pd
   import ta
   
   def analyze_trend(df):
       # 1. EMA 20 и EMA 50 для определения тренда
       df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()
       df['EMA50'] = df['close'].ewm(span=50, adjust=False).mean()
       
       # 2. RSI (14) для подтверждения импульса
       rsi_ind = ta.momentum.RSIIndicator(close=df['close'], window=14)
       df['RSI'] = rsi_ind.rsi()
       
       last = df.iloc[-1]
       trend = "НЕТ"
       
       # Если EMA20 выше EMA50 и RSI > 50 -> Восходящий импульс (LONG)
       if last['EMA20'] > last['EMA50'] and last['RSI'] > 50:
           trend = "LONG"
           
       # Если EMA20 ниже EMA50 и RSI < 50 -> Нисходящий импульс (SHORT)
       elif last['EMA20'] < last['EMA50'] and last['RSI'] < 50:
           trend = "SHORT"
           
       return trend, last['close'], last['RSI'], 0