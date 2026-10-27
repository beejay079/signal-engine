"""
scanner.py - checks your watchlist on its own and pushes alerts to your phone.

How it runs: cron-job.org (free) calls  /api/scan  once a minute. Each call checks the
markets whose entry candle just closed, Gold first, inside the Twelve Data credit limit.

Every check is a full TOP-DOWN analysis (topdown.py):
  Daily + 4H bias -> 1H location -> 15m pullback -> 5m/1m zone entry -> risk.
Orders are only alerted when the top-down passes. Gold also gets:
  - trade management: filled / +1R breakeven / TP1 / stopped / WITHDRAW warnings
  - HOLD / CAUTION / WITHDRAW status every 15 min while a trade is open
  - an alert when the Gold bias changes, and a morning "scanner online" message

Grade (0-100) of a market entry: agreement % x 0.5 + triggers x 10 (max 30)
  + higher TF agrees 15 (neutral 5) + trending 5 / ranging 3, minus penalties.
  A+ >= 85, A >= 75, B >= 65.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import data
import manage
import markets as mk
import notify
import setups
import signal_engine as se
import topdown as td

STATE_FILE = Path(os.getenv("DATA_DIR", Path(__file__).with_name("data"))) / "scanner.json"
GRADES = {"A+": 85, "A": 75, "B": 65}
TF_OPTIONS = ("1min", "5min", "15min")

DEFAULTS = dict(
    enabled=True,
    watch=["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY", "XAG/USD", "BTC/USD"],
    gold_tf="5min", other_tf="15min",
    gold_grade="B", other_grade="A",         # top-down already filters hard
    gold_limit_alerts=True, limit_alerts=False,
    gold_updates=True,                       # HOLD/WITHDRAW every 15 min while a Gold trade is open
    gold_bias_alerts=True, morning_hello=True,
    gold_hourly=True,                        # a Gold status message every hour (shows alerts are working)
    balance=100.0, risk=2.0, cent=False,
    start_hour=7, end_hour=20,               # UTC. 07-20 UTC = 08:00-21:00 Lagos (London + New York)
)

_lock = threading.Lock()
state = dict(settings=dict(DEFAULTS), last_bucket={}, latest={}, alerts=[], last_scan=None,
             last_scan_note="", notify_errors=[], gold_bias=None, bias_candidate=(None, 0),
             hello_day=None, last_error=None, gold_hourly_last=0)


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
            v = [mk.GOLD] + [x for x in v if x != mk.GOLD]          # gold is always watched, first
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
    if planned and (r.get("risk_amount") or 0) > 1.5 * planned:
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


# ---------------------------------------------------------------- data + analysis
def tf_for(symbol, s=None):
    s = s or state["settings"]
    return s["gold_tf"] if symbol == mk.GOLD else s["other_tf"]


def frames_for(symbol, tf, reserve=0, entry_max_age=20):
    """All top-down timeframes. Higher timeframes come from cache most of the time."""
    gold = symbol == mk.GOLD
    out = {}
    for f in td.frames_needed(tf):
        size = 500 if f in ("1min", "5min", "15min") else 300
        age = entry_max_age if f == tf else td.refresh_minutes(f, gold) * 60
        out[f] = data.candles(symbol, f, size, max_age=age, reserve=reserve)
    return out


def stale_count(symbol, tf):
    gold = symbol == mk.GOLD
    n = 0
    for f in td.frames_needed(tf):
        age = data.cache_age(symbol, f)
        limit = 20 if f == tf else td.refresh_minutes(f, gold) * 60
        n += age is None or age > limit
    return n


def analyze(symbol, tf, frames, balance, risk, cent=False):
    """Top-down analysis -> grade -> locked setup (fixed until it finishes)."""
    r = td.analyze(symbol, tf, frames, balance, risk, cent)
    add_grade(r, balance, risk)
    r["setup"] = setups.sync(symbol, tf, r, frames[tf])
    r["previous_setup"] = setups.last_finished(symbol, tf)
    r["my_trade"] = setups.update_manual(symbol, frames[tf])
    return r


# ---------------------------------------------------------------- schedule + budget
def market_open(symbol, now=None, s=None):
    s, now = s or state["settings"], now or datetime.now(timezone.utc)
    if not (s["start_hour"] <= now.hour < s["end_hour"]):
        return False
    crypto = mk.BY_SYMBOL.get(symbol, {}).get("category") == "Crypto"
    return crypto or now.weekday() < 5


def cost_per_day(symbol, tf, window_min):
    gold = symbol == mk.GOLD
    return sum(window_min / (se.TF_MINUTES[f] if f == tf else td.refresh_minutes(f, gold))
               for f in td.frames_needed(tf))


def estimate(s=None):
    """Credits per day the watchlist will use (weekday, inside the hours)."""
    s = s or state["settings"]
    window = max(0, s["end_hour"] - s["start_hour"]) * 60
    rows, total = [], 0
    for sym in s["watch"]:
        if not mk.allowed(sym):
            continue
        tf = tf_for(sym, s)
        cost = cost_per_day(sym, tf, window)
        rows.append(dict(symbol=sym, tf=tf, credits=round(cost)))
        total += cost
    per_min, per_day = data.credits.limits()
    budget = None if per_day is None else per_day - data.MANUAL_RESERVE_PER_DAY
    return dict(per_symbol=rows, total=round(total), budget=budget,
                fits=budget is None or total <= budget, window_min=window)


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


# ---------------------------------------------------------------- alerts
def push(title, body, priority=4, tags=None, symbol=None, kind="info"):
    alert = dict(time=int(time.time()), symbol=symbol, kind=kind, title=title, body=body, demo=not data.live())
    if data.live():
        click = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
        errs = notify.send(title, body, priority=priority, tags=tags or [], click=click)
        alert["sent"] = not errs
        if errs:
            state["notify_errors"] = ([f"{time.strftime('%H:%M')} {e}" for e in errs] + state["notify_errors"])[:5]
    else:
        alert["sent"] = False                                     # demo data never notifies
    state["alerts"].insert(0, alert)
    state["alerts"] = state["alerts"][:30]
    save()
    return alert


def _should_alert(r, s):
    gold = r["symbol"] == mk.GOLD
    if r.get("grade") and r["score"] >= GRADES[s["gold_grade"] if gold else s["other_grade"]]:
        return "market"
    if r.get("limit_orders") and (s["gold_limit_alerts"] if gold else s["limit_alerts"]):
        p = r["limit_orders"][0]
        planned = s["balance"] * s["risk"] / 100
        if p.get("topdown_ok") and p["confluence"] >= (1 if gold else 2) and p["risk_amount"] <= 1.5 * planned:
            return "limit"
    return None


def _checklist(r):
    t = r.get("topdown") or {}
    return " · ".join(f"{'✓' if x['ok'] else '✗'}{x['tf']}" for x in t.get("steps", []))


def _setup_message(r, kind):
    name = mk.BY_SYMBOL.get(r["symbol"], {}).get("name", r["symbol"])
    tf = r["interval"].replace("min", "m")
    td_line = f"Top-down {r['topdown']['stack']}: {_checklist(r)}"
    if kind == "market":
        title = f"{r['signal']} {name} now · {r['grade']} · {tf}"
        body = (f"Enter at {r['entry']} (market)\n"
                f"SL {r['stop_loss']}  |  TP1 {r['take_profit_1']} (1:2)  |  TP2 {r['take_profit_2']}\n"
                f"Lot {r['lot_size']} (risk ${r['risk_amount']:.2f})\n"
                f"Entry: {r.get('entry_zone', '')}\nSL: {r.get('sl_reason', '')}\n{td_line}")
        tags = ["green_circle" if r["signal"] == "BUY" else "red_circle"]
        warn = any("Min lot" in w for w in r.get("warnings", []))
    else:
        p = r["limit_orders"][0]
        title = f"{p['side']} {name} · {tf}"
        body = (f"Place {p['side'].lower()} at {p['entry']} (price now {r['price']})\n"
                f"SL {p['stop_loss']}  |  TP1 {p['take_profit_1']} (1:2)  |  TP2 {p['take_profit_2']}\n"
                f"Lot {p['lot_size']} (risk ${p['risk_amount']:.2f})\n"
                f"Zone: {p['zone']}\nCancel if not filled within {p['expires_in_minutes']} min\n{td_line}")
        tags = ["hourglass"]
        warn = "Min lot" in p.get("lot_note", "")
    if warn:
        body += "\n⚠ Min lot 0.01 is above your risk %"
    return title, body, tags


def _gold_status(r):
    """Hourly Gold summary: top-down, what to wait for, and any open setup."""
    t = r.get("topdown") or {}
    lines = [" · ".join(f"{'✓' if x['ok'] else '✗'} {x['tf']} {x['dir'].lower()}" for x in t.get("steps", []))]
    setup = r.get("setup")
    if setup:
        o = setup["orders"][0]
        adv = (o.get("advice") or {}).get("verdict")
        lines.append(f"Setup: {o['side']} @ {o['entry']} — {o['status'].lower()}" + (f" · {adv}" if adv else ""))
    elif r.get("limit_orders"):
        p = r["limit_orders"][0]
        lines.append(f"Plan: {p['side'].lower()} at {p['entry']} (SL {p['stop_loss']}, TP1 {p['take_profit_1']})")
    else:
        lines.append("Plan: no order — " + (r.get("reasons") or ["waiting"])[0])
    mine = r.get("my_trade")
    if mine:
        o = mine["orders"][0]
        lines.append(f"Your trade: {o['side']} @ {o['entry']} — {(o.get('advice') or {}).get('verdict', o['status'])}")
    return (f"🟡 Gold {r['price']} · bias {t.get('bias', '?').lower()}", "\n".join(lines), 3, ["bar_chart"])


def handle_alerts(r, s):
    """New setups, trade management and Gold bias changes for one analysed market."""
    out = []
    sym, gold = r["symbol"], r["symbol"] == mk.GOLD
    name = mk.BY_SYMBOL.get(sym, {}).get("name", sym)
    setup = r.get("setup")
    if setup and setup.get("new") and not setup.get("checked"):
        setup["checked"] = True
        kind = _should_alert(r, s)
        if kind:
            setup["alerted"] = True
            title, body, tags = _setup_message(r, kind)
            out.append(push(title, body, 5 if gold else 4, tags, sym, kind))
    every = 15 if (gold and s["gold_updates"]) else 0
    for st_ in (r.get("previous_setup"), setup, r.get("my_trade")):
        if st_ and (st_.get("alerted") or st_.get("shown") or st_.get("tf") == "manual"):
            label = name + (" (your trade)" if st_.get("tf") == "manual" else "")
            for title, body, prio, tags in manage.check(label, sym, r["interval"], st_, r, updates_every=every):
                out.append(push(title, body, prio, tags, sym, "manage"))
    if gold and s.get("gold_hourly") and time.time() - state["gold_hourly_last"] >= 3600:
        state["gold_hourly_last"] = time.time()
        out.append(push(*_gold_status(r), symbol=sym, kind="update"))
    if gold and s["gold_bias_alerts"]:
        bias = r["topdown"]["bias"]
        cand, n = state["bias_candidate"]
        n = n + 1 if cand == bias else 1
        state["bias_candidate"] = (bias, n)
        if n >= 2 and bias != state["gold_bias"]:                 # confirmed on 2 checks in a row
            if state["gold_bias"] is not None:
                steps = r["topdown"]["steps"]
                look = "buys only" if bias == "Bullish" else "sells only" if bias == "Bearish" else "no trades until it's clear"
                out.append(push(f"🧭 Gold bias now {bias.upper()}",
                                f"{' · '.join(x['tf'] + ' ' + x['dir'].lower() for x in steps[:3])}\nLook for {look}. "
                                + ("If you hold a trade the other way, consider closing it." if bias != "Neutral" else ""),
                                4, ["compass"], sym, "bias"))
            state["gold_bias"] = bias
    return out


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
        due_list = due(now_ts, s) if s["enabled"] else []
        day = datetime.fromtimestamp(now_ts, timezone.utc).date().isoformat()
        if s["morning_hello"] and due_list and state["hello_day"] != day and market_open(mk.GOLD, datetime.fromtimestamp(now_ts, timezone.utc), s):
            state["hello_day"] = day
            others = len([x for x in s["watch"] if x != mk.GOLD and mk.allowed(x)])
            alerts.append(push("☀️ Scanner online", f"Watching Gold every {s['gold_tf'].replace('min', ' min')} "
                               f"(top-down) + {others} other markets. You'll get setups, fills and HOLD/WITHDRAW updates here.",
                               3, ["sunny"], mk.GOLD, "hello")["title"])
        for sym, bucket, _ in due_list:
            tf = tf_for(sym, s)
            need = stale_count(sym, tf)
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
                frames = frames_for(sym, tf, reserve=data.MANUAL_RESERVE_PER_MIN)
                r = analyze(sym, tf, frames, s["balance"], s["risk"], s["cent"])
            except data.Busy as e:
                notes.append(str(e))
                break
            except Exception as e:
                state["latest"][sym] = dict(symbol=sym, error=str(e)[:160], time=int(now_ts))
                state["last_error"] = f"{sym}: {str(e)[:160]}"
                state["last_bucket"][sym] = bucket
                continue
            state["last_bucket"][sym] = bucket
            state["latest"][sym] = _summary(r)
            checked.append(sym)
            alerts += [a["title"] for a in handle_alerts(r, s)]
        state["last_scan"] = int(now_ts)
        state["last_scan_note"] = " ".join(dict.fromkeys(notes))
        return dict(ok=True, checked=checked, alerts=alerts, note=state["last_scan_note"])
    finally:
        _lock.release()


def _summary(r):
    keep = ("symbol", "interval", "signal", "grade", "score", "price", "regime", "entry")
    out = {k: r[k] for k in keep if k in r}
    out["time"] = int(time.time())
    t = r.get("topdown") or {}
    out["bias"], out["checklist"] = t.get("bias"), _checklist(r)
    if r.get("setup"):
        o = r["setup"]["orders"][0]
        out["setup"] = dict(side=o["side"], entry=o["entry"], status=o["status"], new=r["setup"]["new"],
                            advice=(o.get("advice") or {}).get("verdict"))
    if r.get("my_trade"):
        o = r["my_trade"]["orders"][0]
        out["my_trade"] = dict(side=o["side"], entry=o["entry"], status=o["status"], advice=o.get("advice"))
    return out


def problems():
    """Plain reasons why alerts might not reach the phone."""
    p = []
    s = state["settings"]
    if not data.live():
        p.append("TWELVE_DATA_API_KEY is not set on Render - the site runs on demo data and never sends alerts.")
    if not notify.channels():
        p.append("NTFY_TOPIC is not set on Render - there is nowhere to send alerts.")
    if not s["enabled"]:
        p.append("The scanner is switched off.")
    now = datetime.now(timezone.utc)
    if not market_open(mk.GOLD, now, s):
        p.append("Outside scan hours (08:00-21:00 Lagos, Mon-Fri). Nothing is checked now - that's normal.")
    elif not state["last_scan"] or time.time() - state["last_scan"] > 180:
        p.append("The every-minute scan link is not being called. Set it up on cron-job.org (Step 6) "
                 "with ?key= followed by your APP_PASSWORD.")
    if state["notify_errors"]:
        p.append("Last notification failed: " + state["notify_errors"][0])
    if state["last_error"]:
        p.append("Last data error: " + state["last_error"])
    return p


def status():
    return dict(settings=state["settings"], estimate=estimate(), credits=data.credits.snapshot(),
                live=data.live(), channels=notify.channels(), last_scan=state["last_scan"],
                note=state["last_scan_note"], latest=state["latest"], alerts=state["alerts"][:20],
                notify_errors=state["notify_errors"], plan=mk.plan(), problems=problems(),
                gold_bias=state["gold_bias"],
                stacks={k: td.frames_needed(k) for k in td.STACKS}, refresh=td.REFRESH,
                tf_minutes={k: se.TF_MINUTES[k] for k in ("1min", "5min", "15min", "1h", "4h", "1day")})


load()
