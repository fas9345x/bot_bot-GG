import asyncio
import os
from datetime import datetime
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
from executor import place_real_trade, close_position
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
TF = "15"  # 15 минут
SYMBOLS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]  # 4 монеты

database.init_db()

def get_main_keyboard():
    kb = [
        [KeyboardButton(text="📊 Анализ рынка")],
        [KeyboardButton(text="⏱ Бэктест 15м"), KeyboardButton(text="⏱ Бэктест 1ч")],
        [KeyboardButton(text="📈 История сигналов"), KeyboardButton(text="🤖 Статус бота")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("/start"))
async def start(message: types.Message):
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = message.chat.id
    await message.answer("🎯 ИМПУЛЬС-РЕЖИМ 15м 🎯\n\nТаймфрейм: 15 минут\nМонеты: SOL, XRP, DOGE, ADA\nПлечо: x3\nСтратегия: EMA 20/50 + MACD + RSI\n\n⚡ Ожидается 2-3 сигнала в день!", reply_markup=get_main_keyboard())

@dp.message(F.text == "📊 Анализ рынка")
async def btn_scan(message: types.Message):
    await message.answer("⏳ Скачиваю данные (15м)...")
    tasks = [asyncio.to_thread(get_klines, symbol, TF, 100) for symbol in SYMBOLS]
    results = await asyncio.gather(*tasks)
    await message.answer("🧠 Ищу импульсные входы...")
    
    found_signal = False
    for symbol, df in zip(SYMBOLS, results):
        if df is None or df.empty: continue
        try:
            trend, price, rsi = await asyncio.to_thread(analyze_trend, df)
            if trend == "НЕТ": continue
            signal = await asyncio.to_thread(get_entry_signal, df, trend)
            if not signal: continue
            
            found_signal = True
            coins, usd_size = calculate_position_size(signal['entry'], signal['stop_loss'])
            database.save_signal(symbol, signal['direction'], signal['entry'], signal['stop_loss'], signal['take_profit'])
            
            success, real_qty = await asyncio.to_thread(
                place_real_trade, symbol, signal['direction'], coins, signal['take_profit'], signal['stop_loss']
            )
            
            if success:
                text = f"⚡ <b>ИМПУЛЬС-СДЕЛКА ОТКРЫТА!</b> ⚡\n"
            else:
                text = f"⚠️ <b>ОРДЕР НЕ ОТКРЫТ</b> ⚠️\n"
            
            text += f"🚨 {signal['direction']} | {symbol} (15м)\n\n"
            text += f"📍 Вход: <b>{signal['entry']:.4f}</b>\n🛑 SL: <b>{signal['stop_loss']:.4f}</b> (-3%)\n🎯 TP: <b>{signal['take_profit']:.4f}</b> (+6%)\n"
            text += f"💼 Размер: {real_qty} (~{usd_size:.2f}$)\n📈 RSI: {rsi:.1f}\n\n"
            text += f"⚡ Плечо: x3 | Риск/Прибыль: 1:2"
            await message.answer(text, parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка {symbol}: {e}")
    
    if not found_signal:
        await message.answer("⚪ Нет сигналов на 15м. Ждем пересечения MACD...")

@dp.message(F.text == "📈 История сигналов")
async def btn_history(message: types.Message):
    total, wins, losses = database.get_stats()
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    text = f"📁 <b>Статистика:</b>\n\nВсего: {total}\n🟢 WIN: {wins}\n🔴 LOSS: {losses}\n🎯 Win Rate: <b>{win_rate:.1f}%</b>"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "⏱ Бэктест 15м")
async def bt15m(message: types.Message): await run_bt(message, "15")

@dp.message(F.text == "⏱ Бэктест 1ч")
async def bt1h(message: types.Message): await run_bt(message, "60")

async def run_bt(message: types.Message, tf: str):
    await message.answer(f"Бэктест {tf}м...")
    df = await asyncio.to_thread(get_klines, "SOLUSDT", tf, 1000)
    if df is None: return
    results = await asyncio.to_thread(run_backtest, df)
    text = f"📊 <b>БЭКТЕСТ {tf}м</b>\n\nВсего: {results['total']}\n🟢 Win: {results['wins']}\n🔴 Loss: {results['losses']}\n🎯 WR: <b>{results['win_rate']:.1f}%</b>\n⚖️ PF: <b>{results['profit_factor']:.2f}</b>"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🤖 Статус бота")
async def btn_status(message: types.Message):
    status = "🟢 Вкл" if ADMIN_CHAT_ID else "🔴 Ожидает /start"
    text = f"<b>Статус:</b>\nАвто-сканер: {status}\nРежим: Импульс 15м\nМонеты: {', '.join(SYMBOLS)}\nПлечо: x3\nТорговля: ⚠️ РЕАЛЬНАЯ"
    await message.answer(text, parse_mode="HTML")

async def signal_tracker():
    print("Трекер запущен...")
    while True:
        try:
            open_signals = database.get_open_signals()
            for sig in open_signals:
                sig_id, symbol, direction, entry, sl, tp = sig
                
                df = await asyncio.to_thread(get_klines, symbol, "1", 1)
                if df is not None and not df.empty:
                    price = df['close'].iloc[-1]
                    if direction == "LONG":
                        if price >= tp:
                            database.update_signal_status(sig_id, "WIN")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"✅ <b>TP!</b> {symbol} LONG +6%", parse_mode="HTML")
                        elif price <= sl:
                            database.update_signal_status(sig_id, "LOSS")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"🛑 <b>SL!</b> {symbol} LONG -3%", parse_mode="HTML")
                    elif direction == "SHORT":
                        if price <= tp:
                            database.update_signal_status(sig_id, "WIN")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"✅ <b>TP!</b> {symbol} SHORT +6%", parse_mode="HTML")
                        elif price >= sl:
                            database.update_signal_status(sig_id, "LOSS")
                            if ADMIN_CHAT_ID: await bot.send_message(ADMIN_CHAT_ID, f"🛑 <b>SL!</b> {symbol} SHORT -3%", parse_mode="HTML")
        except Exception as e:
            print(f"Ошибка трекера: {e}")
        await asyncio.sleep(60)  # Каждую минуту

