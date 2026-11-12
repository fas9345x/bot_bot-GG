import os
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(
    testnet=False,
    api_key=API_KEY,
    api_secret=API_SECRET,
)

# 👇 ДОБАВЛЯЕМ ИГНОРИРОВАНИЕ ОШИБКИ 110043 (Плечо не изменено) 👇
session.ignore_codes.add(110043)

def place_real_trade(symbol, direction, qty, tp, sl):
    try:
        # 1. Устанавливаем плечо (x10)
        try:
            session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage="10",
                sellLeverage="10"
            )
        except Exception as e:
            # Если ошибка не 110043 (например, проблема с маржой), то выходим
            if "110043" not in str(e):
                raise e

        # 2. Проверяем минимальный лот биржи
        info = session.get_instruments_info(category="linear", symbol=symbol)
        min_qty = float(info['result']['list'][0]['lotSizeFilter']['minOrderQty'])
        
        final_qty = max(float(qty), min_qty)
        
        qty_step = info['result']['list'][0]['lotSizeFilter']['qtyStep']
        decimals = len(qty_step.split('.')[1]) if '.' in qty_step else 0
        final_qty = round(final_qty, decimals)

        side = "Buy" if direction == "LONG" else "Sell"

        # 3. Открываем ордер по рынку
        session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(final_qty),
        )

        # 4. Выставляем TP и SL
        session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=str(tp),
            stopLoss=str(sl),
            positionIdx=0
        )
        return True, final_qty
    except Exception as e:
        print(f"❌ Ошибка при торговле {symbol}: {e}")
        return False, 0