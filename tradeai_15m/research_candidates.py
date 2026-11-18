"""Exploratory 16-candidate training-only comparison; no orders, no future bars.

Selection uses only the first 70% of the common historical data, with median
return across symbols and at least 8 closed trades per symbol. It then reports
later-period results as exploratory because this research already examined an
initial candidate's later-period result before broadening the search.
"""
import csv
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import backtest
from scanner import _strategy_frame

SYMBOLS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
CANDIDATES = [(kind, mult) for kind in (
    "pullback", "cross", "breakout20_trend", "breakout55_trend", "breakout96_trend",
    "breakout20", "breakout55", "breakout96") for mult in (1.5, 2.0)]


def signal_frame(df, kind):
    frame = _strategy_frame(df)
    close = frame.close
    rising = frame.ema50 > frame.ema50.shift(5)
    falling = frame.ema50 < frame.ema50.shift(5)
    up = frame.ema20 > frame.ema50
    down = frame.ema20 < frame.ema50
    if kind == "pullback":
        long = ((close.shift(1) <= frame.ema20.shift(1)) & (close > frame.ema20)
                & up & rising & (frame.rsi > 50) & (frame.rsi <= 70)
                & (close > 1.5 * frame.atr))
        short = ((close.shift(1) >= frame.ema20.shift(1)) & (close < frame.ema20)
                 & down & falling & (frame.rsi >= 30) & (frame.rsi < 50)
                 & (close > 3.0 * frame.atr))
    elif kind == "cross":
        long = up & ~(up.shift(1, fill_value=False)) & rising & (frame.rsi > 50) & (frame.rsi <= 70)
        short = down & ~(down.shift(1, fill_value=False)) & falling & (frame.rsi >= 30) & (frame.rsi < 50)
    else:
        lookback = int(kind.replace("breakout", "").replace("_trend", ""))
        long = close > frame.high.shift(1).rolling(lookback).max()
        short = close < frame.low.shift(1).rolling(lookback).min()
        if kind.endswith("_trend"):
            long &= up & rising & (frame.rsi > 50)
            short &= down & falling & (frame.rsi < 50)
    ready = (np.arange(len(frame)) >= 59) & (frame.atr > 0)
    frame["direction"] = np.select([ready & long, ready & short], ["LONG", "SHORT"], default="НЕТ")
    return frame


def evaluate(df, kind, atr_mult, start=0):
    frame = signal_frame(df, kind)
    def signal(direction, close, atr):
        risk = float(atr)*atr_mult
        if direction == "LONG": sl,tp = float(close)-risk, float(close)+2*risk
        else: sl,tp = float(close)+risk, float(close)-2*risk
        if sl <= 0 or tp <= 0 or risk <= 0: return None
        return {"direction":direction,"entry":float(close),"stop_loss":sl,"take_profit":tp,"risk_reward":2.0}
    with patch.object(backtest, "_strategy_frame", return_value=frame), patch.object(backtest, "_signal_from_values", side_effect=signal):
        return backtest.run_backtest(df, start_index=start)


def main():
    raw = {s: pd.read_csv(Path("historical_data") / f"{s}_15m.csv", parse_dates=["timestamp"]) for s in SYMBOLS}
    start = max(d.timestamp.iloc[0] for d in raw.values())
    end = min(d.timestamp.iloc[-1] for d in raw.values())
    data = {s:d[(d.timestamp>=start)&(d.timestamp<=end)].reset_index(drop=True) for s,d in raw.items()}
    cut = int(len(data[SYMBOLS[0]]) * 0.70)
    trials=[]
    for kind,mult in CANDIDATES:
        results = {s:evaluate(d.iloc[:cut].copy(),kind,mult) for s,d in data.items()}
        returns = [results[s]["return_pct"] for s in SYMBOLS]
        counts = [results[s]["total"] for s in SYMBOLS]
        row={"kind":kind,"atr_multiplier":mult,"median_train_return_pct":float(np.median(returns)),
             "minimum_trades":min(counts), "min_train_return_pct":min(returns)}
        for s in SYMBOLS:
            row[s+"_return_pct"] = results[s]["return_pct"]
            row[s+"_trades"] = results[s]["total"]
        trials.append(row)
    valid = [x for x in trials if x["minimum_trades"] >= 8]
    if not valid: raise RuntimeError("No candidate met the minimum trade count")
    winner = max(valid, key=lambda x:(x["median_train_return_pct"],x["min_train_return_pct"]))
    with Path("candidate_trials.csv").open("w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=trials[0].keys()); writer.writeheader(); writer.writerows(trials)
    later={s:{key:result[key] for key in ("total","win_rate","return_pct","max_drawdown_pct")} for s,d in data.items()
           for result in [evaluate(d,winner["kind"],winner["atr_multiplier"],cut)]}
    report={"selection":winner,"oos_exploratory":later,"split":str(data[SYMBOLS[0]].timestamp.iloc[cut]),
            "candidate_count":len(trials), "selection_rule":"max median training return across four symbols; >=8 trades per symbol; tie-break max worst-symbol return",
            "warning":"OOS is exploratory: previous candidate OOS was inspected before this candidate comparison."}
    Path("candidate_selection.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__ == "__main__":
    main()