async def auto_scanner():
    global ADMIN_CHAT_ID
    print("Авто-сканер (импульс 15м) запущен...")
    while True:
        if ADMIN_CHAT_ID is not None:
            for symbol in SYMBOLS:
                try:
                    df = await asyncio.to_thread(get_klines, symbol, TF, 100)
                    if df is not None:
                        trend, price, rsi = await asyncio.to_thread(analyze_trend, df)
                        if trend in ["LONG", "SHORT"] and last_signals.get(symbol) != trend:
                            signal = await asyncio.to_thread(get_entry_signal, df, trend)
                            if signal:
                                coins, usd_size = calculate_position_size(signal['entry'], signal['stop_loss'])
                                database.save_signal(symbol, signal['direction'], signal['entry'], signal['stop_loss'], signal['take_profit'])
                                
                                success, real_qty = await asyncio.to_thread(
                                    place_real_trade, symbol, signal['direction'], coins, signal['take_profit'], signal['stop_loss']
                                )
                                
                                if success:
                                    text = f"⚡ <b>АВТО-ИМПУЛЬС: {signal['direction']}</b> ⚡\n{symbol} (15м)\n\n"
                                    text += f"📍 {signal['entry']:.4f} | 🛑 {signal['stop_loss']:.4f} | 🎯 {signal['take_profit']:.4f}\n"
                                    text += f"💼 {real_qty} (~{usd_size:.2f}$)\n⚡ Плечо: x3 | Риск/Прибыль: 1:2"
                                    await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML")
                                last_signals[symbol] = trend
                        elif trend == "НЕТ":
                            last_signals[symbol] = None
                except Exception as e:
                    print(f"Ошибка сканера ({symbol}): {e}")
                await asyncio.sleep(3)
        await asyncio.sleep(60)  # Каждую минуту

async def main():
    print("Импульс-бот 15м запущен...")
    asyncio.create_task(auto_scanner())
    asyncio.create_task(signal_tracker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())