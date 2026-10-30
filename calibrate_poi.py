"""
calibrate_poi.py - measure how often each kind of POI really gets a reaction.

    python3 calibrate_poi.py candles.csv XAU/USD 15min 1h
    (csv: time, open, high, low, close - 1-minute candles, as for backtest.py)

For every BOS/CHoCH in the history it builds the POIs exactly like the live site (structure.py,
judged at the same fixed cut-off), then looks forward:

    reaction = after price first returns into the zone, it moves 1 R in the trade direction
               (R = zone entry to the stop beyond the HL/LH) before a candle closes through the zone.

Results are grouped by number of confluences and saved to poi_stats.json next to this file.
Once that file is on the server, the site shows MEASURED rates instead of estimates
(only for groups with 30+ past zones).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

import signal_engine as se
import structure as stx

RULE = {"5min": "5min", "15min": "15min", "1h": "1h", "4h": "4h"}
WINDOW = 300


def resample(df, tf):
    return df.resample(RULE[tf], label="left", closed="left").agg(
        dict(open="first", high="max", low="min", close="last")).dropna()


def outcome(df, start, d, top, bot, risk, max_bars=400):
    """1 = reacted (+1R) / 0 = failed (closed through) / None = never came back."""
    entry = top if d == 1 else bot
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    touched = False
    for t in range(start, min(start + max_bars, len(df))):
        if not touched:
            if (d == 1 and l[t] <= top) or (d == -1 and h[t] >= bot):
                touched = True
            else:
                continue
        if (d == 1 and c[t] < bot) or (d == -1 and c[t] > top):
            return 0
        if (d == 1 and h[t] >= entry + risk) or (d == -1 and l[t] <= entry - risk):
            return 1
    return None


def run(base, symbol, tfs):
    stats = {}
    for tf in tfs:
        df = resample(base, tf)
        seen = set()
        full = se.Ctx(df, tf)
        evs = stx.events(full)
        print(f"{tf}: {len(df)} candles, {len(evs)} BOS/CHoCH")
        for E in evs:
            cut = E["bar"] + stx.CONFIRM
            if cut >= len(df) - 5 or cut < WINDOW:
                continue
            w = df.iloc[cut - WINDOW + 1:cut + 1]
            a = stx.analyse_tf(se.Ctx(w, tf), tf, symbol)
            if not a["id"] or a["id"] in seen or not a["key"]:
                continue
            seen.add(a["id"])
            d, origin = a["dir"], a["key"]["origin"]
            for p in a["pois"]:
                entry = p["top"] if d == 1 else p["bot"]
                risk = abs(entry - origin) or 1e-9
                res = outcome(df, cut + 1, d, p["top"], p["bot"], risk)
                if res is None:
                    continue
                for key in (str(min(p["n"], 5)), p["type"]):
                    row = stats.setdefault(key, [0, 0])
                    row[0] += res
                    row[1] += 1
    by_conf = {k: dict(rate=round(v[0] / v[1], 3), n=v[1]) for k, v in stats.items() if k.isdigit()}
    by_type = {k: dict(rate=round(v[0] / v[1], 3), n=v[1]) for k, v in stats.items() if not k.isdigit()}
    return dict(source=f"{symbol} {', '.join(tfs)} {base.index[0]:%b %Y}-{base.index[-1]:%b %Y}",
                by_confluence=by_conf, by_type=by_type)


if __name__ == "__main__":
    path, symbol, tfs = sys.argv[1], sys.argv[2], sys.argv[3:] or ["15min", "1h"]
    base = pd.read_csv(path, index_col=0, parse_dates=True)[["open", "high", "low", "close"]]
    base.index = pd.to_datetime(base.index, utc=True)
    res = run(base, symbol, tfs)
    Path(__file__).with_name("poi_stats.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
    print("Saved poi_stats.json - upload it with the other files and the site will use these rates.")
