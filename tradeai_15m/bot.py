import asyncio
import logging
import os
import time

from dotenv import load_dotenv

# Load local development settings before importing modules that read environment variables.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from filelock import FileLock, Timeout

from market import get_klines
from scanner import analyze_trend
from entry import get_entry_signal
from risk import calculate_position_size
from backtest import run_backtest
import executor
import database


TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing from Railway Variables")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tradeai")

if PROXY_URL:
    telegram_api = TelegramAPIServer.from_base(PROXY_URL)
    bot = Bot(token=TOKEN, session=AiohttpSession(api=telegram_api))
else:
    bot = Bot(token=TOKEN)

dp = Dispatcher()
last_signals = {}
_admin_chat_id_value = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID = int(_admin_chat_id_value) if _admin_chat_id_value.isdigit() else None
TF = "15"
SYMBOLS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
TRACKER_INTERVAL = max(60, int(os.getenv("TRACKER_INTERVAL_SECONDS", "300")))
SCAN_INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "300")))
SYMBOL_LOCKS = {symbol: asyncio.Lock() for symbol in SYMBOLS}
# One position at a time across the configured symbols, within this process.
PORTFOLIO_LOCK = asyncio.Lock()

database.init_db()


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        text = getattr(event, "text", "") or ""
        if text.startswith("/myid"):
            return await handler(event, data)
        if ADMIN_CHAT_ID is None:
            await event.answer(
                "Доступ ещё не настроен. Отправьте /myid, затем добавьте полученный "
                "числовой ID в Railway Variables как TELEGRAM_ADMIN_CHAT_ID и перезапустите бота."
            )
            return None
        if getattr(getattr(event, "chat", None), "id", None) != ADMIN_CHAT_ID:
            return None
        return await handler(event, data)


dp.message.middleware(AdminOnlyMiddleware())


def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text="📊 Анализ рынка")],
        [KeyboardButton(text="⏱ Бэктест 15м")],
        [KeyboardButton(text="📈 История сигналов"), KeyboardButton(text="🤖 Статус бота")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


async def _open_signal_if_clear(symbol, signal):
    """Serialize manual/automatic entries and write DB only after protected order success."""
    async with PORTFOLIO_LOCK:
        if database.get_open_signals():
            return {"success": False, "reason": "Есть открытая или ещё не сверенная сделка; лимит — одна позиция."}
        candle = signal.get("signal_candle")
        if database.was_signal_candle_traded(symbol, candle):
            return {"success": False, "reason": "По этой свече сделка уже открывалась."}

        sizing = calculate_position_size(signal["entry"], signal["stop_loss"])
        submitted_at_ms = int(time.time() * 1000)
        success, real_qty = await asyncio.to_thread(
            executor.place_real_trade,
            symbol,
            signal["direction"],
            sizing["qty"],
            signal["take_profit"],
            signal["stop_loss"],
            signal["entry"],
        )
        if not success:
            block = executor.get_order_block_status()
            if block["blocked"]:
                reason = (
                    "ВНИМАНИЕ: результат ордера может быть неизвестен. Новые сделки "
                    "заблокированы. Проверьте позиции, активные ордера и историю Bybit "
                    "в указанной среде; не перезапускайте торговлю до ручной проверки."
                )
            else:
                reason = (
                    "Вход не подтверждён. Проверьте режим, Testnet, "
                    "минимальный размер ордера и журнал Railway."
                )
            return {
                "success": False,
                "reason": reason,
                "sizing": sizing,
            }

        try:
            database.save_signal(
                symbol,
                signal["direction"],
                signal["entry"],
                signal["stop_loss"],
                signal["take_profit"],
                candle,
                submitted_at_ms,
            )
            # A durable guard is left in place until the DB record is committed.
            if not executor.clear_order_block_after_record():
                raise RuntimeError("Не удалось снять защитный флаг после записи в SQLite")
        except Exception:
            logger.exception("Order confirmed but database/guard reconciliation failed for %s", symbol)
            persisted = executor.block_new_orders(f"Ошибка записи или снятия флага для {symbol}")
            return {
                "success": True,
                "qty": real_qty,
                "sizing": sizing,
                "reason": (
                    "ВНИМАНИЕ: ордер исполнен, но запись SQLite или предохранитель "
                    "требуют проверки. Новые входы заблокированы."
                    + (" КРИТИЧНО: отключите торговлю — флаг не удалось сохранить!" if not persisted else "")
                ),
            }

        return {"success": True, "qty": real_qty, "sizing": sizing}


@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "Бот запущен. Проверьте режим в «Статус бота».\n"
        "Торговля по умолчанию отключена, Bybit — Testnet.",
        reply_markup=get_main_keyboard(),
    )


