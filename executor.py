import os
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)
session.ignore_codes.add(110043)

def place_real_trade(symbol, direction, qty, tp, sl):
    try:
        # Плечо x3 (умеренное)
        try:
            session.set_leverage(category="linear", symbol=symbol, buyLeverage="3", sellLeverage="3")
        except Exception as e:
            if "110043" not in str(e):
                raise e

        info = session.get_instruments_info(category="linear", symbol=symbol)
        min_qty = float(info['result']['list'][0]['lotSizeFilter']['minOrderQty'])
        final_qty = max(float(qty), min_qty)
        qty_step = info['result']['list'][0]['lotSizeFilter']['qtyStep']
        decimals = len(qty_step.split('.')[1]) if '.' in qty_step else 0
        final_qty = round(final_qty, decimals)

        side = "Buy" if direction == "LONG" else "Sell"
        session.place_order(category="linear", symbol=symbol, side=side, orderType="Market", qty=str(final_qty))
        session.set_trading_stop(category="linear", symbol=symbol, takeProfit=str(tp), stopLoss=str(sl), positionIdx=0)
        return True, final_qty
    except Exception as e:
        print(f"❌ Ошибка при торговле {symbol}: {e}")
        return False, 0

def close_position(symbol):
    """Закрывает позицию по рынку"""
    try:
        positions = session.get_positions(category="linear", symbol=symbol)
        for pos in positions['result']['list']:
            size = float(pos['size'])
            if size > 0:
                side = "Sell" if pos['side'] == "Buy" else "Buy"
                session.place_order(category="linear", symbol=symbol, side=side, orderType="Market", qty=str(size), reduceOnly=True)
        return True
    except Exception as e:
        print(f"❌ Ошибка при закрытии {symbol}: {e}")
        return False