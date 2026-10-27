"""
journal.py - the results of every setup the site gave, so you can see if it really makes money.

Each finished order is written once:
  TP1 HIT  -> win  (+2R)        STOPPED -> loss (-1R)
  EXPIRED / MISSED / INVALID / CANCELLED / CLEARED -> not taken (0R, not in win rate)
R = how many times your risk. +2R with $2 risk = +$4.
"""
import threading
import time

import store

_lock = threading.Lock()
KEY = "journal"
MAX_ROWS = 2000
TERMINAL = {"TP1 HIT", "STOPPED", "EXPIRED", "MISSED", "INVALID", "CANCELLED", "CLEARED"}
R_OF = {"TP1 HIT": 2.0, "STOPPED": -1.0}
rows: list = []
_seen: set = set()


def load():
    global rows, _seen
    rows = store.get(KEY, []) or []
    _seen = {x["id"] for x in rows}


def record(setup, mode=None):
    """Write every finished order of a setup (site setup or your own trade) once."""
    if not setup:
        return 0
    added = 0
    with _lock:
        for n, o in enumerate(setup["orders"]):
            if o["status"] not in TERMINAL:
                continue
            rid = f"{setup['id']}|{n}"
            if rid in _seen:
                continue
            r_mult = R_OF.get(o["status"], 0.0)
            risk_d = abs(o["entry"] - o["stop_loss"])
            if o["status"] == "TP1 HIT" and risk_d:
                r_mult = round(abs(o["take_profit_1"] - o["entry"]) / risk_d, 2)   # 2R, or more if TP1 was further
            risk = o.get("risk_amount")
            rows.insert(0, dict(
                id=rid, symbol=setup["symbol"], tf=setup["tf"], side=o["side"], kind=o["kind"],
                entry=o["entry"], stop_loss=o["stop_loss"], take_profit_1=o["take_profit_1"],
                status=o["status"], r=r_mult, risk=risk,
                pnl=round(r_mult * risk, 2) if risk else None,
                created=setup.get("created"), filled_at=o.get("filled_at"), closed_at=o.get("closed_at") or int(time.time()),
                grade=setup.get("grade"), zone=o.get("zone", ""), mode=mode,
                taken=o["status"] in R_OF))
            _seen.add(rid)
            added += 1
        if added:
            del rows[MAX_ROWS:]
            store.put(KEY, rows)
    return added


def reset():
    global rows, _seen
    with _lock:
        rows, _seen = [], set()
        store.put(KEY, rows)


def stats(symbol=None, days=None):
    since = time.time() - days * 86400 if days else 0
    sel = [x for x in rows if (not symbol or x["symbol"] == symbol) and (x["closed_at"] or 0) >= since]
    taken = [x for x in sel if x["taken"]]
    wins = [x for x in taken if x["r"] > 0]
    total_r = sum(x["r"] for x in taken)
    pnl = sum(x["pnl"] or 0 for x in taken)
    # equity curve, oldest first
    curve, run = [], 0.0
    for x in sorted(taken, key=lambda x: x["closed_at"] or 0):
        run += x["r"]
        curve.append(dict(t=x["closed_at"], r=round(run, 2)))
    streak, kind = 0, None
    for x in taken:                                  # newest first
        k = "W" if x["r"] > 0 else "L"
        if kind in (None, k):
            kind, streak = k, streak + 1
        else:
            break
    by_symbol = {}
    for x in taken:
        b = by_symbol.setdefault(x["symbol"], dict(trades=0, wins=0, r=0.0))
        b["trades"] += 1
        b["wins"] += x["r"] > 0
        b["r"] = round(b["r"] + x["r"], 2)
    by_kind = {}
    for x in taken:
        b = by_kind.setdefault(x["kind"], dict(trades=0, wins=0))
        b["trades"] += 1
        b["wins"] += x["r"] > 0
    not_taken = {}
    for x in sel:
        if not x["taken"]:
            not_taken[x["status"]] = not_taken.get(x["status"], 0) + 1
    # break-even win rate at 1:2 is 33.3%
    return dict(trades=len(taken), wins=len(wins), losses=len(taken) - len(wins),
                win_rate=round(100 * len(wins) / len(taken), 1) if taken else None,
                total_r=round(total_r, 2), pnl=round(pnl, 2),
                expectancy=round(total_r / len(taken), 2) if taken else None,
                streak=f"{streak}{kind}" if kind else "", curve=curve,
                by_symbol=by_symbol, by_kind=by_kind, not_taken=not_taken, storage=store.backend())


load()
