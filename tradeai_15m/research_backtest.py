"""Reproduce a fixed-parameter chronological train/holdout study from local CSVs.

No order placement and no parameter optimization. Results are per-symbol, not
an aggregate portfolio; they are simplified and not a live-trading forecast.
"""
import argparse
import json
from pathlib import Path
import pandas as pd
from backtest import run_backtest

SYMBOLS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]


def benchmark(df, start, fee, slip, initial=1000.0):
    if start + 1 >= len(df):
        return 0.0
    entry = float(df.iloc[start + 1]["open"]) * (1 + slip)
    exit_price = float(df.iloc[-1]["close"]) * (1 - slip)
    qty = initial / (entry * (1 + fee))
    return 100 * (qty * exit_price * (1 - fee) / initial - 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="historical_data")
    parser.add_argument("--out", default="BACKTEST_RESULTS_RU.md")
    args = parser.parse_args()
    manifest_path = Path(args.data) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    retrieved_at = manifest.get("retrieved_at_utc", "не указано")
    raw = {s: pd.read_csv(Path(args.data) / f"{s}_15m.csv", parse_dates=["timestamp"]) for s in SYMBOLS}
    first = max(d.timestamp.iloc[0] for d in raw.values())
    last = min(d.timestamp.iloc[-1] for d in raw.values())
    frames = {s: d[(d.timestamp >= first) & (d.timestamp <= last)].reset_index(drop=True) for s, d in raw.items()}
    for s, d in frames.items():
        if d.empty or d.timestamp.duplicated().any() or (d.timestamp.diff().dropna() != pd.Timedelta(minutes=15)).any():
            raise ValueError(f"{s}: incomplete or duplicated history")
    lengths = {len(d) for d in frames.values()}
    if len(lengths) != 1:
        raise ValueError("Different data coverage")
    count = lengths.pop()
    cut = int(count * 0.70)
    cutoff = frames[SYMBOLS[0]].timestamp.iloc[cut]
    lines = [
        "# Результаты исследования стратегии 15 минут", "",
        "Это историческая симуляция, а не обещание прибыли и не готовое основание для торговли. Сравнивались 16 заранее заданных вариантов; один выбран по медианной доходности на первых 70% истории. Результаты всех вариантов находятся в `candidate_trials.csv`. Это поиск с риском переобучения, а не доказательство оптимальности.", "",
        "Выбранный вариант: пробой закрытием максимума или минимума предыдущих 96 свечей; стоп 2 × ATR(14), цель 2R. Лучший результат по выбранному критерию на обучающем периоде не означает прибыльности стратегии.", "",
        f"Источник: публичный API Bybit, категория `linear`, USDT perpetual, 15-минутные свечи, mainnet: [Get Kline](https://bybit-exchange.github.io/docs/v5/market/kline). Время загрузки по манифесту: {retrieved_at}; сведения по файлам — в `manifest.json`.", "",
        f"Общий период: начало первой свечи **{first}**, начало последней **{last}**; {count} свечей на монету, без пропусков и дубликатов. Начало последней свечи не является временем её закрытия.", "",
        f"Первые 70% свечей — период подбора; последние 30% — исследовательская проверка (свеча сигнала не раньше **{cutoff}**). Результат первоначального варианта на позднем периоде был просмотрен до расширения поиска, поэтому этот период уже нельзя считать полностью независимым. Предшествующая история используется только для прогрева индикаторов; проверка начинается без позиции и ордеров, с балансом 1000 USDT.", "",
        "На каждую монету отдельно: 1000 USDT, номинальный риск 0,5% от капитала на сделку, максимум 3x номинала, комиссия **0,06%** и проскальзывание **0,02%** за каждую сторону. Сигнал на закрытии, вход не раньше открытия следующей свечи; одновременное касание SL/TP разрешается в пользу SL. На последней свече открытая позиция принудительно закрывается. Все числа условные и не гарантируют соответствия фактическим тарифам.", "",
        "## Исследовательская проверка: последние 30%", "",
        "| Монета | Сделок | Доля прибыльных | Доходность стратегии | Макс. просадка | Buy & hold 1x* | Стресс по издержкам** |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    train_lines = ["", "## Первые 70% истории", "", "| Монета | Сделок | Доля прибыльных | Доходность | Макс. просадка |", "|---|---:|---:|---:|---:|"]
    for symbol in SYMBOLS:
        df = frames[symbol]
        oos = run_backtest(df, start_index=cut)
        stress = run_backtest(df, start_index=cut, commission_rate=0.0012, slippage_rate=0.0004)
        train = run_backtest(df.iloc[:cut].copy())
        hold = benchmark(df, cut, 0.0006, 0.0002)
        lines.append(f"| {symbol} | {oos['total']} | {oos['win_rate']:.1f}% | {oos['return_pct']:+.2f}% | {oos['max_drawdown_pct']:.2f}% | {hold:+.2f}% | {stress['return_pct']:+.2f}% |")
        train_lines.append(f"| {symbol} | {train['total']} | {train['win_rate']:.1f}% | {train['return_pct']:+.2f}% | {train['max_drawdown_pct']:.2f}% |")
    lines.extend(train_lines)
    lines.extend(["", "* Buy & hold — условная несбалансированная длинная позиция 1x на тот же период, с теми же комиссиями и проскальзыванием, без funding; не является эквивалентным по риску бенчмарком для стратегии с LONG и SHORT. Доходность каждого символа посчитана на отдельном счёте — складывать их как доходность портфеля нельзя.", "", "** Стресс: комиссия 0,12% за сторону и проскальзывание 0,04% за сторону; остальные параметры прежние.", "", "### Ограничения", "", "Симуляция не учитывает funding, ликвидацию, реальные спреды/стакан, задержку сканирования до нескольких минут, объём и частичное исполнение, минимальные объёмы и округление биржи. Рыночный гэп может превысить плановый риск. Бот в действительности удерживает не более одной позиции среди всех монет; независимые тесты по отдельным инструментам этого ограничения не воспроизводят. Тест использует всю предшествующую историю для индикаторов, а бот запрашивает последние 250 свечей, поэтому в пограничных случаях значения EMA/RSI/ATR и сигнал могут отличаться. Просадка считается на закрытии свечи, а не внутри неё. Последние 27 дней и только один режим рынка недостаточны для вывода об устойчивости. Реальные результаты, тем более будущие, могут существенно отличаться.", "", "Повтор: `python research_backtest.py --data historical_data --out BACKTEST_RESULTS_RU.md`. Данные также можно заново загрузить `python download_history.py --days 90`; обновлённая история даст другие результаты.", ""])
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
