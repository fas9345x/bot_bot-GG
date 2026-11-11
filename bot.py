import asyncio
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from dotenv import load_dotenv

from market import get_klines
from scanner import analyze_trend
from entry import get_entry_signal
from risk import calculate_position_size
from backtest import run_backtest
import database

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
TOKEN = os.getenv("TELEGRAM_TOKEN")
PROXY_URL = "https://telegram-proxy.fas9345x.workers.dev"

custom_api = TelegramAPIServer.from_base(PROXY_URL)
session = AiohttpSession(api=custom_api)
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

last_signals = {}
ADMIN_CHAT_ID = None

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]

database.init_db()

def get_main_keyboard():
    kb = [
        [KeyboardButton(text="📊 Анализ рынка")],
        [KeyboardButton(text="⏱ Бэктест 15м"), KeyboardButton(text="⏱ Бэктест 60м")],
        [KeyboardButton(text="📈 История сигналов"), KeyboardButton(text="🤖 Статус бота")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"))
async def start(message: types.Message):
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = message.chat.id
    await message.answer("Привет! Я TradeAI Scanner.\n\nСлежу за 6 монетами на 15м таймфрейме. Выбери действие 👇", reply_markup=get_main_keyboard())

@dp.message(F.text == "📊 Анализ рынка")
async def btn_scan(message: types.Message):
    await message.answer("⏳ Скачиваю данные по 6 монетам...")
    
    tasks = [asyncio.to_thread(get_klines, symbol, "15", 100) for symbol in SYMBOLS]
    results = await asyncio.gather(*tasks)
    
    await message.answer("🧠 Анализирую индикаторы...")
    
    found_signal = False
    for symbol, df in zip(SYMBOLS, results):
        if df is None or df.empty: 
            continue
            
        trend, price, rsi, adx = analyze_trend(df)
        if trend == "НЕТ": 
            continue

        signal = get_entry_signal(df, trend)
        if not signal: 
            continue

        found_signal = True
        coins, usd_size = calculate_position_size(signal['entry'], signal['stop_loss'])
        database.save_signal(symbol, signal['direction'], signal['entry'], signal['stop_loss'], signal['take_profit'])

        text = f"🚨 <b>СИГНАЛ: {signal['direction']}</b> 🚨\nМонета: <b>{symbol} (15м)</b>\n\n"
        text += f"📍 Вход: <b>{signal['entry']:.4f}</b>\n🛑 SL: <b>{signal['stop_loss']:.4f}</b>\n🎯 TP: <b>{signal['take_profit']:.4f}</b>\n\n"
        text += f"💼 Размер: {coins:.4f} (~{usd_size:.2f} $)\n📈 RSI: {rsi:.2f} | 📉 ADX: {adx:.2f}"
        await message.answer(text, parse_mode="HTML")
    
    if not found_signal:
        await message.answer("⚪ На 15м таймфрейме сильных трендов нет. Ждем!")

@dp.message(F.text == "📈 История сигналов")
async def btn_history(message: types.Message):
    total, wins, losses = database.get_stats()
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    
    text = f"📁 <b>Статистика сигналов:</b>\n\n"
    text += f"Всего сохранено: {total}\n"
    text += f"🟢 Прибыльных (TP): {wins}\n"
    text += f"🔴 Убыточных (SL): {losses}\n"
    text += f"🎯 Текущий Win Rate: <b>{win_rate:.1f}%</b>\n\n"
    text += f"<i>(Сигналы со статусом OPEN еще в работе)</i>"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "⏱ Бэктест 15м")
async def btn_backtest_15(message: types.Message): await run_backtest_command(message, "15")

@dp.message(F.text == "⏱ Бэктест 60м")
async def btn_backtest_60(message: types.Message): await run_backtest_command(message, "60")

async def run_backtest_command(message: types.Message, tf: str):
    await message.answer(f"Запускаю бэктест на {tf}м таймфрейме...")
    df = await asyncio.to_thread(get_klines, "BTCUSDT", tf, 1000)
    if df is None: return
    results = await asyncio.to_thread(run_backtest, df)
    text = f"📊 <b>БЭКТЕСТ (BTCUSDT {tf}м)</b>\n\nВсего: {results['total']}\n🟢 Win: {results['wins']}\n🔴 Loss: {results['losses']}\n🎯 Win Rate: <b>{results['win_rate']:.1f}%</b>\n⚖️ Profit Factor: <b>{results['profit_factor']:.2f}</b>\n\n💰 Баланс: <b>${results['final_balance']:.2f}</b>"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🤖 Статус бота")
async def btn_status(message: types.Message):
    status = "🟢 Включен" if ADMIN_CHAT_ID else "🔴 Ожидает /start"
    text = f"<b>Статус системы:</b>\nАвто-сканер: {status}\nМонеты: {', '.join(SYMBOLS)}\nТаймфрейм: 15м\nМост: Активен"
    await message.answer(text, parse_mode="HTML")

async def signal_tracker():
    print("Трекер сигналов запущен...")
    while True:
        try:
            open_signals = database.get_open_signals()
            for sig in open_signals:
                sig_id, symbol, direction, entry, sl, tp = sig
                df = await asyncio.to_thread(get_klines, symbol, "1", 1)
                if df is not None and not df.empty:
                    current_price = df['close'].iloc[-1]
                    
                    if direction == "LONG":
                        if current_price >= tp:
                            database.update_signal_status(sig_id, "WIN")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"✅ <b>TP ДОСТИГНУТ!</b>\nСигнал {symbol} (LONG) закрыт в плюс.", parse_mode="HTML")
                        elif current_price <= sl:
                            database.update_signal_status(sig_id, "LOSS")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"🛑 <b>STOP LOSS!</b>\nСигнал {symbol} (LONG) закрыт в минус.", parse_mode="HTML")
                            
                    elif direction == "SHORT":
                        if current_price <= tp:
                            database.update_signal_status(sig_id, "WIN")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"✅ <b>TP ДОСТИГНУТ!</b>\nСигнал {symbol} (SHORT) закрыт в плюс.", parse_mode="HTML")
                        elif current_price >= sl:
                            database.update_signal_status(sig_id, "LOSS")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"🛑 <b>STOP LOSS!</b>\nСигнал {symbol} (SHORT) закрыт в минус.", parse_mode="HTML")
        except Exception as e:
            print(f"Ошибка трекера: {e}")
            
        await asyncio.sleep(900)

