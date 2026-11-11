import requests
import pandas as pd

def get_klines(symbol="BTCUSDT", interval="15", limit=100):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    
    # Добавляем заголовок, чтобы биржа думала, что запрос идет от браузера
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        # Проверяем код ответа. Если не 200 (например 403 или 451) - выходим.
        if response.status_code != 200:
            print(f"⚠️ Bybit заблокировал запрос ({symbol}). Код ответа: {response.status_code}")
            return None
            
        # Безопасно пытаемся прочитать JSON
        try:
            data = response.json()
        except Exception:
            print(f"⚠️ Bybit вернул не JSON ({symbol}). Текст ответа: {response.text[:100]}")
            return None
            
    except Exception as e:
        print(f"Ошибка сети: {e}")
        return None
        
    if data.get('retCode') != 0:
        print(f"Ошибка API Bybit: {data}")
        return None
        
    try:
        klines = data['result']['list']
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms')
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Ошибка обработки данных ({symbol}): {e}")
        return None