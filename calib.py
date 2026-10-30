"""
calib.py - measure, on this market's OWN history, how the structure rules really performed.

For one symbol + timeframe it replays every past BOS/CHoCH with exactly the live code (structure.read):

  POI reaction  after price first returns into the zone, does it move 1R away (R = zone to the stop
                beyond the protected HL/LH) before a candle closes through the zone?
                Counted per zone type and number of confluences.
  Plan rule     the full order: limit at the best order block, SL beyond the HL/LH, TP at the HH/LL,
                only when R:R after spread >= 1.5. Pending orders are cancelled if a new BOS/CHoCH
                comes first; after a fill it runs to TP or SL. Spread is charged. Result in R.

No look-ahead: each structure is judged with the candles that had closed at that moment.
The site uses up to 5,000 candles per timeframe (one Twelve Data request) and caches the result.
"""
from __future__ import annotations

import time

import structure as stx


def _poi_outcome(c, start, d, top, bot, risk, max_bars=400):
    entry = top if d == 1 else bot
    touched = False
    for t in range(start, min(start + max_bars, c.n)):
        if not touched:
            if (d == 1 and c.l[t] <= top) or (d == -1 and c.h[t] >= bot):
                touched = True
            else:
                continue
        if (d == 1 and c.c[t] < bot) or (d == -1 and c.c[t] > top):
            return 0
        if (d == 1 and c.h[t] >= entry + risk) or (d == -1 and c.l[t] <= entry - risk):
            return 1
    return None


def _plan_outcome(c, start, stop_pending, pl, spread):
    """R result of one plan, or None if it never filled."""
    d, entry, sl, tp = pl["dir"], pl["entry"], pl["sl"], pl["tp"]
    risk = abs(entry - sl)
    filled = False
    for t in range(start, c.n):
        if not filled:
            if t > stop_pending:
                return None                                  # new BOS/CHoCH first: order cancelled
            if (d == 1 and c.l[t] <= entry) or (d == -1 and c.h[t] >= entry):
                filled = True
                if (d == 1 and c.l[t] <= sl) or (d == -1 and c.h[t] >= sl):
                    return -1 - spread / risk
                continue
            if (d == 1 and c.h[t] >= tp) or (d == -1 and c.l[t] <= tp):
                return None                                  # target reached without a fill
        else:
            if (d == 1 and c.l[t] <= sl) or (d == -1 and c.h[t] >= sl):
                return -1 - spread / risk
            if (d == 1 and c.h[t] >= tp) or (d == -1 and c.l[t] <= tp):
                return abs(tp - entry) / risk - spread / risk
    return None


def compute(df, symbol, tf_label):
    t0 = time.time()
    c = stx.Lite(df)
    evs = stx.events(c, c.n - 1)
    poi, results = {}, []
    spread = stx.se.SYMBOLS.get(symbol, {}).get("spread", 0.0)
    for k, E in enumerate(evs):
        nxt = evs[k + 1]["bar"] if k + 1 < len(evs) else c.n - 1
        cut = E["bar"] + stx.CONFIRM
        if cut >= nxt or cut >= c.n - 2:
            continue
        R = stx.read(c, cut, symbol)
        if not R["E"] or R["E"]["bar"] != E["bar"]:
            continue
        d = R["dir"]
        for p in R["pois"]:
            entry = p["top"] if d == 1 else p["bot"]
            res = _poi_outcome(c, cut + 1, d, p["top"], p["bot"], abs(entry - R["origin"]) or 1e-9)
            if res is None:
                continue
            for key in (f"{p['type']}|{min(p['n'], 4)}", f"{p['type']}|*", f"*|{min(p['n'], 4)}"):
                row = poi.setdefault(key, [0, 0])
                row[0] += res
                row[1] += 1
        # the full order, as offered live once the HH/LL is confirmed
        ext = [j for j in (c.sh if d == 1 else c.sl) if j >= E["bar"] and j + stx.K < nxt]
        if not ext:
            continue
        t = max(cut, ext[0] + stx.K)
        R2 = stx.read(c, t, symbol)
        pl = R2["plan"]
        if (not R2["E"] or R2["E"]["bar"] != E["bar"] or R2["neutral"] or not pl or pl["tp"] is None
                or pl["rr_net"] < stx.MIN_RR or pl["status"] != "waiting" or pl["ob"]["status"] in ("invalid", "mitigated")):
            continue
        res = _plan_outcome(c, t + 1, nxt, pl, spread)
        if res is not None:
            results.append(res)
    wins = sum(1 for x in results if x > 0)
    days = (df.index[-1] - df.index[0]).total_seconds() / 86400
    span = f"{tf_label} history, last {days:.0f} days" if days >= 2 else f"{tf_label} history, last {days * 24:.0f} hours"
    return dict(span=span, candles=c.n, structures=len(evs), seconds=round(time.time() - t0, 2),
                poi={k: tuple(v) for k, v in poi.items()},
                rule=dict(trades=len(results), wins=wins, win_rate=round(100 * wins / len(results), 1) if results else 0.0,
                          avg_r=round(sum(results) / len(results), 3) if results else 0.0,
                          total_r=round(sum(results), 2)))