async def auto_scanner():
    global ADMIN_CHAT_ID
    print("Авто-сканер запущен в фоне...")
    while True:
        if ADMIN_CHAT_ID is not None:
            for symbol in SYMBOLS:
                try:
                    df = await asyncio.to_thread(get_klines, symbol, "15", 100)
                    if df is not None:
                        trend, price, rsi, adx = analyze_trend(df)
                        if trend in ["LONG", "SHORT"] and last_signals.get(symbol) != trend:
                            signal = get_entry_signal(df, trend)
                            if signal:
                                coins, usd_size = calculate_position_size(signal['entry'], signal['stop_loss'])
                                database.save_signal(symbol, signal['direction'], signal['entry'], signal['stop_loss'], signal['take_profit'])
                                text = f"🤖 <b>АВТО-СИГНАЛ: {signal['direction']}</b> 🤖\nМонета: <b>{symbol} (15м)</b>\n\n"
                                text += f"📍 Вход: <b>{signal['entry']:.4f}</b>\n🛑 SL: <b>{signal['stop_loss']:.4f}</b>\n🎯 TP: <b>{signal['take_profit']:.4f}</b>\n\n💼 Размер: {coins:.4f} (~{usd_size:.2f} $)"
                                await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML")
                                last_signals[symbol] = trend
                        elif trend == "НЕТ":
                            last_signals[symbol] = None
                except Exception as e: print(f"Ошибка сканера ({symbol}): {e}")
                await asyncio.sleep(2)
        await asyncio.sleep(3600)

async def main():
    print("Бот запущен...")
    asyncio.create_task(auto_scanner())
    asyncio.create_task(signal_tracker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())