import requests
import pandas as pd

def get_klines(symbol="BTCUSDT", interval="15", limit=100):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"Ошибка сети: {e}")
        return None
        
    if data.get('retCode') != 0:
        print(f"Ошибка API Bybit: {data}")
        return None
        
    klines = data['result']['list']
    df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
    df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
    # Исправляем предупреждение pandas
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
    df = df.sort_values('timestamp').reset_index(drop=True)
    return df