@dp.message(Command("myid"))
async def myid(message: types.Message):
    await message.answer(f"Ваш Telegram chat ID: {message.chat.id}")


@dp.message(F.text == "📊 Анализ рынка")
async def btn_scan(message: types.Message):
    await message.answer("⏳ Скачиваю закрытые свечи 15м...")
    tasks = [asyncio.to_thread(get_klines, symbol, TF, 250) for symbol in SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    await message.answer("🧠 Проверяю условия...")

    found_signal = False
    for symbol, df in zip(SYMBOLS, results):
        if isinstance(df, Exception):
            logger.warning("Market data error for %s: %s", symbol, df)
            continue
        if df is None or df.empty:
            continue
        try:
            if any(row[1] == symbol for row in database.get_open_signals()):
                continue

            trend, price, rsi, _ = await asyncio.to_thread(analyze_trend, df)
            if trend not in {"LONG", "SHORT"}:
                continue
            signal = await asyncio.to_thread(get_entry_signal, df, trend)
            if not signal:
                continue

            signal["signal_candle"] = df["timestamp"].iloc[-1].isoformat()
            found_signal = True
            result = await _open_signal_if_clear(symbol, signal)
            if result["success"]:
                text = "✅ <b>Защищённый ордер принят!</b>\n"
                if result.get("reason"):
                    text += f"⚠️ {result['reason']}\n"
            else:
                text = "⚪ <b>Сигнал есть; исполнение не подтверждено.</b>\n"
                text += f"{result['reason']}\n"

            text += (
                f"\n{signal['direction']} · {symbol} (15м)\n"
                f"Вход-ориентир: <b>{signal['entry']:.6g}</b>\n"
                f"SL: <b>{signal['stop_loss']:.6g}</b>\n"
                f"TP: <b>{signal['take_profit']:.6g}</b>\n"
                f"RSI: {rsi:.1f}"
            )
            if result.get("sizing"):
                sizing = result["sizing"]
                text += (
                    f"\nПлановый риск до SL: {sizing['risk_usdt']:.4f} USDT"
                    f"\nРасчётный номинал: {sizing['notional']:.4f} USDT"
                )
            if result.get("success") and result.get("qty") is not None:
                text += f"\nКоличество: {result['qty']}"
            await message.answer(text, parse_mode="HTML")
        except Exception as exc:
            logger.exception("Manual scan failed for %s", symbol)
            await message.answer(f"Ошибка анализа {symbol}: {exc}")

    if not found_signal:
        await message.answer("⚪ Сигналов по закрытым 15м-свечам нет.")


@dp.message(F.text == "📈 История сигналов")
async def btn_history(message: types.Message):
    total, wins, losses = database.get_stats()
    closed_count = wins + losses
    win_rate = (wins / closed_count * 100) if closed_count else 0
    open_count = len(database.get_open_signals())
    await message.answer(
        f"📁 <b>Статистика:</b>\n\nВсего записей: {total}\n"
        f"Открытых: {open_count}\n🟢 WIN: {wins}\n🔴 LOSS: {losses}\n"
        f"🎯 WR подтверждённых закрытий: <b>{win_rate:.1f}%</b>\n"
        "Закрытия без подтверждённого PnL не входят в WIN/LOSS.",
        parse_mode="HTML",
    )


@dp.message(F.text == "⏱ Бэктест 15м")
async def bt15(message: types.Message):
    await run_bt(message, "15")


async def run_bt(message: types.Message, tf: str):
    await message.answer(f"Бэктест {tf}м: до 1000 последних свечей по каждой монете...")
    lines = []
    for symbol in SYMBOLS:
        try:
            df = await asyncio.to_thread(get_klines, symbol, tf, 1000)
            if df is None or df.empty:
                lines.append(f"{symbol}: нет данных")
                continue
            result = await asyncio.to_thread(run_backtest, df)
            pnl_pct = (result["final_balance"] / 1000 - 1) * 100
            lines.append(f"{symbol}: {result['total']} сделок, WR {result['win_rate']:.1f}%, итог {pnl_pct:+.2f}%")
        except Exception:
            logger.exception("Backtest failed for %s", symbol)
            lines.append(f"{symbol}: ошибка теста")
    venue = "Testnet" if executor.BYBIT_TESTNET else "Mainnet"
    await message.answer(
        f"📊 <b>БЭКТЕСТ {tf}м ({venue})</b>\n" + "\n".join(lines)
        + "\nБаланс 1000 USDT на каждую монету; комиссии и проскальзывание — модельные. "
        "Funding и ликвидация не учтены. Короткая история не доказывает будущую доходность.",
        parse_mode="HTML",
    )


@dp.message(F.text == "🤖 Статус бота")
async def btn_status(message: types.Message):
    blocked = executor.get_order_block_status()["blocked"]
    await message.answer(
        f"<b>Статус:</b>\nАвто-сканер: {'🟢 активен' if ADMIN_CHAT_ID else '🔴 нажмите /start'}\n"
        f"Режим торговли: <b>{executor.mode_label()}</b>\n"
        f"Предохранитель новых ордеров: {'ЗАБЛОКИРОВАН — проверьте Bybit и логи' if blocked else 'не активен'}\n"
        f"Среда Bybit: {'Testnet' if executor.BYBIT_TESTNET else 'MAINNET'}\n"
        f"Таймфрейм: 15м\nПлечо: x{executor.LEVERAGE}\n"
        f"Расчётный баланс риска: {os.getenv('RISK_BALANCE_USDT', '12')} USDT\n"
        f"Риск на сделку: {os.getenv('RISK_PER_TRADE_PCT', '0.5')}%\n"
        f"Монеты: {', '.join(SYMBOLS)}",
        parse_mode="HTML",
    )


def _normalize_side(value):
    value = str(value or "").strip().upper()
    if value in {"LONG", "BUY"}:
        return "BUY"
    if value in {"SHORT", "SELL"}:
        return "SELL"
    return None


def _to_ms(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    if result < 10**12:
        result *= 1000
    return result


async def signal_tracker():
    logger.info("Signal tracker started; interval=%s seconds", TRACKER_INTERVAL)
    while True:
        try:
            if not executor.API_KEY or not executor.API_SECRET:
                await asyncio.sleep(TRACKER_INTERVAL)
                continue

            open_signals = database.get_open_signal_details()
            if not open_signals:
                await asyncio.sleep(TRACKER_INTERVAL)
                continue

            # One ticker request instead of one kline request per open symbol.
            ticker_map = {}
            try:
                ticker_rows = await asyncio.to_thread(executor.get_tickers_snapshot)
                ticker_map = {
                    row.get("symbol"): row.get("lastPrice")
                    for row in ticker_rows
                    if row.get("symbol") and row.get("lastPrice") is not None
                }
            except Exception as exc:
                logger.warning("Ticker snapshot failed: %s", exc)

            # Do not change DB status unless this private API call succeeded.
            positions = await asyncio.to_thread(executor.get_positions_snapshot)
            active_positions = set()
            for position in positions:
                try:
                    size = float(position.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                side = _normalize_side(position.get("side"))
                symbol = position.get("symbol")
                if symbol and side and size > 0:
                    active_positions.add((symbol, side))

            missing = []
            for signal in open_signals:
                symbol = signal.get("symbol")
                side = _normalize_side(signal.get("direction"))
                if symbol and side and (symbol, side) not in active_positions:
                    missing.append(signal)

            if missing:
                # One closed-PnL request for all symbols, only when a position is absent.
                try:
                    closed_rows = await asyncio.to_thread(executor.get_closed_pnl_snapshot)
                except Exception as exc:
                    logger.warning("Closed PnL lookup failed; leaving DB rows OPEN: %s", exc)
                    closed_rows = None

                if closed_rows is not None:
                    for signal in missing:
                        symbol = signal.get("symbol")
                        side = _normalize_side(signal.get("direction"))
                        opened_at_ms = _to_ms(signal.get("opened_at_ms"))
                        matching = []

                        if opened_at_ms is not None:
                            for row in closed_rows:
                                if row.get("symbol") != symbol:
                                    continue
                                close_side = "SELL" if side == "BUY" else "BUY"
                                if _normalize_side(row.get("side")) != close_side:
                                    continue
                                close_time_ms = _to_ms(
                                    row.get("updatedTime") or row.get("createdTime")
                                )
                                if close_time_ms is None or close_time_ms < opened_at_ms:
                                    continue
                                try:
                                    pnl = float(row.get("closedPnl") or 0)
                                except (TypeError, ValueError):
                                    continue
                                matching.append((close_time_ms, row, pnl))

                        if matching:
                            realized_pnl = sum(item[2] for item in matching)
                            last_time_ms, last_row, _ = max(
                                matching, key=lambda item: item[0]
                            )
                            try:
                                exit_price = float(last_row.get("avgExitPrice") or 0) or None
                            except (TypeError, ValueError):
                                exit_price = None

                            if realized_pnl > 0:
                                status = "WIN"
                            elif realized_pnl < 0:
                                status = "LOSS"
                            else:
                                status = "BREAKEVEN"

                            database.close_signal(
                                signal["id"], status, realized_pnl, exit_price,
                                last_time_ms, "Bybit closed PnL",
                            )
                            logger.info(
                                "Closed trade confirmed: id=%s symbol=%s status=%s pnl=%.8f",
                                signal["id"], symbol, status, realized_pnl,
                            )
                            if ADMIN_CHAT_ID:
                                await bot.send_message(
                                    ADMIN_CHAT_ID,
                                    f"Закрытие подтверждено Bybit: {symbol} · {status} · "
                                    f"PnL {realized_pnl:.6f} USDT",
                                )
                        else:
                            # A delayed or incomplete PnL response must not unlock
                            # the portfolio or fabricate a closing result.
                            logger.warning(
                                "Position absent, PnL unverified; keeping OPEN: id=%s symbol=%s",
                                signal["id"], symbol,
                            )

        except Exception as exc:
            # One warning per interval, not a rapid loop that floods Railway logs.
            logger.warning("Signal tracker cycle failed: %s", exc)

        await asyncio.sleep(TRACKER_INTERVAL)


async def auto_scanner():
    global ADMIN_CHAT_ID
    logger.info("Auto scanner started; scan interval=%s seconds", SCAN_INTERVAL)
    while True:
        if ADMIN_CHAT_ID is not None:
            for symbol in SYMBOLS:
                try:
                    if any(row[1] == symbol for row in database.get_open_signals()):
                        continue

                    df = await asyncio.to_thread(get_klines, symbol, TF, 250)
                    if df is None or df.empty:
                        continue

                    trend, price, rsi, _ = await asyncio.to_thread(analyze_trend, df)
                    candle = df["timestamp"].iloc[-1].isoformat()
                    if trend in {"LONG", "SHORT"} and last_signals.get(symbol) != candle:
                        signal = await asyncio.to_thread(get_entry_signal, df, trend)
                        if signal:
                            signal["signal_candle"] = candle
                            result = await _open_signal_if_clear(symbol, signal)
                            if result["success"]:
                                text = (
                                    f"⚡ <b>АВТО-СИГНАЛ: {signal['direction']}</b> ⚡\n"
                                    f"{symbol} (15м)\n"
                                    f"Вход-ориентир: {signal['entry']:.6g}\n"
                                    f"SL: {signal['stop_loss']:.6g} · TP: {signal['take_profit']:.6g}\n"
                                    f"Риск до SL: {result['sizing']['risk_usdt']:.4f} USDT\n"
                                    f"Номинал: {result['sizing']['notional']:.4f} USDT\n"
                                    f"Количество: {result['qty']} · RSI: {rsi:.1f}"
                                )
                            else:
                                text = (
                                    f"Сигнал {signal['direction']} {symbol}; исполнение не подтверждено.\n"
                                    f"{result['reason']}"
                                )
                            await bot.send_message(
                                chat_id=ADMIN_CHAT_ID,
                                text=text,
                                parse_mode="HTML",
                            )
                            last_signals[symbol] = candle
                except Exception as exc:
                    logger.warning("Scanner error for %s: %s", symbol, exc)
                await asyncio.sleep(2)
        await asyncio.sleep(SCAN_INTERVAL)


async def main():
    # This only coordinates instances that share the same persistent volume.
    database.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(database.DB_PATH.parent / "tradeai.instance.lock"), timeout=0)
    try:
        with lock:
            logger.info(
                "Bot started; mode=%s; TF=%sm; tracker=%ss",
                executor.mode_label(), TF, TRACKER_INTERVAL,
            )
            asyncio.create_task(auto_scanner())
            asyncio.create_task(signal_tracker())
            await dp.start_polling(bot)
    except Timeout as exc:
        raise RuntimeError("На общем томе уже работает другая копия бота") from exc


if __name__ == "__main__":
    asyncio.run(main())
