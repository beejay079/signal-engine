"""
scanner.py - checks your watchlist on its own and alerts your phone on top-grade trades.

How it runs: cron-job.org (free) calls  /api/scan  once a minute. Each call checks the
markets whose candle just closed, Gold first, staying inside the Twelve Data credit limit.

Grading (0-100) of a BUY/SELL from signal_engine:
  agreement %  x 0.5            (max 50)
  entry triggers x 10           (max 30, 3 triggers)
  higher timeframe agrees       +15 (neutral +5, against 0)
  trending +5, ranging +3
  -10 if min lot risks more than your %, -10 if counter-trend
  A+ >= 85   A >= 75   B >= 65   C below
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import data
import limit_orders as lo
import markets as mk
import notify
import signal_engine as se

STATE_FILE = Path(os.getenv("DATA_DIR", Path(__file__).with_name("data"))) / "scanner.json"
GRADES = {"A+": 85, "A": 75, "B": 65}
TF_OPTIONS = ("1min", "5min", "15min")
DEDUP_MINUTES = 30

DEFAULTS = dict(
    enabled=True,
    watch=["XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY", "GBP/JPY", "AUD/USD",
           "BTC/USD", "ETH/USD"],
    gold_tf="5min", other_tf="15min",
    gold_grade="A", other_grade="A+",
    limit_alerts=False,
    balance=100.0, risk=2.0, cent=False,
    start_hour=7, end_hour=20,        # UTC. 07-20 UTC = 08:00-21:00 Lagos (London + New York)
)

_lock = threading.Lock()
state = dict(settings=dict(DEFAULTS), last_bucket={}, latest={}, alerts=[], sent={},
             last_scan=None, last_scan_note="", notify_errors=[])


# ---------------------------------------------------------------- persistence
def load():
    try:
        saved = json.loads(STATE_FILE.read_text())
        state["settings"].update({k: v for k, v in saved.get("settings", {}).items() if k in DEFAULTS})
        state["alerts"] = saved.get("alerts", [])[:30]
    except Exception:
        pass


def save():
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(dict(settings=state["settings"], alerts=state["alerts"][:30])))
    except Exception:
        pass


def update_settings(new: dict):
    s = dict(state["settings"])
    for k, v in new.items():
        if k not in DEFAULTS:
            continue
        if k == "watch":
            v = [x for x in v if x in mk.BY_SYMBOL]
            if mk.GOLD in v:                       # gold always first
                v = [mk.GOLD] + [x for x in v if x != mk.GOLD]
        if k in ("gold_tf", "other_tf") and v not in TF_OPTIONS:
            continue
        if k in ("gold_grade", "other_grade") and v not in GRADES:
            continue
        if k in ("balance", "risk"):
            v = float(v)
            if v <= 0 or (k == "risk" and v > 10):
                continue
        if k in ("start_hour", "end_hour"):
            v = max(0, min(24, int(v)))
        s[k] = v
    state["settings"] = s
    save()
    return s


# ---------------------------------------------------------------- grading
def grade(r):
    if r.get("signal") not in ("BUY", "SELL"):
        return None, 0
    d = 1 if r["signal"] == "BUY" else -1
    htf = {"BULLISH": 1, "BEARISH": -1}.get(r.get("htf_bias"), 0)
    score = r.get("confidence", 0) * 0.5 + min(len(r.get("triggers", [])), 3) * 10
    score += 15 if htf == d else 5 if htf == 0 else 0
    score += {"TRENDING": 5, "RANGING": 3}.get(r.get("regime"), 0)
    warns = " ".join(r.get("warnings", []))
    if "Min lot" in warns:
        score -= 10
    if "Counter-trend" in warns:
        score -= 10
    planned = r.get("_planned_risk")
    if planned and r.get("risk_amount", 0) > 1.5 * planned:
        score = min(score, 70)          # min lot risks far more than you want -> never "perfect"
    score = max(0, min(100, round(score)))
    letter = "A+" if score >= 85 else "A" if score >= 75 else "B" if score >= 65 else "C"
    return letter, score


def add_grade(r, balance=None, risk=None):
    if balance and risk:
        r["_planned_risk"] = balance * risk / 100
    r["grade"], r["score"] = grade(r)
    r.pop("_planned_risk", None)
    return r


# ---------------------------------------------------------------- schedule + budget
def tf_for(symbol, s=None):
    s = s or state["settings"]
    return s["gold_tf"] if symbol == mk.GOLD else s["other_tf"]


def htf_refresh_minutes(tf):
    return min(se.TF_MINUTES.get(se.HIGHER_TF[tf], 60), 60)


def market_open(symbol, now=None, s=None):
    s, now = s or state["settings"], now or datetime.now(timezone.utc)
    if not (s["start_hour"] <= now.hour < s["end_hour"]):
        return False
    crypto = mk.BY_SYMBOL.get(symbol, {}).get("category") == "Crypto"
    return crypto or now.weekday() < 5


def estimate(s=None):
    """Credits per day the watchlist will use (weekday, inside the hours)."""
    s = s or state["settings"]
    window = max(0, s["end_hour"] - s["start_hour"]) * 60
    rows, total = [], 0
    for sym in s["watch"]:
        if not mk.allowed(sym):
            continue
        tf = tf_for(sym, s)
        cost = window / se.TF_MINUTES[tf] + window / htf_refresh_minutes(tf)
        rows.append(dict(symbol=sym, tf=tf, credits=round(cost)))
        total += cost
    per_min, per_day = data.credits.limits()
    budget = None if per_day is None else per_day - data.MANUAL_RESERVE_PER_DAY
    return dict(per_symbol=rows, total=round(total), budget=budget,
                fits=budget is None or total <= budget)


def due(now_ts, s):
    out = []
    now = datetime.fromtimestamp(now_ts, timezone.utc)
    for sym in s["watch"]:
        if not mk.allowed(sym) or not market_open(sym, now, s):
            continue
        secs = se.TF_MINUTES[tf_for(sym, s)] * 60
        bucket = int(now_ts // secs)
        if state["last_bucket"].get(sym) != bucket:
            out.append((sym, bucket, now_ts - bucket * secs))
    out.sort(key=lambda x: (x[0] != mk.GOLD, -x[2]))   # gold first, then most overdue
    return out


# ---------------------------------------------------------------- evaluation + alerts
def evaluate(symbol, s):
    tf = tf_for(symbol, s)
    htf = se.HIGHER_TF[tf]
    reserve = data.MANUAL_RESERVE_PER_MIN
    df = data.candles(symbol, tf, 500, max_age=20, reserve=reserve)
    htf_df = data.candles(symbol, htf, 300, max_age=htf_refresh_minutes(tf) * 60, reserve=reserve)
    r = se.generate_signal(symbol, tf, s["balance"], s["risk"], cent=s["cent"], df=df, htf_df=htf_df)
    lo.add_limit_orders(r, symbol, tf, df, s["balance"], s["risk"], s["cent"])
    return add_grade(r, s["balance"], s["risk"])


def _summary(r):
    keep = ("symbol", "interval", "signal", "grade", "score", "price", "regime", "htf_bias",
            "confidence", "entry", "stop_loss", "take_profit_1", "take_profit_2", "lot_size",
            "risk_amount", "triggers")
    out = {k: r[k] for k in keep if k in r}
    out["time"] = int(time.time())
    if r.get("limit_orders"):
        p = r["limit_orders"][0]
        out["limit"] = dict(side=p["side"], entry=p["entry"], confluence=p["confluence"], zone=p["zone"])
    return out


def _should_alert(r, s):
    sym = r["symbol"]
    need = s["gold_grade"] if sym == mk.GOLD else s["other_grade"]
    if r.get("grade") and r["score"] >= GRADES[need]:
        return "market"
    if s["limit_alerts"] and r.get("limit_orders"):
        p = r["limit_orders"][0]
        planned = s["balance"] * s["risk"] / 100
        if p["confluence"] >= 2 and "strategies" in p.get("bias", "") and p["risk_amount"] <= 1.5 * planned:
            return "limit"
    return None


def _message(r, kind):
    name = mk.BY_SYMBOL.get(r["symbol"], {}).get("name", r["symbol"])
    tf = r["interval"].replace("min", "m")
    if kind == "market":
        title = f"{r['signal']} {name} ({r['symbol']}) · {r['grade']} · {tf}"
        body = (f"Enter now at {r['entry']}\n"
                f"SL {r['stop_loss']}  |  TP1 {r['take_profit_1']} ({r['rr_tp1']})  |  TP2 {r['take_profit_2']}\n"
                f"Lot {r['lot_size']} (risk ${r['risk_amount']:.2f})\n"
                f"Why: {', '.join(r['triggers'])}\n"
                f"{r['confidence']}% agree · {r['htf']} trend {r['htf_bias']} · {r['regime']}")
        key_entry, side = r["entry"], r["signal"]
        tags = ["green_circle" if side == "BUY" else "red_circle"]
    else:
        p = r["limit_orders"][0]
        title = f"{p['side']} {name} ({r['symbol']}) · {tf}"
        body = (f"Place {p['side'].lower()} at {p['entry']} (price now {r['price']})\n"
                f"SL {p['stop_loss']}  |  TP1 {p['take_profit_1']} (1:2)  |  TP2 {p['take_profit_2']}\n"
                f"Lot {p['lot_size']} (risk ${p['risk_amount']:.2f})\n"
                f"Zone: {p['zone']}\n"
                f"Cancel if not filled within {p['expires_in_minutes']} min")
        key_entry, side = p["entry"], p["side"]
        tags = ["hourglass"]
    if any("Min lot" in w for w in r.get("warnings", [])) or (kind == "limit" and "Min lot" in p.get("lot_note", "")):
        body += "\n⚠ Min lot is above your risk %"
    return title, body, side, key_entry, tags


def maybe_alert(r, s):
    kind = _should_alert(r, s)
    if not kind:
        return None
    title, body, side, entry, tags = _message(r, kind)
    key = f"{r['symbol']}|{side}"
    prev = state["sent"].get(key)
    dist = r.get("sl_distance") or (r.get("limit_orders") or [{}])[0].get("sl_distance") or 0
    if prev and time.time() - prev[0] < DEDUP_MINUTES * 60 and abs(entry - prev[1]) < dist:
        return None                                              # same setup already sent
    state["sent"][key] = (time.time(), entry)
    alert = dict(time=int(time.time()), symbol=r["symbol"], kind=kind, title=title, body=body,
                 grade=r.get("grade"), demo=not data.live())
    if data.live():
        click = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
        prio = 5 if (r.get("grade") == "A+" or r["symbol"] == mk.GOLD) else 4
        errs = notify.send(title, body, priority=prio, tags=tags, click=click)
        alert["sent"] = not errs
        if errs:
            state["notify_errors"] = (errs + state["notify_errors"])[:5]
    else:
        alert["sent"] = False                                     # demo data never notifies
    state["alerts"].insert(0, alert)
    state["alerts"] = state["alerts"][:30]
    save()
    return alert


# ---------------------------------------------------------------- one scan cycle
def run(now_ts=None):
    if not _lock.acquire(blocking=False):
        return dict(ok=True, note="A scan is already running.")
    try:
        s = state["settings"]
        now_ts = now_ts or time.time()
        checked, alerts, notes = [], [], []
        if not s["enabled"]:
            notes.append("Scanner is switched off.")
        for sym, bucket, _ in (due(now_ts, s) if s["enabled"] else []):
            tf = tf_for(sym, s)
            need = 1 + (1 if (data.cache_age(sym, se.HIGHER_TF[tf]) or 1e9) > htf_refresh_minutes(tf) * 60 else 0)
            if data.live():
                if data.credits.minute_left() - data.MANUAL_RESERVE_PER_MIN < need:
                    notes.append("Minute limit reached - the rest wait for the next minute.")
                    break
                left = data.credits.day_left()
                if left is not None:
                    if left - need < 10:
                        notes.append("Daily credits almost used - scanning paused until tomorrow.")
                        break
                    if sym != mk.GOLD and left - need < data.MANUAL_RESERVE_PER_DAY:
                        notes.append("Daily credits low - only Gold is being scanned.")
                        continue
            try:
                r = evaluate(sym, s)
            except data.Busy as e:
                notes.append(str(e))
                break
            except Exception as e:
                state["latest"][sym] = dict(symbol=sym, error=str(e)[:160], time=int(now_ts))
                state["last_bucket"][sym] = bucket
                continue
            state["last_bucket"][sym] = bucket
            state["latest"][sym] = _summary(r)
            checked.append(sym)
            a = maybe_alert(r, s)
            if a:
                alerts.append(a["title"])
        state["last_scan"] = int(now_ts)
        state["last_scan_note"] = " ".join(dict.fromkeys(notes))
        return dict(ok=True, checked=checked, alerts=alerts, note=state["last_scan_note"])
    finally:
        _lock.release()


def status():
    return dict(settings=state["settings"], estimate=estimate(), credits=data.credits.snapshot(),
                live=data.live(), channels=notify.channels(), last_scan=state["last_scan"],
                note=state["last_scan_note"], latest=state["latest"], alerts=state["alerts"][:20],
                notify_errors=state["notify_errors"], plan=mk.plan())


load()
