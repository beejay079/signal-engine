"""
Signal website - built around signal_engine.py (generate_signal).

Pick a pair and timeframe on the page, tap Get signal. The engine runs all
29 strategies on live Twelve Data candles, filtered by the higher timeframe
(1min -> 15min, 5min -> 1h, 15min -> 4h, 1h -> 4h).

Environment variables (set them on Render):
  TWELVE_DATA_API_KEY   your Twelve Data key (without it the site runs on fake demo data)
  APP_PASSWORD          optional - if set, the page asks for it once
"""
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

import limit_orders as lo
import signal_engine as se

app = FastAPI(title="Signal Engine")
PAGE = Path(__file__).with_name("index.html")
TIMEFRAMES = ("1min", "5min", "15min", "1h")
CACHE_SECONDS = 15          # protects the free Twelve Data limit (8 requests/min)
_cache: dict = {}
DEMO_START = {"XAU/USD": 2650, "EUR/USD": 1.08, "GBP/USD": 1.27, "USD/JPY": 150, "BTC/USD": 65000}


def candles(symbol: str, tf: str, key: str, size: int):
    k, now = (symbol, tf), time.time()
    if k in _cache and now - _cache[k][0] < CACHE_SECONDS:
        return _cache[k][1]
    df = se.fetch_candles(symbol, tf, key, size)
    _cache[k] = (now, df)
    return df


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "live": bool(os.getenv("TWELVE_DATA_API_KEY"))}


@app.get("/api/signal")
def signal(symbol: str = "XAU/USD", tf: str = "1min", balance: float = 100.0,
           risk: float = 2.0, cent: bool = False,
           x_app_password: str | None = Header(default=None)):
    password = os.getenv("APP_PASSWORD")
    if password and x_app_password != password:
        raise HTTPException(status_code=401, detail="Wrong password")
    if symbol not in se.SYMBOLS:
        raise HTTPException(400, f"Unknown pair {symbol}")
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of {', '.join(TIMEFRAMES)}")
    if not (balance > 0 and 0.1 <= risk <= 10):
        raise HTTPException(400, "Balance must be above 0 and risk between 0.1% and 10%")

    htf = se.HIGHER_TF[tf]
    key = os.getenv("TWELVE_DATA_API_KEY")
    try:
        if key:
            df, htf_df = candles(symbol, tf, key, 500), candles(symbol, htf, key, 300)
        else:  # no key yet -> fake data so the page can still be tried
            seed = int(time.time() // 60) % 997
            start = DEMO_START.get(symbol, 100)
            df = se.demo_candles(tf, 500, seed=seed, start_price=start)
            htf_df = se.demo_candles(htf, 300, seed=seed + 1, start_price=start)
        result = se.generate_signal(symbol, tf, balance, risk, cent=cent, df=df, htf_df=htf_df)
        lo.add_limit_orders(result, symbol, tf, df, balance, risk, cent)   # pending order if no entry now
        result["demo"] = not key
    except Exception as e:
        msg = str(e)
        if "credits" in msg.lower() or "limit" in msg.lower():
            msg = "Twelve Data limit reached - wait a minute and try again."
        return JSONResponse({"error": msg}, status_code=502)
    return Response(json.dumps(result, default=float), media_type="application/json")
