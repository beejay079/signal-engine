"""
calibrate_poi.py - check the structure rules on a long CSV history (optional, for your own testing).

    python3 calibrate_poi.py candles.csv XAU/USD 15min 1h 4h
    (csv: time, open, high, low, close - 1-minute candles, as for backtest.py)

Uses exactly the same code as the live site (calib.py -> structure.py):
  - how often each POI type reacted (+1R before a close through the zone)
  - the full order rule (limit at OB, SL beyond HL/LH, TP at HH/LL, R:R >= 1.5 after spread)
The live site already does this by itself on the last 5,000 candles of every timeframe.
"""
from __future__ import annotations

import json
import sys

import pandas as pd

import calib

RULE = {"1min": "1min", "5min": "5min", "15min": "15min", "1h": "1h", "4h": "4h", "1day": "1D"}
LABEL = {"1min": "1M", "5min": "5M", "15min": "15M", "1h": "1H", "4h": "4H", "1day": "D1"}

if __name__ == "__main__":
    path, symbol, tfs = sys.argv[1], sys.argv[2], sys.argv[3:] or ["15min", "1h"]
    base = pd.read_csv(path, index_col=0, parse_dates=True)[["open", "high", "low", "close"]]
    base.index = pd.to_datetime(base.index, utc=True)
    out = {}
    for tf in tfs:
        df = base.resample(RULE[tf], label="left", closed="left").agg(
            dict(open="first", high="max", low="min", close="last")).dropna()
        r = calib.compute(df, symbol, LABEL[tf])
        out[tf] = dict(span=r["span"], structures=r["structures"], rule=r["rule"],
                       poi={k: dict(wins=w, n=n, rate=round(100 * w / n, 1)) for k, (w, n) in r["poi"].items() if n})
        print(tf, json.dumps(out[tf]["rule"]))
    print(json.dumps(out, indent=2))
