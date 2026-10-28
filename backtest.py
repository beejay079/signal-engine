"""
backtest.py - replay the exact live logic (topdown.py) candle by candle.

    python3 backtest.py data.csv XAU/USD 5min            # csv: time,open,high,low,close (1-minute)

No look-ahead: at each closed entry candle only candles that had closed by
then are visible; higher-timeframe candles are used only once closed.
Market trades fill at that candle's close; limits fill when touched later.
Spread is charged on every trade. Results are in R (1R = the stop distance).
"""
from __future__ import annotations

import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd

import signal_engine as se
import topdown as td

RULE = {"1min": "1min", "5min": "5min", "15min": "15min", "1h": "1h", "4h": "4h", "1day": "1D", "1week": "1W"}


def resample(df, tf):
    if tf == "1min":
        return df
    return df.resample(RULE[tf], label="left", closed="left").agg(
        dict(open="first", high="max", low="min", close="last")).dropna()


def run(base, symbol, entry_tf, P=None, start=None, end=None, books=("market", "limit"), progress=False):
    P = {**td.DEFAULTS, **(P or {})}
    top_tf, mid_tf, setup_tf = td.STACKS[entry_tf]
    tfs = dict(entry=entry_tf, setup=setup_tf, mid=mid_tf, top=top_tf)
    full = {k: resample(base, tf) for k, tf in tfs.items()}
    dur = {k: pd.Timedelta(minutes=se.TF_MINUTES[tf]) for k, tf in tfs.items()}
    ends = {k: full[k].index + dur[k] for k in full}          # candle close times
    E = full["entry"]
    atr_e = se.atr(E).values
    emin = dur["entry"]
    i0 = max(E.index.searchsorted(pd.Timestamp(start, tz="UTC")) if start else 0,
             int(E.index.searchsorted(ends["top"][min(len(ends["top"]) // 2, 150)])), 520)
    i1 = E.index.searchsorted(pd.Timestamp(end, tz="UTC")) if end else len(E) - 1
    view, vkey, active, trades = None, None, {b: None for b in books}, []
    ratio = max(1, se.TF_MINUTES[setup_tf] // se.TF_MINUTES[entry_tf])
    win = int(min(60, max(8, 3 * ratio)))
    t0 = time.time()
    n_eval = 0
    for pos in range(i0, i1):
        T = E.index[pos]
        now = T + emin
        key = tuple(int(np.searchsorted(ends[k].values, now.to_datetime64(), side="right")) for k in ("setup", "mid", "top"))
        new_setup_bar = vkey is None or key[0] != vkey[0]
        if key != vkey:
            fr = {}
            for k, n in zip(("setup", "mid", "top"), key):
                c = full[k].iloc[max(0, n - 300):n]
                fr[k] = pd.concat([c, c.iloc[-1:]])
            view = td.build_view(symbol, entry_tf, fr)
            vkey = key
        price = float(E["close"].iloc[pos])
        hist = E.iloc[max(0, pos - 600):pos + 1]
        # manage open trades with this candle
        for b in books:
            tr = active[b]
            if tr is None:
                continue
            td.manage(tr, hist, view, P, now.to_pydatetime(), price, atr_e[pos])
            if tr["status"] in ("closed", "cancelled"):
                trades.append(tr)
                active[b] = None
        # new market trade
        if "market" in books and active["market"] is None and (view.bias or view.s_regime == "RANGING"):
            lo_w, hi_w = float(E["low"].iloc[pos - win:pos + 1].min()), float(E["high"].iloc[pos - win:pos + 1].max())
            a = view.s.atr_now
            near = False
            for d in ([view.bias] if view.bias else [1, -1]):
                ext = lo_w if d == 1 else hi_w
                if any(z["bot"] - 0.3 * a <= ext <= z["top"] + 0.15 * a for z in view.zones[d]) or \
                        any(x and abs(ext - x) <= 0.3 * a for x in view.key_lv[d]) or view.s_regime == "RANGING" \
                        or view.bias_kind == "aligned":
                    near = True
            if near:
                n_eval += 1
                e = se.Ctx(E.iloc[pos - 499:pos + 1], entry_tf)
                ms = td.market_setup(view, e, price, lo_w, hi_w, now.to_pydatetime(), P)
                b = ms["best"]
                if b and b["grade"] in P["trade_grades"] and not ms["hard"]:
                    tr = td.new_trade(view, "market", b["side"], b["levels"], T, b["name"])
                    tr.update(grade=b["grade"], score=b["score"], opened=iso_t(T))
                    tr["last_ts"] = iso_t(T)
                    active["market"] = tr
        # quick trade (enter now)
        if "quick" in books and active["quick"] is None and (view.s_regime != "VOLATILE"):
            e = se.Ctx(E.iloc[pos - 499:pos + 1], entry_tf)
            if se.detect_regime(e)[0] != "VOLATILE":
                q, _ = td.quick_setup(view, e, price, td.strategy_votes(e, se.detect_regime(e)[0]), P)
                if q:
                    tr = td.new_trade(view, "market", q["side"], q, T, "Quick trade")
                    tr.update(kind="quick", grade="-", score=0); tr["last_ts"] = iso_t(T)
                    active["quick"] = tr
        # new limit (only when a setup candle closes, like a trader re-reading the chart)
        if "limit" in books and active["limit"] is None and new_setup_bar and P["limit_on"]:
            lim, why = td.limit_setup(view, price, atr_e[pos], now.to_pydatetime(), P)
            if lim:
                tr = td.new_trade(view, "limit", lim["side"], lim, T, "Pullback limit",
                                  extra=dict(expires=td.iso(lim["expires"]), grade="-", score=0, full_close=P["lim_full_close"]))
                tr["last_ts"] = iso_t(T)
                active["limit"] = tr
        if progress and pos % 5000 == 0:
            print(f"  {pos - i0}/{i1 - i0} bars, {len(trades)} trades, {time.time() - t0:.0f}s", flush=True)
    return trades, dict(bars=i1 - i0, evals=n_eval, secs=round(time.time() - t0, 1),
                        start=str(E.index[i0]), end=str(E.index[i1 - 1]))


def iso_t(ts):
    return pd.Timestamp(ts).isoformat()


def stats(trades, kind=None):
    t = [x for x in trades if x["status"] == "closed" and (kind is None or x["kind"] == kind)]
    cancelled = len([x for x in trades if x["status"] == "cancelled" and (kind is None or x["kind"] == kind)])
    if not t:
        return dict(trades=0, cancelled=cancelled)
    r = np.array([x["realized_r"] for x in t])
    wins, losses = r[r > 0.05], r[r < -0.05]
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    streak = cur = 0
    for v in r:
        cur = cur + 1 if v < -0.05 else 0
        streak = max(streak, cur)
    full_sl = int(sum(1 for x in t if x["events"] and x["events"][-1]["text"].startswith("Stop loss hit")))
    return dict(trades=len(t), cancelled=cancelled, win_rate=round(len(wins) / len(t), 3),
                breakeven=int(len(t) - len(wins) - len(losses)), full_sl_rate=round(full_sl / len(t), 3),
                avg_r=round(float(r.mean()), 3), total_r=round(float(r.sum()), 1),
                profit_factor=round(float(wins.sum() / -losses.sum()), 2) if len(losses) else None,
                max_dd_r=round(dd, 1), max_losing_streak=streak)


if __name__ == "__main__":
    path, sym, tf = sys.argv[1], sys.argv[2], sys.argv[3]
    df = pd.read_csv(path, index_col=0, parse_dates=True)[["open", "high", "low", "close"]]
    df.index = pd.to_datetime(df.index, utc=True)
    tr, info = run(df, sym, tf, progress=True)
    print(info)
    for k in ("market", "limit", "quick"):
        print(k, stats(tr, k))
