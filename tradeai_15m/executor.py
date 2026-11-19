"""Fail-closed Bybit USDT-perpetual execution.

The persistent directory must actually be backed by a durable, shared volume.
The caller MUST record a confirmed trade and then call
``clear_order_block_after_record()``; otherwise all new entries stay blocked.
"""
import fcntl
import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN
from pathlib import Path

from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP
from bybit_guard import call_bybit


_CONFIG_ERRORS = []


def _env_bool(name, default):
    """Only literal true/false are accepted; invalid configuration disables entries."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    _CONFIG_ERRORS.append(f"{name} must be exactly true or false")
    return default


# An invalid testnet value resolves to testnet AND blocks all new entries.
BYBIT_TESTNET = _env_bool("BYBIT_TESTNET", True)
TRADING_ENABLED = _env_bool("TRADING_ENABLED", False)
ALLOW_MAINNET_TRADING = _env_bool("ALLOW_MAINNET_TRADING", False)
LEVERAGE = os.getenv("BYBIT_LEVERAGE", "3")
API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()

# pybit retries some Bybit error codes even with force_retry=False. Set BOTH
# max_retries=1 and an empty retry_codes set: no create request may be retried.
session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=API_KEY or None,
    api_secret=API_SECRET or None,
    max_retries=1,
    force_retry=False,
    timeout=3,
)
session.retry_codes = set()

_VOLUME_PATH = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data"))
ORDER_BLOCK_FILE = _VOLUME_PATH / "bybit_new_orders_block.json"
_ORDER_LOCK_FILE = _VOLUME_PATH / "bybit_new_orders.lock"
_PROCESS_LOCK = threading.RLock()
_PROCESS_BLOCKED = False
# Only the process that obtained a confirmed result may clear the marker after
# it has saved that very result. A restart deliberately loses this capability.
_CONFIRMED_RECORD_TOKEN = None
_CONFIRM_TIMEOUT_SECONDS = 10.0
_CONFIRM_POLL_SECONDS = 0.5
_CONFIRM_MAX_ATTEMPTS = 20


def mode_label():
    if _CONFIG_ERRORS:
        return "ОТКЛЮЧЕНА (ошибка конфигурации)"
    if not TRADING_ENABLED:
        return "ОТКЛЮЧЕНА (защитный режим)"
    if not BYBIT_TESTNET and not ALLOW_MAINNET_TRADING:
        return "ОТКЛЮЧЕНА (mainnet не разрешён)"
    return "TESTNET" if BYBIT_TESTNET else "РЕАЛЬНЫЙ СЧЁТ"


def _api_call(method, action, **params):
    """Route one exchange call through pacing/cooldown; never replay it."""
    return call_bybit(lambda: method(**params), action=action)


def _check_response(response, action):
    if not isinstance(response, dict) or type(response.get("retCode")) not in (int, str):
        raise RuntimeError(f"Bybit вернул некорректный ответ для {action}")
    code = response["retCode"]
    if code not in (0, "0"):
        raise RuntimeError(f"Bybit {action}: retCode={code}, retMsg={response.get('retMsg', 'unknown error')}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Bybit вернул некорректный result для {action}")
    return result


def _rows(response, action):
    result = _check_response(response, action)
    rows = result.get("list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"Bybit вернул некорректный список для {action}")
    return result, rows


def get_tickers_snapshot():
    _, rows = _rows(_api_call(session.get_tickers, "get_tickers", category="linear"), "get_tickers")
    return rows


def _all_pages(method, action, **params):
    rows = []
    seen = set()
    for _ in range(100):
        result, page = _rows(method(**params), action)
        rows.extend(page)
        cursor = result.get("nextPageCursor", "")
        if cursor in (None, ""):
            return rows
        if not isinstance(cursor, str) or cursor in seen:
            raise RuntimeError(f"Некорректный или повторный cursor для {action}")
        seen.add(cursor)
        params["cursor"] = cursor
    raise RuntimeError(f"Слишком много страниц для {action}; снимок неполон")


def get_positions_snapshot():
    # Monitoring deliberately does not consult the new-order block.
    method = lambda **params: _api_call(session.get_positions, "get_positions", **params)
    return _all_pages(method, "get_positions", category="linear", settleCoin="USDT", limit=200)


def get_closed_pnl_snapshot():
    method = lambda **params: _api_call(session.get_closed_pnl, "get_closed_pnl", **params)
    return _all_pages(method, "get_closed_pnl", category="linear", limit=100)


def _decimal(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Некорректное значение {field}")
    if not result.is_finite():
        raise ValueError(f"Некорректное значение {field}")
    return result


def _floor_to_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_to_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _sync_directory():
    fd = os.open(str(ORDER_BLOCK_FILE.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd, data):
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise OSError("short write of order safety marker")
        data = data[written:]


def _marker_payload(state, reason, token=None, **details):
    return {"version": 1, "state": state, "reason": str(reason)[:1000],
            "token": token, "time": time.time(), **details}


def _create_marker(payload):
    """Create the intent before submission, fsync file and directory."""
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(ORDER_BLOCK_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory()


def _replace_marker(payload):
    """Atomically update a marker without ever opening a no-marker window."""
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".bybit-block-", dir=str(ORDER_BLOCK_FILE.parent))
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, ORDER_BLOCK_FILE)
        _sync_directory()
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _read_marker():
    # lstat treats dangling links and malformed/unreadable files as blocks too.
    try:
        ORDER_BLOCK_FILE.lstat()
    except FileNotFoundError:
        return None
    try:
        if ORDER_BLOCK_FILE.is_symlink():
            return {"state": "unknown", "reason": "safety marker is a symlink"}
        with ORDER_BLOCK_FILE.open("r", encoding="utf-8") as stream:
            marker = json.load(stream)
        if not isinstance(marker, dict):
            raise ValueError("marker is not an object")
        return marker
    except (OSError, ValueError, UnicodeError):
        return {"state": "unknown", "reason": "safety marker is unreadable or malformed"}


@contextmanager
def _exclusive(blocking):
    # The directory must be provisioned in advance: silently creating a missing
    # mount point could use an ephemeral container filesystem instead of a volume.
    with _PROCESS_LOCK:
        if not ORDER_BLOCK_FILE.parent.is_dir():
            raise RuntimeError(f"Persistent volume directory is unavailable: {ORDER_BLOCK_FILE.parent}")
        fd = os.open(str(_ORDER_LOCK_FILE), os.O_RDWR | os.O_CREAT |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def get_order_block_status():
    """Return {blocked, state, reason, path}; never gates monitoring or closing."""
    if _PROCESS_BLOCKED:
        return {"blocked": True, "state": "process_error", "reason": "Ошибка сохранения предохранителя; перезапуск без проверки опасен", "path": str(ORDER_BLOCK_FILE)}
    try:
        if not ORDER_BLOCK_FILE.parent.is_dir():
            raise RuntimeError("persistent volume directory unavailable")
        marker = _read_marker()
        return {"blocked": marker is not None, "state": marker.get("state") if marker else None,
                "reason": marker.get("reason", "") if marker else "", "path": str(ORDER_BLOCK_FILE)}
    except Exception as exc:
        return {"blocked": True, "state": "error", "reason": str(exc), "path": str(ORDER_BLOCK_FILE)}


def _manual_review_log(reason):
    print(f"[TRADING] КРИТИЧНО: новые ордера ВСЕХ символов заблокированы: {reason}. "
          f"Нужен ручной разбор позиции, ордеров и истории Bybit; только пользователь "
          f"после проверки может удалить флаг {ORDER_BLOCK_FILE}. Не перезапускайте "
          "торговлю, пока не удостоверитесь, что volume постоянный.", flush=True)


def block_new_orders(reason):
    """Persist a global manual-review block, e.g. after a SQLite save failure.

    Returns True if the marker was persisted, False on I/O/locking error. Even
    on failure this process latches closed; the operator must disable trading.
    """
    global _PROCESS_BLOCKED, _CONFIRMED_RECORD_TOKEN
    _CONFIRMED_RECORD_TOKEN = None
    try:
        with _exclusive(blocking=True):
            payload = _marker_payload("manual_review_required", reason)
            if _read_marker() is None:
                _create_marker(payload)
            else:
                _replace_marker(payload)
        _manual_review_log(reason)
        return True
    except Exception as exc:
        _PROCESS_BLOCKED = True
        _manual_review_log(f"НЕ УДАЛОСЬ НАДЁЖНО СОХРАНИТЬ ФЛАГ: {exc}; {reason}; немедленно отключите торговлю")
        return False


def clear_order_block_after_record():
    """Release only this process's confirmed trade, after its SQLite commit.

    Uncertain/manual flags and flags left by a previous process CANNOT be cleared
    here; those require a user's manual investigation and removal.
    """
    global _CONFIRMED_RECORD_TOKEN, _PROCESS_BLOCKED
    try:
        with _exclusive(blocking=True):
            marker = _read_marker()
            if (_PROCESS_BLOCKED or not _CONFIRMED_RECORD_TOKEN or not marker or
                    marker.get("state") != "confirmed_awaiting_record" or
                    marker.get("token") != _CONFIRMED_RECORD_TOKEN):
                print("[TRADING] Флаг не снят: нет подтверждённой сделки текущего процесса либо требуется ручная проверка.", flush=True)
                return False
            os.unlink(ORDER_BLOCK_FILE)
            try:
                _sync_directory()
            except Exception:
                # No entry may follow an ambiguous persistence result.
                _PROCESS_BLOCKED = True
                try:
                    _create_marker(_marker_payload("manual_review_required", "Ошибка фиксации снятия флага после записи сделки"))
                except Exception:
                    pass
                raise
            _CONFIRMED_RECORD_TOKEN = None
        print("[TRADING] Подтверждённая сделка записана; флаг новых входов снят.", flush=True)
        return True
    except Exception as exc:
        _PROCESS_BLOCKED = True
        _CONFIRMED_RECORD_TOKEN = None
        _manual_review_log(f"ошибка снятия флага: {exc}; немедленно отключите торговлю")
        return False


def _positions_for_symbol(symbol):
    response = _api_call(session.get_positions, "get_positions", category="linear", symbol=symbol)
    result, rows = _rows(response, "get_positions")
    if result.get("nextPageCursor"):
        raise RuntimeError("Неполный список позиций")
    for row in rows:
        if row.get("symbol") != symbol or str(row.get("positionIdx")) != "0":
            raise RuntimeError("Неизвестная позиция или hedge mode: требуется one-way positionIdx=0")
        if _decimal(row.get("size"), "position size") < 0:
            raise RuntimeError("Некорректный размер позиции")
    return rows


def _assert_no_existing_exposure(symbol):
    for position in _positions_for_symbol(symbol):
        if _decimal(position.get("size"), "position size") != 0:
            raise RuntimeError(f"Для {symbol} уже существует позиция")
    response = _api_call(session.get_open_orders, "get_open_orders", category="linear", symbol=symbol, openOnly=0, limit=50)
    result, orders = _rows(response, "get_open_orders")
    if result.get("nextPageCursor"):
        raise RuntimeError("Неполный список незавершённых ордеров")
    if orders:
        raise RuntimeError(f"Для {symbol} есть незавершённый ордер или ответ неоднозначен")


def _instrument(symbol):
    response = _api_call(session.get_instruments_info, "get_instruments_info", category="linear", symbol=symbol)
    result, rows = _rows(response, "get_instruments_info")
    if result.get("nextPageCursor") or len(rows) != 1:
        raise RuntimeError("Инструмент не найден или данные неоднозначны")
    info = rows[0]
    if (info.get("symbol") != symbol or info.get("status") != "Trading" or
            info.get("contractType") != "LinearPerpetual" or
            info.get("quoteCoin") != "USDT" or info.get("settleCoin") != "USDT"):
        raise RuntimeError("Разрешён только активный USDT linear perpetual")
    lot, price = info.get("lotSizeFilter"), info.get("priceFilter")
    if not isinstance(lot, dict) or not isinstance(price, dict):
        raise RuntimeError("Отсутствуют биржевые фильтры")
    minimum = _decimal(lot.get("minOrderQty"), "minOrderQty")
    step = _decimal(lot.get("qtyStep"), "qtyStep")
    maximum = _decimal(lot.get("maxMktOrderQty"), "maxMktOrderQty")
    notional = _decimal(lot.get("minNotionalValue"), "minNotionalValue")
    tick = _decimal(price.get("tickSize"), "tickSize")
    if min(minimum, step, maximum, notional, tick) <= 0 or maximum < minimum:
        raise RuntimeError("Некорректные биржевые фильтры")
    return minimum, step, maximum, notional, tick, price


def _quote(symbol):
    response = _api_call(session.get_tickers, "get_tickers", category="linear", symbol=symbol)
    result, rows = _rows(response, "get_tickers")
    if result.get("nextPageCursor") or len(rows) != 1 or rows[0].get("symbol") != symbol:
        raise RuntimeError("Нет однозначной котировки символа")
    row = rows[0]
    prices = {name: _decimal(row.get(name), name) for name in ("lastPrice", "markPrice", "bid1Price", "ask1Price")}
    if min(prices.values()) <= 0 or prices["bid1Price"] > prices["ask1Price"]:
        raise RuntimeError("Некорректная котировка")
    return prices


def _within_bounds(value, direction, stop, take):
    return stop < value < take if direction == "LONG" else take < value < stop


def _find_order(symbol, order_id):
    # Realtime first, history second because completed orders may disappear from
    # the former. An absent record is not evidence that the order was unfilled.
    for method, action in ((session.get_open_orders, "get_open_orders"), (session.get_order_history, "get_order_history")):
        result, rows = _rows(method(category="linear", symbol=symbol, orderId=order_id, limit=50), action)
        if result.get("nextPageCursor") or len(rows) > 1:
            raise RuntimeError("Неоднозначная история ордера")
        if rows:
            if rows[0].get("orderId") != order_id or rows[0].get("symbol") != symbol:
                raise RuntimeError("История вернула другой ордер")
            return rows[0]
    return None


def _confirm_order(symbol, order_id, link_id, direction, final_qty, take, stop, reference, max_pct, risk_budget):
    side = "Buy" if direction == "LONG" else "Sell"
    deadline = time.monotonic() + _CONFIRM_TIMEOUT_SECONDS
    emergency = False
    for attempt in range(_CONFIRM_MAX_ATTEMPTS):
        order = _find_order(symbol, order_id)
        positions = _positions_for_symbol(symbol)
        live = [p for p in positions if _decimal(p.get("size"), "position size") > 0]
        if order and order.get("orderStatus") == "Filled":
            if (order.get("orderLinkId") != link_id or order.get("orderType") != "Market" or
                    order.get("reduceOnly") is not False or order.get("side") != side or
                    str(order.get("positionIdx")) != "0" or
                    _decimal(order.get("qty"), "order qty") != final_qty or
                    _decimal(order.get("cumExecQty"), "cumExecQty") != final_qty):
                raise RuntimeError("Исполнение ордера не совпадает с запросом")
            if len(live) == 1 and live[0].get("side") == side and _decimal(live[0].get("size"), "position size") == final_qty:
                pos = live[0]
                # Position TP/SL prove protection is on the actual position;
                # the order record proves the requested trigger sources.
                protection = (pos.get("takeProfit") not in (None, "") and pos.get("stopLoss") not in (None, "") and
                              _decimal(pos["takeProfit"], "position takeProfit") == take and
                              _decimal(pos["stopLoss"], "position stopLoss") == stop and
                              order.get("takeProfit") not in (None, "") and order.get("stopLoss") not in (None, "") and
                              _decimal(order["takeProfit"], "order takeProfit") == take and
                              _decimal(order["stopLoss"], "order stopLoss") == stop and
                              order.get("tpTriggerBy") == "LastPrice" and order.get("slTriggerBy") == "LastPrice")
                emergency = not protection
                if protection:
                    order_avg = _decimal(order.get("avgPrice"), "order avgPrice")
                    pos_avg = _decimal(pos.get("avgPrice"), "position avgPrice")
                    if order_avg != pos_avg or order_avg <= 0:
                        raise RuntimeError("Цена исполнения ордера и позиции не совпадает")
                    if (not _within_bounds(order_avg, direction, stop, take) or
                            abs(order_avg - reference) / reference * 100 > max_pct or
                            final_qty * abs(order_avg - stop) > risk_budget):
                        emergency = True
                    else:
                        return
        if attempt + 1 >= _CONFIRM_MAX_ATTEMPTS or time.monotonic() >= deadline:
            break
        time.sleep(min(_CONFIRM_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    if emergency:
        print(f"[TRADING] {symbol}: подтверждена позиция, но защита/риск не подтверждены; аварийное reduceOnly закрытие.", flush=True)
        close_position(symbol, expected_side=side, expected_qty=final_qty)
    raise RuntimeError("Исполнение и защита не подтверждены в установленный срок")


def place_real_trade(symbol, direction, qty, tp, sl, entry_price=None):
    """Return (confirmed_and_protected, actual_qty); None reference is rejected.

    Success leaves the global durable marker in place until the caller commits
    its trade record and calls clear_order_block_after_record(). Any ambiguous
    outcome remains blocked and requires a user's manual investigation.
    """
    global _PROCESS_BLOCKED, _CONFIRMED_RECORD_TOKEN
    if _CONFIG_ERRORS or not TRADING_ENABLED or (not BYBIT_TESTNET and not ALLOW_MAINNET_TRADING):
        print(f"[TRADING] Ордер запрещён: {mode_label()}; {'; '.join(_CONFIG_ERRORS)}", flush=True)
        return False, 0.0
    if not API_KEY or not API_SECRET:
        print("[TRADING] Ордер запрещён: нет ключей Bybit.", flush=True)
        return False, 0.0
    sent_intent = False
    try:
        with _exclusive(blocking=False):
            if _PROCESS_BLOCKED or _read_marker() is not None:
                _manual_review_log("действует глобальный предохранитель или ожидается запись сделки")
                return False, 0.0
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{2,30}USDT", symbol):
                raise ValueError("Некорректный USDT symbol")
            if direction not in {"LONG", "SHORT"}:
                raise ValueError("Direction must be LONG or SHORT")
            requested_qty = _decimal(qty, "qty")
            raw_take, raw_stop = _decimal(tp, "take profit"), _decimal(sl, "stop loss")
            if entry_price is None:
                raise ValueError("Нужен entry_price: без ориентира нельзя проверить отклонение цены")
            reference = _decimal(entry_price, "entry price")
            max_pct = _decimal(os.getenv("MAX_ENTRY_DEVIATION_PCT", "0.2"), "MAX_ENTRY_DEVIATION_PCT")
            if not (Decimal("0.01") <= max_pct <= Decimal("1")):
                raise ValueError("MAX_ENTRY_DEVIATION_PCT должен быть от 0.01 до 1 процента")
            if min(requested_qty, raw_take, raw_stop, reference) <= 0 or not _within_bounds(reference, direction, raw_stop, raw_take):
                raise ValueError("Неверные параметры входа, стопа или цели")
            min_qty, step, max_qty, min_notional, tick, price_filter = _instrument(symbol)
            take = _floor_to_step(raw_take, tick) if direction == "LONG" else _ceil_to_step(raw_take, tick)
            stop = _ceil_to_step(raw_stop, tick) if direction == "LONG" else _floor_to_step(raw_stop, tick)
            if take <= 0 or stop <= 0 or not _within_bounds(reference, direction, stop, take):
                raise ValueError("Цены после округления не окружают ориентир")
            for price_name in ("minPrice", "maxPrice"):
                value = price_filter.get(price_name)
                if value not in (None, ""):
                    bound = _decimal(value, price_name)
                    if bound <= 0 or (price_name == "minPrice" and min(take, stop) < bound) or (price_name == "maxPrice" and max(take, stop) > bound):
                        raise ValueError("SL/TP вне биржевого ценового диапазона")
            _assert_no_existing_exposure(symbol)
            try:
                leverage_response = _api_call(
                    session.set_leverage, "set_leverage", category="linear", symbol=symbol,
                    buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE),
                )
                if not (isinstance(leverage_response, dict) and leverage_response.get("retCode") in (110043, "110043")):
                    _check_response(leverage_response, "set_leverage")
            except InvalidRequestError as exc:
                if str(exc.status_code) != "110043":
                    raise
            prices = _quote(symbol)  # same HTTP session/environment as order
            for price in prices.values():
                if not _within_bounds(price, direction, stop, take) or abs(price - reference) / reference * 100 > max_pct:
                    raise ValueError("Котировка вне SL/TP или слишком далеко от ориентира")
            fraction = max_pct / 100
            if direction == "LONG":
                worst_limit = min(reference * (1 + fraction), take - tick)
                slip = (worst_limit / prices["ask1Price"] - 1) * 100
            else:
                worst_limit = max(reference * (1 - fraction), take + tick)
                slip = (1 - worst_limit / prices["bid1Price"]) * 100
            # Bybit percent tolerance has two decimal places and min 0.01%.
            slip = _floor_to_step(min(slip, max_pct), Decimal("0.01"))
            if slip < Decimal("0.01"):
                raise ValueError("Недостаточный запас для безопасного рыночного проскальзывания")
            risk_budget = requested_qty * abs(reference - raw_stop)
            capped_fill = (prices["ask1Price"] * (1 + slip / 100) if direction == "LONG"
                           else prices["bid1Price"] * (1 - slip / 100))
            adverse = (max(reference, capped_fill, *prices.values()) if direction == "LONG"
                       else min(reference, capped_fill, *prices.values()))
            risk_per_unit = abs(adverse - stop)
            if risk_per_unit <= 0:
                raise ValueError("Некорректный риск на единицу")
            final_qty = _floor_to_step(min(requested_qty, max_qty, risk_budget / risk_per_unit), step)
            if final_qty < min_qty or final_qty * min(reference, *prices.values()) < min_notional:
                raise ValueError("Количество или notional ниже минимума; объём не увеличивается")
            # Intent is on disk and fsynced BEFORE any possible network submission.
            token = uuid.uuid4().hex
            link_id = "safe" + token
            _create_marker(_marker_payload("submission_uncertain", "Возможна отправка ордера; нужна ручная проверка", token,
                                           symbol=symbol, orderLinkId=link_id, testnet=BYBIT_TESTNET))
            sent_intent = True
            response = _api_call(
                session.place_order, "place_order", category="linear", symbol=symbol,
                side="Buy" if direction == "LONG" else "Sell", orderType="Market",
                qty=format(final_qty, "f"), positionIdx=0, orderLinkId=link_id,
                takeProfit=format(take, "f"), stopLoss=format(stop, "f"),
                tpTriggerBy="LastPrice", slTriggerBy="LastPrice", tpslMode="Full",
                tpOrderType="Market", slOrderType="Market",
                slippageToleranceType="Percent", slippageTolerance=format(slip, "f"),
            )
            result = _check_response(response, "place_order")
            order_id = result.get("orderId")
            if not isinstance(order_id, str) or not order_id.strip():
                raise RuntimeError("Подтверждение приёма не содержит orderId")
            if result.get("orderLinkId") not in (None, "", link_id):
                raise RuntimeError("Несовпадение orderLinkId")
            print(f"[TRADING] {symbol}: Bybit принял запрос {order_id}; исполнение пока НЕ подтверждено.", flush=True)
            _confirm_order(symbol, order_id, link_id, direction, final_qty, take, stop, reference, max_pct, risk_budget)
            _replace_marker(_marker_payload("confirmed_awaiting_record", "Исполнено и защищено; ожидается запись SQLite", token,
                                            symbol=symbol, orderId=order_id, orderLinkId=link_id, testnet=BYBIT_TESTNET))
            _CONFIRMED_RECORD_TOKEN = token
            print(f"[TRADING] {symbol}: исполнение и TP/SL подтверждены; новые входы заблокированы до записи SQLite.", flush=True)
            return True, float(final_qty)
    except Exception as exc:
        if sent_intent:
            _CONFIRMED_RECORD_TOKEN = None
            _manual_review_log(f"после отправки {symbol} результат/защита не подтверждены: {exc}")
        else:
            print(f"[TRADING] {symbol}: вход не отправлен: {exc}", flush=True)
        return False, 0.0


def close_position(symbol, expected_side=None, expected_qty=None):
    """Best-effort reduceOnly close; True means accepted, not confirmed filled."""
    try:
        positions = _positions_for_symbol(symbol)
        live = [p for p in positions if _decimal(p.get("size"), "position size") > 0]
        if len(live) > 1:
            raise RuntimeError("Неоднозначное количество открытых позиций")
        if expected_side is not None and (len(live) != 1 or live[0].get("side") != expected_side or
                                          _decimal(live[0].get("size"), "position size") != expected_qty):
            raise RuntimeError("Позиция изменилась; аварийное закрытие отменено, нужен ручной разбор")
        for position in positions:
            size = _decimal(position.get("size"), "position size")
            if size == 0:
                continue
            side = position.get("side")
            if side not in ("Buy", "Sell"):
                raise RuntimeError("Неизвестная сторона позиции; закрытие запрещено")
            response = _api_call(
                session.place_order, "emergency_close", category="linear", symbol=symbol,
                side="Sell" if side == "Buy" else "Buy", orderType="Market",
                qty=format(size, "f"), reduceOnly=True, positionIdx=0,
            )
            _check_response(response, "emergency_close")
            print(f"[TRADING] {symbol}: запрос reduceOnly закрытия принят, исполнение не подтверждено.", flush=True)
        return True
    except Exception as exc:
        print(f"[TRADING] Аварийное закрытие {symbol} не подтверждено: {exc}", flush=True)
        return False
