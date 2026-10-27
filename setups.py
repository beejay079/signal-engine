"""
setups.py - a setup, once given, stays FIXED until it finishes.

Per market + timeframe there is at most one active setup. Every new candle is checked:
  limit order:  WAITING -> FILLED (price touched entry) -> TP1 HIT / STOPPED
                WAITING -> EXPIRED (not filled in time) / MISSED (ran to TP1 without filling)
                          / INVALID (closed beyond the stop before filling)
  market entry: IN TRADE -> TP1 HIT / STOPPED
  range pair:   when one side fills, the other is CANCELLED.
While a setup is active, the site and the scanner show the same entry, SL and TP - they do
not change with every refresh. A new setup is only offered after the old one finishes
(or you clear it).
"""
import threading
import time

import pandas as pd

_lock = threading.Lock()
active: dict = {}      # key -> setup
history: list = []     # finished setups, newest first
ACTIVE_STATES = {"WAITING", "FILLED", "IN TRADE"}


def key(symbol, tf):
    return f"{symbol}|{tf}"


def _order(kind, side, src, expires_min, now):
    return dict(kind=kind, side=side, entry=src["entry"], stop_loss=src["stop_loss"],
                take_profit_1=src["take_profit_1"], take_profit_2=src["take_profit_2"],
                rr_tp2=src.get("rr_tp2"), lot_size=src["lot_size"], risk_amount=src["risk_amount"],
                zone=src.get("entry_zone") or src.get("zone", ""), sl_reason=src.get("sl_reason", ""),
                lot_note=src.get("lot_note", ""),
                status="IN TRADE" if kind == "market" else "WAITING",
                expires_at=None if kind == "market" else now + expires_min * 60,
                filled_at=now if kind == "market" else None, closed_at=None)


def _advance(setup, df):
    """Replay candles that formed after the setup was created."""
    since = pd.Timestamp(setup["bar_time"], unit="s", tz="UTC")
    rows = df[df.index > since]
    now = int(df.index[-1].timestamp())          # market time of the newest candle
    for o in setup["orders"]:
        if o["status"] not in ACTIVE_STATES:
            continue
        d = 1 if o["side"].startswith("BUY") else -1
        for ts, row in rows.iterrows():
            hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
            adverse = lo if d == 1 else hi
            favour = hi if d == 1 else lo
            t = int(ts.timestamp())
            if o["status"] == "WAITING":
                if o["expires_at"] and t > o["expires_at"]:
                    o["status"], o["closed_at"] = "EXPIRED", t
                    break
                if (adverse - o["entry"]) * d <= 0:                  # touched the entry
                    o["status"], o["filled_at"] = "FILLED", t
                    if (adverse - o["stop_loss"]) * d <= 0:          # same candle went through SL
                        o["status"], o["closed_at"] = "STOPPED", t
                        break
                    continue
                if (favour - o["take_profit_1"]) * d >= 0:
                    o["status"], o["closed_at"] = "MISSED", t       # ran to target without us
                    break
                if (cl - o["stop_loss"]) * d < 0:
                    o["status"], o["closed_at"] = "INVALID", t
                    break
            if o["status"] in ("FILLED", "IN TRADE"):
                if (adverse - o["stop_loss"]) * d <= 0:
                    o["status"], o["closed_at"] = "STOPPED", t
                    break
                if (favour - o["take_profit_1"]) * d >= 0:
                    o["status"], o["closed_at"] = "TP1 HIT", t
                    break
        if o["status"] == "WAITING" and o["expires_at"] and now > o["expires_at"]:
            o["status"], o["closed_at"] = "EXPIRED", now
    # range pair: once one side fills, cancel the other
    filled = [o for o in setup["orders"] if o["filled_at"]]
    if filled:
        for o in setup["orders"]:
            if not o["filled_at"] and o["status"] != "CANCELLED":
                o["status"], o["closed_at"] = "CANCELLED", now
    setup["live"] = any(o["status"] in ACTIVE_STATES for o in setup["orders"])


def sync(symbol, tf, result, df):
    """Return the setup for this market (existing if still active, else a new one from result)."""
    k, now = key(symbol, tf), time.time()
    with _lock:
        s = active.get(k)
        if s:
            _advance(s, df)
            if s["live"]:
                s["new"] = False
                return s
            history.insert(0, s)
            del history[20:]
            active.pop(k, None)
        orders, bar = [], int(df.index[-1].timestamp())
        if result.get("signal") in ("BUY", "SELL"):
            orders.append(_order("market", result["signal"], result, 0, bar))
        else:
            for p in result.get("limit_orders", []):
                orders.append(_order("limit", p["side"], p, p["expires_in_minutes"], bar))
        if not orders:
            return None
        # don't re-offer the same zone that just expired / was missed / broke
        for h in history[:5]:
            if key(h["symbol"], h["tf"]) != k or now - h["created"] > 3 * 3600:
                continue
            ho, no = h["orders"][0], orders[0]
            gap = abs(no["entry"] - no["stop_loss"]) * 0.3
            if ho["side"] == no["side"] and abs(ho["entry"] - no["entry"]) <= gap and \
                    ho["status"] in ("EXPIRED", "MISSED", "INVALID", "STOPPED", "CLEARED"):
                return None
        s = dict(id=f"{k}|{int(now)}", symbol=symbol, tf=tf, created=int(now),
                 bar_time=bar, orders=orders, live=True, new=True,
                 grade=result.get("grade"))
        active[k] = s
        return s


def last_finished(symbol, tf):
    k = key(symbol, tf)
    for s in history:
        if key(s["symbol"], s["tf"]) == k:
            return s
    return None


def clear(symbol, tf):
    with _lock:
        s = active.pop(key(symbol, tf), None)
        if s:
            for o in s["orders"]:
                if o["status"] in ACTIVE_STATES:
                    o["status"], o["closed_at"] = "CLEARED", int(time.time())
            s["live"] = False
            history.insert(0, s)
        return bool(s)


# ---------------------------------------------------------------- your own trades
manual: dict = {}     # symbol -> setup with one "manual" order you opened yourself


def add_manual(symbol, side, entry, stop_loss, take_profit_1, take_profit_2=None, lot=None):
    """Track a trade you opened in MT5 so the scanner can tell you HOLD or WITHDRAW."""
    d = 1 if side.upper().startswith("BUY") else -1
    dist = abs(entry - stop_loss)
    tp1 = take_profit_1 or entry + d * 2 * dist
    o = dict(kind="manual", side="BUY" if d == 1 else "SELL", entry=float(entry), stop_loss=float(stop_loss),
             take_profit_1=float(tp1), take_profit_2=float(take_profit_2 or entry + d * 3 * dist),
             rr_tp2=None, lot_size=lot, risk_amount=None, zone="Your own trade", sl_reason="",
             lot_note="", status="IN TRADE", expires_at=None, filled_at=int(time.time()), closed_at=None)
    now = int(time.time())
    with _lock:
        manual[symbol] = dict(id=f"{symbol}|manual|{now}", symbol=symbol, tf="manual", created=now,
                              bar_time=now, orders=[o], live=True, new=True, grade=None)
        return manual[symbol]


def update_manual(symbol, df):
    with _lock:
        s = manual.get(symbol)
        if s and s["live"]:
            _advance(s, df)
        return s


def remove_manual(symbol):
    with _lock:
        return manual.pop(symbol, None) is not None
