"""Offline tests: synthetic candles and local history; no network or secrets."""

import json
import math
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from backtest import run_backtest
from entry import _signal_from_values, get_entry_signal
from scanner import (ATR_MULTIPLIER, REWARD_MULTIPLE, WARMUP_BARS,
                     _strategy_frame, analyze_trend)


def wave_candles(side=1, count=120):
    x = np.arange(count)
    close = 100 + side * (0.025 * x + 0.7 * np.sin(x * 0.4))
    opening = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": opening, "high": np.maximum(opening, close) + 0.2,
                         "low": np.minimum(opening, close) - 0.2, "close": close})


def engine_frame(rows, direction="LONG", signal_index=0):
    """Controlled signal at close=100, ATR=0.5: risk=1, reward=2."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame = df.copy()
    frame["atr"] = 0.5
    frame["direction"] = "НЕТ"
    frame.loc[signal_index, "direction"] = direction
    return df, frame


def run_engine(rows, direction="LONG", signal_index=0, **kwargs):
    df, frame = engine_frame(rows, direction, signal_index)
    # Inject a known signal, keeping execution and level calculation real. Other
    # tests exercise the actual indicators and the full, unmocked integration.
    with patch("backtest._strategy_frame", return_value=frame):
        return run_backtest(df, **kwargs)


class SignalTests(unittest.TestCase):
    def test_warmup_exactly_97_bars_and_levels(self):
        self.assertEqual(WARMUP_BARS, 97)
        self.assertEqual((ATR_MULTIPLIER, REWARD_MULTIPLE), (2.0, 2.0))
        for side, expected in ((1, "LONG"), (-1, "SHORT")):
            with self.subTest(direction=expected):
                close = np.r_[[100.0] * 95, 100.0 + side * 2, 100.0 + side * 4]
                opening = np.r_[close[0], close[:-1]]
                df = pd.DataFrame({"open": opening, "high": np.maximum(opening, close) + 0.2,
                                   "low": np.minimum(opening, close) - 0.2, "close": close})
                # Candle 95 breaks the preceding 95 highs/lows, but 96 bars
                # still cannot provide 96 *previous* candles.
                for count in (95, 96):
                    self.assertEqual(analyze_trend(df.iloc[:count])[0], "НЕТ")
                    self.assertIsNone(get_entry_signal(df.iloc[:count], expected))
                self.assertEqual(_strategy_frame(df).direction.iloc[:96].tolist(), ["НЕТ"] * 96)
                direction, price, rsi, atr = analyze_trend(df)
                self.assertEqual(direction, expected)
                self.assertTrue(math.isfinite(rsi) and atr > 0)
                signal = get_entry_signal(df, direction)
                self.assertEqual(signal["direction"], expected)
                self.assertEqual(signal["entry"], price)
                self.assertAlmostEqual(signal["stop_loss"], price - side * 2 * atr)
                self.assertAlmostEqual(signal["take_profit"], price + side * 4 * atr)
                self.assertEqual(signal["risk_reward"], 2.0)
                self.assertIsNone(get_entry_signal(df, "SHORT" if side == 1 else "LONG"))

    def test_strict_channel_boundaries_and_exclude_current_wick(self):
        base = [(100.0, 101.0, 99.0, 100.0)] * 96
        for side, boundary, breakout, expected in ((1, 101.0, 101.01, "LONG"),
                                                    (-1, 99.0, 98.99, "SHORT")):
            with self.subTest(direction=expected):
                # The current high/low extends far beyond the close. Including
                # it in the channel would incorrectly suppress the breakout.
                df = pd.DataFrame(base + [(100.0, 110.0, 90.0, boundary)],
                                  columns=["open", "high", "low", "close"])
                self.assertEqual(analyze_trend(df)[0], "НЕТ")  # equality is not a breakout
                df.loc[96, "close"] = breakout
                self.assertEqual(analyze_trend(df)[0], expected)
                self.assertIsNotNone(get_entry_signal(df, expected))
                # A wick beyond the channel without a closing breakout is not enough.
                df.loc[96, "close"] = 100.0
                self.assertEqual(analyze_trend(df)[0], "НЕТ")

    def test_exact_96_bar_window_includes_oldest_then_expires(self):
        for side, expected in ((1, "LONG"), (-1, "SHORT")):
            with self.subTest(direction=expected):
                rows = [(100.0, 101.0, 99.0, 100.0)] * 96
                rows[0] = (100.0, 110.0 if side == 1 else 101.0,
                           90.0 if side == -1 else 99.0, 100.0)
                rows += [(100.0, 102.1 if side == 1 else 100.1,
                          99.9 if side == 1 else 97.9, 100.0 + side * 2),
                         (100.0 + side * 2, 103.1 if side == 1 else 98.1,
                          101.9 if side == 1 else 96.9, 100.0 + side * 3)]
                df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
                self.assertEqual(analyze_trend(df.iloc[:97])[0], "НЕТ")
                self.assertEqual(analyze_trend(df)[0], expected)

    def test_no_rsi_or_ema_filters(self):
        for side, expected in ((1, "LONG"), (-1, "SHORT")):
            with self.subTest(direction=expected):
                # A counter-trend jump breaks the channel despite an opposing
                # EMA relationship and slope and an extreme RSI reading.
                close = np.r_[100 - side * np.linspace(0, 20, 96), 100 + side * 1.2]
                opening = np.r_[close[0], close[:-1]]
                df = pd.DataFrame({"open": opening, "high": np.maximum(opening, close) + 0.1,
                                   "low": np.minimum(opening, close) - 0.1, "close": close})
                frame = _strategy_frame(df)
                self.assertLess(side * (frame.ema20.iloc[-1] - frame.ema50.iloc[-1]), 0)
                self.assertLess(side * (frame.ema50.iloc[-1] - frame.ema50.iloc[-6]), 0)
                self.assertTrue(frame.rsi.iloc[-1] > 70 if side == 1 else frame.rsi.iloc[-1] < 30)
                self.assertEqual(analyze_trend(df)[0], expected)
                self.assertIsNotNone(get_entry_signal(df, expected))

    def test_signal_conditions_and_no_lookahead(self):
        for side, expected in ((1, "LONG"), (-1, "SHORT")):
            with self.subTest(direction=expected):
                df = wave_candles(side, count=300)
                before = df.copy(deep=True)
                full = _strategy_frame(df)
                self.assertIn(expected, full.direction.values)
                for i in range(90, len(df)):
                    trend, price, rsi, atr = analyze_trend(df.iloc[:i + 1])
                    row = full.iloc[i]
                    self.assertEqual(trend, row["direction"])
                    self.assertAlmostEqual(price, row["close"])
                    self.assertAlmostEqual(rsi, row["rsi"])
                    self.assertAlmostEqual(atr, row["atr"])
                    signal = get_entry_signal(df.iloc[:i + 1], trend)
                    if trend == "LONG":
                        self.assertGreater(price, df.high.iloc[i - 96:i].max())
                        self.assertIsNotNone(signal)
                    elif trend == "SHORT":
                        self.assertLess(price, df.low.iloc[i - 96:i].min())
                        self.assertIsNotNone(signal)
                    else:
                        self.assertIsNone(signal)
                # Mutating all future bars leaves the full earlier frame unchanged.
                altered = df.copy(deep=True)
                altered.loc[150:, ["open", "high", "low", "close"]] *= 1.2
                assert_frame_equal(full.iloc[:150], _strategy_frame(altered).iloc[:150])
                assert_frame_equal(df, before)

    def test_wilder_atr_seed_and_recursive_smoothing(self):
        close = np.arange(70, dtype=float) + 100.0
        df = pd.DataFrame({"open": close, "high": close + 1,
                           "low": close - 1, "close": close})
        df.loc[14, "high"] += 7.0
        df.loc[15, ["open", "high", "low", "close"]] = [125.0, 126.0, 124.0, 125.0]
        frame = _strategy_frame(df)
        self.assertTrue(math.isnan(frame.atr.iloc[12]))
        self.assertEqual(frame.atr.iloc[13], 2.0)  # mean TR of first 14, including bar 0
        self.assertAlmostEqual(frame.atr.iloc[14], (13 * 2.0 + 9.0) / 14)
        # Gap: high 126 minus previous close 114 beats the 2-point bar range.
        self.assertAlmostEqual(frame.atr.iloc[15], (13 * frame.atr.iloc[14] + 12.0) / 14)
        self.assertTrue(math.isnan(frame.rsi.iloc[13]))
        self.assertEqual(frame.rsi.iloc[14], 100.0)
        flat = pd.DataFrame({"high": [101.0] * 97, "low": [99.0] * 97,
                             "close": [100.0] * 97})
        trend, _, rsi, atr = analyze_trend(flat)
        self.assertEqual((trend, rsi, atr), ("НЕТ", 50.0, 2.0))

    def test_nonpositive_atr_or_levels_are_rejected(self):
        flat = pd.DataFrame({"high": [100.0] * 97, "low": [100.0] * 97,
                             "close": [100.0] * 97})
        self.assertEqual(analyze_trend(flat)[0], "НЕТ")
        for invalid_atr in (0, -1, float("nan"), float("inf")):
            self.assertIsNone(_signal_from_values("LONG", 100, invalid_atr))
        self.assertIsNone(_signal_from_values("LONG", float("inf"), 1))
        self.assertIsNone(_signal_from_values("LONG", 1e308, 1e308))
        # Despite actual channel breaks, stop or target respectively equals
        # zero; scanner, entry and backtest all reject the same signal.
        for direction, prior, last in (
                ("LONG", (1.25, 1.75, 0.75, 1.25), (1.25, 2.0, 1.0, 2.0)),
                ("SHORT", (4.75, 5.5, 4.5, 4.75), (4.75, 5.0, 4.0, 4.0))):
            with self.subTest(direction=direction):
                df = pd.DataFrame([prior] * 96 + [last], columns=["open", "high", "low", "close"])
                frame = _strategy_frame(df)
                self.assertEqual(frame.atr.iloc[-1], 1.0)
                if direction == "LONG":
                    self.assertGreater(df.close.iloc[-1], df.high.iloc[:-1].max())
                else:
                    self.assertLess(df.close.iloc[-1], df.low.iloc[:-1].min())
                self.assertEqual(frame.direction.iloc[-1], "НЕТ")
                self.assertIsNone(get_entry_signal(df, direction))
                self.assertIsNone(_signal_from_values(direction, df.close.iloc[-1], 1.0))
                self.assertEqual(run_backtest(df)["unfilled_last_bar_signals"], 0)

    def test_invalid_candles_and_timestamps_fail_closed(self):
        df = wave_candles()
        for column, invalid in (("high", -1), ("close", float("nan")),
                                ("low", float("inf"))):
            bad = df.copy()
            bad.loc[0, column] = invalid
            with self.subTest(column=column), self.assertRaises(ValueError):
                analyze_trend(bad)
        bad = df.copy()
        bad.loc[0, "open"] = bad.loc[0, "high"] + 10
        with self.assertRaises(ValueError):
            run_backtest(bad)
        bad = df.copy()
        bad["timestamp"] = list(range(len(bad) - 1)) + [0]
        with self.assertRaises(ValueError):
            run_backtest(bad)

    def test_empty_and_no_input_mutation(self):
        trend, close, rsi, atr = analyze_trend(pd.DataFrame())
        self.assertEqual(trend, "НЕТ")
        self.assertTrue(all(math.isnan(value) for value in (close, rsi, atr)))
        self.assertIsNone(get_entry_signal(pd.DataFrame(), "LONG"))
        result = run_backtest(pd.DataFrame())
        self.assertEqual((result["total"], result["final_balance"]), (0, 1000.0))
        self.assertEqual(result["equity_curve"], [1000.0])
        df = wave_candles()
        before = df.copy(deep=True)
        analyze_trend(df)
        get_entry_signal(df, "LONG")
        run_backtest(df)
        assert_frame_equal(df, before)

    def test_unmocked_backtest_matches_live_signal_and_prefix(self):
        for side in (1, -1):
            df = wave_candles(side, count=300)
            full = run_backtest(df)
            short = run_backtest(df.iloc[:180])
            full_completed = [t for t in full["trades"] if t["exit_index"] < 179]
            short_completed = [t for t in short["trades"] if t["exit_index"] < 179]
            self.assertEqual(full_completed, short_completed)
            self.assertGreater(len(full_completed), 0)
            for trade in full["trades"]:
                prefix = df.iloc[:trade["signal_index"] + 1]
                direction = analyze_trend(prefix)[0]
                signal = get_entry_signal(prefix, direction)
                self.assertIsNotNone(signal)
                self.assertEqual(trade["direction"], direction)
                self.assertEqual(trade["signal_entry"], signal["entry"])
                self.assertEqual(trade["stop_loss"], signal["stop_loss"])
                self.assertEqual(trade["take_profit"], signal["take_profit"])
                self.assertEqual(trade["entry_index"], trade["signal_index"] + 1)

    def test_oos_starts_flat_without_losing_indicator_warmup(self):
        for side in (1, -1):
            df = wave_candles(side, count=300)
            full = run_backtest(df)
            self.assertEqual(full["trades"][0]["signal_index"], 128)
            starts_at_signal = run_backtest(df, start_index=128)
            self.assertEqual(full["trades"], starts_at_signal["trades"])
            self.assertEqual(starts_at_signal["equity_curve"][0], 1000)
            self.assertEqual(len(starts_at_signal["equity_curve"]), len(df) - 128 + 1)
            # Starting on the next bar cannot inherit the signal at 128.
            oos = run_backtest(df, start_index=129)
            first = oos["trades"][0]
            self.assertEqual((first["signal_index"], first["entry_index"]), (144, 145))
            self.assertEqual(first["balance_before"], 1000)
            self.assertEqual(oos["equity_curve"][0], 1000)
            self.assertEqual(len(oos["equity_curve"]), len(df) - 129 + 1)
            prefix = df.iloc[:145]
            signal = get_entry_signal(prefix, analyze_trend(prefix)[0])
            self.assertEqual(first["stop_loss"], signal["stop_loss"])
            self.assertEqual(first["take_profit"], signal["take_profit"])
            empty = run_backtest(df, start_index=len(df))
            self.assertEqual(empty["total"], 0)
            self.assertEqual(empty["equity_curve"], [1000.0])
            self.assertEqual(empty["final_balance"], 1000.0)
            self.assertEqual(empty["assumptions"]["start_index"], len(df))

    def test_historical_selection_and_oos_regression(self):
        # Replicate research_candidates.main's common-window alignment, then
        # use the original engine and the saved, unrounded research numbers.
        from research_candidates import signal_frame

        root = Path(__file__).resolve().parents[1]
        symbols = ("SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT")
        report = json.loads((root / "candidate_selection.json").read_text(encoding="utf-8"))
        self.assertEqual((report["candidate_count"], report["selection"]["kind"],
                          report["selection"]["atr_multiplier"]), (16, "breakout96", 2.0))
        raw = {s: pd.read_csv(root / "historical_data" / f"{s}_15m.csv",
                              parse_dates=["timestamp"]) for s in symbols}
        start = max(d.timestamp.iloc[0] for d in raw.values())
        end = min(d.timestamp.iloc[-1] for d in raw.values())
        data = {s: d[(d.timestamp >= start) & (d.timestamp <= end)].reset_index(drop=True)
                for s, d in raw.items()}
        self.assertEqual(len({len(d) for d in data.values()}), 1)
        n = len(data[symbols[0]])
        cut = int(n * 0.70)
        self.assertEqual(str(data[symbols[0]].timestamp.iloc[cut]), report["split"])
        train_returns, train_counts = [], []
        for symbol, df in data.items():
            with self.subTest(symbol=symbol):
                # Research direction omits the level-validity check; all four
                # saved data sets have valid derived levels at every breakout.
                actual = _strategy_frame(df)
                reference = signal_frame(df, "breakout96")
                self.assertEqual(actual.direction.tolist(), reference.direction.tolist())
                train = run_backtest(df.iloc[:cut].copy())
                train_returns.append(train["return_pct"])
                train_counts.append(train["total"])
                self.assertEqual(train["return_pct"], report["selection"][symbol + "_return_pct"])
                self.assertEqual(train["total"], report["selection"][symbol + "_trades"])
                oos = run_backtest(df, start_index=cut)
                self.assertEqual({k: oos[k] for k in ("total", "win_rate", "return_pct", "max_drawdown_pct")},
                                 report["oos_exploratory"][symbol])
                self.assertLess(oos["return_pct"], 0)
        self.assertGreaterEqual(min(train_counts), 8)
        self.assertEqual(min(train_counts), report["selection"]["minimum_trades"])
        self.assertEqual(float(np.median(train_returns)), report["selection"]["median_train_return_pct"])
        self.assertLess(np.median(train_returns), 0)

class ExecutionTests(unittest.TestCase):
    # On the signal candle both levels are touched, but no trade exists yet.
    FIRST = (100, 103, 98, 100)

    def test_next_bar_open_fill_and_risk_sizing(self):
        for direction, opening in (("LONG", 100.5), ("SHORT", 99.5)):
            with self.subTest(direction=direction):
                rows = [self.FIRST, (opening, 100.8, 99.2, 100.0)]
                result = run_engine(rows, direction)
                trade = result["trades"][0]
                sign = 1 if direction == "LONG" else -1
                fill = opening * (1 + sign * 0.0002)
                stop = 100 - sign
                target = 100 + 2 * sign
                stop_fill = stop * (1 - sign * 0.0002)
                risk_per_unit = sign * (fill - stop_fill) + 0.0006 * (fill + stop_fill)
                self.assertEqual(trade["signal_index"], 0)
                self.assertEqual(trade["entry_index"], 1)
                self.assertEqual(trade["exit_index"], 1)
                self.assertEqual(trade["exit_reason"], "end_of_data")
                self.assertAlmostEqual(trade["entry_price"], fill)
                self.assertAlmostEqual(trade["stop_loss"], stop)
                self.assertAlmostEqual(trade["take_profit"], target)
                self.assertAlmostEqual(trade["quantity"], 5 / risk_per_unit)
                self.assertAlmostEqual(trade["planned_risk"], 5)

    def test_take_profit_and_stop_for_both_sides(self):
        cases = [("LONG", (100, 102.5, 99.5, 101), "target", 102, 1),
                 ("LONG", (100, 100.5, 98.5, 99.5), "stop", 99, -1),
                 ("SHORT", (100, 100.5, 97.5, 99), "target", 98, 1),
                 ("SHORT", (100, 101.5, 99.5, 100.5), "stop", 101, -1)]
        for direction, bar, reason, reference, pnl_sign in cases:
            with self.subTest(direction=direction, reason=reason):
                result = run_engine([self.FIRST, bar], direction)
                trade = result["trades"][0]
                self.assertEqual(trade["exit_reason"], reason)
                self.assertAlmostEqual(trade["exit_reference"], reference)
                self.assertGreater(trade["net_pnl"] * pnl_sign, 0)
                if reason == "stop":
                    self.assertAlmostEqual(trade["net_pnl"], -5.0)
                self.assertEqual(result["wins"], int(pnl_sign == 1))
                self.assertEqual(result["losses"], int(pnl_sign == -1))

    def test_simultaneous_touch_always_chooses_stop(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                result = run_engine([self.FIRST, (100, 103, 97, 100)], direction)
                self.assertEqual(result["trades"][0]["exit_reason"], "stop")
                self.assertAlmostEqual(result["trades"][0]["net_pnl"], -5.0)

    def test_gap_exit_uses_open_before_same_bar_extremes(self):
        cases = [("LONG", (97, 98, 96, 97), "stop_gap", 97),
                 ("LONG", (104, 105, 98, 100), "target_gap", 104),
                 ("SHORT", (103, 104, 102, 103), "stop_gap", 103),
                 ("SHORT", (96, 102, 95, 100), "target_gap", 96)]
        for direction, gap_bar, reason, reference in cases:
            with self.subTest(direction=direction, reason=reason):
                result = run_engine([self.FIRST, (100, 100.5, 99.5, 100), gap_bar], direction)
                trade = result["trades"][0]
                sign = 1 if direction == "LONG" else -1
                self.assertEqual(trade["exit_index"], 2)
                self.assertEqual(trade["exit_reason"], reason)
                self.assertAlmostEqual(trade["exit_reference"], reference)
                self.assertAlmostEqual(trade["exit_price"], reference * (1 - sign * 0.0002))
                if reason == "stop_gap":
                    self.assertLess(trade["net_pnl"], -5)

    def test_invalid_open_or_slipped_fill_skips_signal(self):
        cases = [("LONG", 99), ("LONG", 102), ("SHORT", 101), ("SHORT", 98),
                 ("LONG", 98), ("SHORT", 102),
                 ("LONG", 101.99), ("SHORT", 98.01)]
        for direction, opening in cases:
            with self.subTest(direction=direction, opening=opening):
                result = run_engine([self.FIRST, (opening, opening + 0.1,
                                                   opening - 0.1, opening)], direction)
                self.assertEqual(result["total"], 0)
                self.assertEqual(result["skipped_signals"], 1)
                self.assertEqual(result["final_balance"], 1000.0)

    def test_fees_slippage_and_profit_factor(self):
        bar = (100, 102.5, 99.5, 101)
        with_costs = run_engine([self.FIRST, bar])
        without = run_engine([self.FIRST, bar], commission_rate=0, slippage_rate=0)
        trade = with_costs["trades"][0]
        qty = trade["quantity"]
        self.assertAlmostEqual(trade["entry_price"], 100 * 1.0002)
        self.assertAlmostEqual(trade["exit_price"], 102 * 0.9998)
        self.assertAlmostEqual(trade["entry_fee"], qty * trade["entry_price"] * 0.0006)
        self.assertAlmostEqual(trade["exit_fee"], qty * trade["exit_price"] * 0.0006)
        self.assertAlmostEqual(with_costs["total_fees"], trade["entry_fee"] + trade["exit_fee"])
        self.assertAlmostEqual(with_costs["total_slippage"],
                               qty * ((trade["entry_price"] - 100) + (102 - trade["exit_price"])))
        self.assertAlmostEqual(with_costs["final_balance"], 1000 + trade["net_pnl"])
        self.assertAlmostEqual(without["final_balance"], 1010.0)
        self.assertLess(with_costs["final_balance"], without["final_balance"])
        self.assertTrue(math.isinf(with_costs["profit_factor"]))
        self.assertEqual(with_costs["win_rate"], 100)

    def test_leverage_cap_after_entry_fee(self):
        result = run_engine([self.FIRST, (100, 100.5, 99.5, 100)], risk_fraction=1.0)
        trade = result["trades"][0]
        self.assertAlmostEqual(trade["entry_notional"],
                               3 * (1000 - trade["entry_fee"]))
        self.assertLessEqual(trade["entry_notional"], 3 * 1000)
        self.assertLess(trade["planned_risk"], 1000)

    def test_close_at_end_and_last_signal_not_filled(self):
        result = run_engine([self.FIRST, (100, 100.5, 99.5, 100.2)])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "end_of_data")
        self.assertAlmostEqual(result["trades"][0]["exit_reference"], 100.2)
        last_signal = run_engine([self.FIRST], signal_index=0)
        self.assertEqual(last_signal["total"], 0)
        self.assertEqual(last_signal["unfilled_last_bar_signals"], 1)

    def test_drawdown_equity_and_accounting(self):
        rows = [self.FIRST, (100, 100.5, 99.5, 99.5), (99.5, 100.5, 98.5, 99)]
        result = run_engine(rows)
        trade = result["trades"][0]
        self.assertEqual(len(result["equity_curve"]), len(rows) + 1)
        self.assertAlmostEqual(result["equity_curve"][-1], result["final_balance"])
        self.assertAlmostEqual(result["net_profit"], trade["net_pnl"])
        self.assertAlmostEqual(result["return_pct"], -0.5)
        self.assertAlmostEqual(result["max_drawdown_pct"], 0.5)
        self.assertEqual(result["profit_factor"], 0.0)
        self.assertEqual(result["assumptions"]["funding"], "not modeled")
        self.assertEqual(result["assumptions"]["liquidation"], "not modeled")

    def test_breakeven_and_profit_factor_with_both_wins_and_losses(self):
        flat = run_engine([self.FIRST, (100, 100.5, 99.5, 100)],
                          commission_rate=0, slippage_rate=0)
        self.assertEqual((flat["total"], flat["wins"], flat["losses"], flat["breakeven"]),
                         (1, 0, 0, 1))
        self.assertEqual((flat["win_rate"], flat["profit_factor"]), (0, 0))
        rows = [self.FIRST, (100, 102.5, 99.5, 100), (100, 100.5, 98.5, 99.5)]
        df, frame = engine_frame(rows)
        frame.loc[1, "direction"] = "LONG"  # exit at bar 1, re-signal at its close
        with patch("backtest._strategy_frame", return_value=frame):
            result = run_backtest(df)
        self.assertEqual((result["total"], result["wins"], result["losses"]), (2, 1, 1))
        self.assertEqual(result["trades"][1]["entry_index"], 2)
        winner, loser = result["trades"]
        self.assertAlmostEqual(loser["planned_risk"], 0.005 * winner["balance_after"])
        self.assertAlmostEqual(result["profit_factor"], winner["net_pnl"] / -loser["net_pnl"])
        self.assertAlmostEqual(result["final_balance"], 1000 + winner["net_pnl"] + loser["net_pnl"])
        json.dumps(result, allow_nan=False)  # finite-PF runs use native JSON numeric types

    def test_extreme_gap_can_bankrupt_without_fictitious_liquidation(self):
        rows = [self.FIRST, (100, 100.5, 99.5, 100),
                (100000, 100001, 99999, 100000), (100000, 100001, 99999, 100000)]
        df, frame = engine_frame(rows, direction="SHORT")
        frame.loc[2, "direction"] = "SHORT"
        with patch("backtest._strategy_frame", return_value=frame):
            result = run_backtest(df)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "stop_gap")
        self.assertLess(result["final_balance"], 0)
        self.assertGreater(result["max_drawdown_pct"], 100)

    def test_parameter_validation(self):
        for kwargs in ({"initial_balance": 0}, {"risk_fraction": 0},
                       {"risk_fraction": 1.1}, {"max_leverage": 0},
                       {"slippage_rate": 1}, {"commission_rate": -1},
                       {"initial_balance": float("nan")}, {"start_index": -1},
                       {"start_index": 121}, {"start_index": 2.0}, {"start_index": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_backtest(wave_candles(), **kwargs)
        with self.assertRaises(ValueError):
            run_backtest(wave_candles().drop(columns="open"))

    def test_10000_candles_are_fast_and_linear(self):
        df = wave_candles(count=10_000)
        start = time.perf_counter()
        result = run_backtest(df)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result["equity_curve"]), 10_001)
        self.assertLess(elapsed, 5.0, f"10k candles took {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
