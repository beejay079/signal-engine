"""
Signal website - top-down analysis (topdown.py) on top of signal_engine.py.

Pick a pair and an ENTRY timeframe, tap Analyse. The server pulls four charts
from Twelve Data and reads them top-down:

    entry 1m  : 4H + 1H bias -> 15M setup -> 1M trigger
    entry 5m  : 4H + 1H bias -> 15M setup -> 5M trigger
    entry 15m : D1 + 4H bias -> 1H setup  -> 15M trigger
    entry 1h  : W1 + D1 bias -> 4H setup  -> 1H trigger

Higher timeframes are cached longer (their candles change slowly), so a check
in Watch mode usually costs only 1-2 Twelve Data credits.

Environment variables (set them on Render):
  TWELVE_DATA_API_KEY   your Twelve Data key (without it the site runs on fake demo data)
  APP_PASSWORD          optional - if set, the page asks for it once

Trades you take are stored on your phone and sent with every check, so the
server can track them (fills, stop moves, early exits) even after it sleeps.
"""
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import signal_engine as se
import topdown as td

app = FastAPI(title="Signal Engine")
PAGE = Path(__file__).with_name("index.html")
# seconds a timeframe's candles are reused before fetching again
CACHE_TTL = {"1min": 20, "5min": 60, "15min": 120, "1h": 300, "4h": 900, "1day": 1800, "1week": 3600}
_cache: dict = {}
DEMO_START = {"XAU/USD": 2650, "EUR/USD": 1.08, "GBP/USD": 1.27, "USD/JPY": 150, "BTC/USD": 65000}


def candles(symbol: str, tf: str, key: str, size: int):
    k, now = (symbol, tf), time.time()
    if k in _cache and now - _cache[k][0] < CACHE_TTL.get(tf, 60):
        return _cache[k][1]
    df = se.fetch_candles(symbol, tf, key, size)
    _cache[k] = (now, df)
    return df


def demo_frames(symbol, entry_tf):
    """One fake series resampled to every timeframe, so the charts agree."""
    top, mid, setup = td.STACKS[entry_tf]
    step = se.TF_MINUTES[entry_tf]                         # base candle size
    n = int(min(max(se.TF_MINUTES.get(top, 10080) * 310 // step, 30000), 200_000))
    rng = np.random.default_rng(int(time.time() // 60) % 997)
    start = DEMO_START.get(symbol, 100)
    vol = 0.00025
    drift, i = np.zeros(n), 0
    while i < n:
        ln = int(rng.integers(300, 3000))
        drift[i:i + ln] = rng.choice([-1, 0, 1]) * vol * rng.uniform(0.05, 0.25)
        i += ln
    close = start * np.exp(np.cumsum(rng.standard_t(4, n) * vol * 0.7 + drift))
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor(f"{step}min"), periods=n, freq=f"{step}min")
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, vol * 0.5, n)) * close
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.5, n)) * close
    base = pd.DataFrame(dict(open=open_, high=high, low=low, close=close), index=idx)
    rule = {"1min": "1min", "5min": "5min", "15min": "15min", "1h": "1h", "4h": "4h", "1day": "1D", "1week": "1W"}

    def rs(tf, size):
        df = base if tf == entry_tf else base.resample(rule[tf], label="left", closed="left").agg(
            dict(open="first", high="max", low="min", close="last")).dropna()
        return df.iloc[-size:]

    return dict(entry=rs(entry_tf, td.SIZES["entry"]), setup=rs(setup, td.SIZES["setup"]),
                mid=rs(mid, td.SIZES["mid"]), top=rs(top, td.SIZES["top"]))


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "live": bool(os.getenv("TWELVE_DATA_API_KEY"))}


class Req(BaseModel):
    symbol: str = "XAU/USD"
    tf: str = "5min"
    balance: float = 100.0
    risk: float = 1.0
    cent: bool = False
    trades: list = []          # trades the phone is tracking (sent back every check)


@app.post("/api/signal")
def signal(req: Req, x_app_password: str | None = Header(default=None)):
    symbol, tf, balance, risk, cent = req.symbol, req.tf, req.balance, req.risk, req.cent
    password = os.getenv("APP_PASSWORD")
    if password and x_app_password != password:
        raise HTTPException(status_code=401, detail="Wrong password")
    if symbol not in se.SYMBOLS:
        raise HTTPException(400, f"Unknown pair {symbol}")
    if tf not in td.STACKS:
        raise HTTPException(400, f"Timeframe must be one of {', '.join(td.STACKS)}")
    if not (balance > 0 and 0.1 <= risk <= 10):
        raise HTTPException(400, "Balance must be above 0 and risk between 0.1% and 10%")

    key = os.getenv("TWELVE_DATA_API_KEY")
    top, mid, setup = td.STACKS[tf]
    try:
        if key:
            frames = dict(entry=candles(symbol, tf, key, td.SIZES["entry"]),
                          setup=candles(symbol, setup, key, td.SIZES["setup"]),
                          mid=candles(symbol, mid, key, td.SIZES["mid"]),
                          top=candles(symbol, top, key, td.SIZES["top"]))
        else:
            frames = demo_frames(symbol, tf)
        for name, df in frames.items():
            if len(df) < 120:
                raise RuntimeError(f"Not enough {name} candles from the data feed ({len(df)}).")
        result = td.analyse(symbol, tf, frames, balance, risk, cent, trades=req.trades)
        result["demo"] = not key
    except Exception as e:
        msg = str(e)
        if "credits" in msg.lower() or "limit" in msg.lower():
            msg = "Twelve Data limit reached (8 per minute) - wait a minute and try again."
        return JSONResponse({"error": msg}, status_code=502)
    return Response(json.dumps(result, default=float), media_type="application/json")
