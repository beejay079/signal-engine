"""
Signal website - built around signal_engine.py (generate_signal, unchanged).

Pages / endpoints
  /                    the site
  /api/markets         every market, grouped (Metals, Forex, Indices, Energy, Crypto, Commodities, Bonds)
  /api/signal          on-demand signal for one market (+ limit order if no entry now)
  /api/scan            one scanner cycle - call it every minute from cron-job.org
  /api/scanner         scanner status, latest results, alerts
  /api/scanner/settings  save watchlist / grades / timeframes
  /api/notify/test     send a test notification

Environment variables (Render -> Environment)
  TWELVE_DATA_API_KEY  live data (without it everything runs on fake demo data)
  APP_PASSWORD         optional - locks the site and the scan link
  DATA_PLAN            free (default) | grow | pro  - your Twelve Data plan
  NTFY_TOPIC           phone notifications through the free ntfy app
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   optional - Telegram notifications
"""
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

import markets as mk          # must load before the engine is used: registers all markets
import data
import notify
import scanner
import setups
import signal_engine as se

app = FastAPI(title="Signal Engine")
PAGE = Path(__file__).with_name("index.html")
TIMEFRAMES = ("1min", "5min", "15min", "1h")


def as_json(obj, status=200):
    return Response(json.dumps(obj, default=float), status_code=status, media_type="application/json")


def check_password(given):
    password = os.getenv("APP_PASSWORD")
    if password and given != password:
        raise HTTPException(status_code=401, detail="Wrong password")


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "live": data.live(), "plan": mk.plan()}


@app.get("/api/markets")
def markets():
    return dict(categories=mk.CATEGORIES, markets=mk.catalog(), plan=mk.plan(), gold=mk.GOLD)


@app.get("/api/signal")
def signal(symbol: str = "XAU/USD", tf: str = "1min", balance: float = 100.0,
           risk: float = 2.0, cent: bool = False,
           x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    if symbol not in mk.BY_SYMBOL:
        raise HTTPException(400, f"Unknown market {symbol}")
    if not mk.allowed(symbol):
        raise HTTPException(403, f"{symbol} needs the Twelve Data {mk.BY_SYMBOL[symbol]['plan'].title()} plan")
    if tf not in TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of {', '.join(TIMEFRAMES)}")
    if not (balance > 0 and 0.1 <= risk <= 10):
        raise HTTPException(400, "Balance must be above 0 and risk between 0.1% and 10%")
    try:
        frames = scanner.frames_for(symbol, tf, reserve=0, entry_max_age=20 if tf == "1min" else 60)
        result = scanner.analyze(symbol, tf, frames, balance, risk, cent)
        if result.get("setup"):
            result["setup"]["shown"] = True          # you saw it -> it gets fill / HOLD / WITHDRAW alerts
        result["demo"] = not data.live()
        result["name"] = mk.BY_SYMBOL[symbol]["name"]
    except data.Busy as e:
        return JSONResponse({"error": str(e)}, status_code=429)
    except Exception as e:
        msg = str(e)
        if "symbol" in msg.lower() and mk.BY_SYMBOL[symbol]["verify"]:
            msg += " - this market's code may differ on your data plan."
        return JSONResponse({"error": msg}, status_code=502)
    return as_json(result)


@app.post("/api/setup/clear")
def setup_clear(symbol: str, tf: str, x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    return dict(cleared=setups.clear(symbol, tf))


@app.post("/api/trade")
def trade_add(payload: dict = Body(...), x_app_password: str | None = Header(default=None)):
    """Tell the site about a trade you opened yourself, so it can alert HOLD / WITHDRAW."""
    check_password(x_app_password)
    try:
        sym = payload["symbol"]
        side = payload["side"].upper()
        entry, sl = float(payload["entry"]), float(payload["stop_loss"])
        tp1 = float(payload["take_profit_1"]) if payload.get("take_profit_1") else None
    except Exception:
        raise HTTPException(400, "Need symbol, side, entry and stop_loss")
    if sym not in mk.BY_SYMBOL or side not in ("BUY", "SELL"):
        raise HTTPException(400, "Unknown market or side")
    d = 1 if side == "BUY" else -1
    if (entry - sl) * d <= 0 or (tp1 is not None and (tp1 - entry) * d <= 0):
        raise HTTPException(400, "Stop loss must be below entry for BUY (above for SELL), and TP on the other side")
    if sym not in scanner.state["settings"]["watch"]:
        scanner.update_settings(dict(watch=scanner.state["settings"]["watch"] + [sym]))
    return as_json(setups.add_manual(sym, side, entry, sl, tp1, None, payload.get("lot")))


@app.delete("/api/trade")
def trade_remove(symbol: str, x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    return dict(removed=setups.remove_manual(symbol))


@app.get("/api/trades")
def trades(x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    return as_json(list(setups.manual.values()))


@app.get("/api/scan")
def scan(key: str | None = Query(default=None)):
    check_password(key)
    return as_json(scanner.run())


@app.get("/api/scanner")
def scanner_status(x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    return as_json(scanner.status())


@app.post("/api/scanner/settings")
def scanner_settings(payload: dict = Body(...), x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    scanner.update_settings(payload)
    return as_json(scanner.status())


@app.post("/api/notify/test")
def notify_test(x_app_password: str | None = Header(default=None)):
    check_password(x_app_password)
    errs = notify.send("Signal Engine test ✅", "Notifications are working. You'll get alerts like this "
                       "when the scanner finds a top-grade trade.", priority=4, tags=["bell"],
                       click=os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL"))
    return dict(ok=not errs, errors=errs, channels=notify.channels())
