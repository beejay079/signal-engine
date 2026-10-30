"""
structure.py - market structure of ONE timeframe, with fixed price levels.

For every timeframe:
  1. Bias        BULLISH / BEARISH from the last valid BOS/CHoCH, or NEUTRAL when there is no break
                 yet or the protected HL/LH has been lost (possible reversal, waiting for a CHoCH).
  2. Structure   last BOS / CHoCH (exact price + time), swing points HH / HL / LH / LL (exact prices).
  3. Movement    impulsive or corrective; supports the direction, or warns of a possible reversal.
  4. POIs        order blocks, liquidity sweep, supply/demand base (+ FVG as confirmation), exact ranges.
                 Reaction % is MEASURED on this market's own history (calib.py), never guessed.
  5. Plan        limit at the best order block, SL beyond the protected HL/LH, TP at the HH/LL, R:R.
                 TRADE only if R:R >= 1.5, the zone beats a coin flip historically and the same rule
                 made money on this timeframe's history. Otherwise NO TRADE - no forced signals.

FIXED LEVELS: every price comes from closed candles around the last BOS/CHoCH and is judged once,
CONFIRM candles after the break (ATR is measured on the 14 candles up to the break). The current price
is never used for a level. Probabilities and the TRADE / NO TRADE decision are frozen the first time a
structure is seen. A timeframe is re-read only on a new BOS/CHoCH on THAT timeframe; its setup also
updates its status when it is invalidated or completed. Other timeframes never change its levels.
"""
from __future__ import annotations

import threading
import time

import numpy as np

import signal_engine as se

K = se.SWING_K
CONFIRM = 4            # candles after a BOS/CHoCH before its zones are judged (then fixed)
MIN_CONFLUENCE = 2     # a POI needs 2+ confluences besides "caused the break"
MIN_RR = 1.5           # below this the structural target is not worth the risk -> NO TRADE
MIN_RULE_TRADES = 20   # history needed before the rule can be trusted on a timeframe
PRIOR = 5              # Beta(5,5) prior: small samples are pulled towards 50%, not trusted blindly


class Lite:
    """Light chart context: prices, times and confirmed fractal swings (same as signal_engine)."""

    def __init__(self, df):
        self.o, self.h = df["open"].to_numpy(float), df["high"].to_numpy(float)
        self.l, self.c = df["low"].to_numpy(float), df["close"].to_numpy(float)
        self.idx, self.n = df.index, len(df)
        self.sh, self.sl = se.find_swings(self.h, self.l)


def _t(c, j):
    return c.idx[j].isoformat()


def _local_atr(c, b, n=14):
    j0 = max(1, b - n + 1)
    tr = [max(c.h[j] - c.l[j], abs(c.h[j] - c.c[j - 1]), abs(c.l[j] - c.c[j - 1])) for j in range(j0, b + 1)]
    return float(np.mean(tr)) if tr else float(c.h[b] - c.l[b])


# --------------------------------------------------------------------------- swings + events (as of bar u)
def swings(c, u):
    """Swings confirmed by bar u, labelled vs the previous swing of the same kind."""
    pts = sorted([(j, "H", float(c.h[j])) for j in c.sh if j + K <= u] + [(j, "L", float(c.l[j])) for j in c.sl if j + K <= u])
    out, ph, pl = [], None, None
    for j, t, v in pts:
        if t == "H":
            lab, ph = ("H" if ph is None else "HH" if v > ph else "LH"), v
        else:
            lab, pl = ("L" if pl is None else "HL" if v > pl else "LL"), v
        out.append(dict(bar=j, kind=t, label=lab, price=v))
    return out


def events(c, u):
    """Every BOS / CHoCH up to bar u: a candle CLOSE beyond the last confirmed swing (a wick is only a sweep)."""
    sh = [(j + K, j) for j in c.sh]
    sl = [(j + K, j) for j in c.sl]
    last_h = last_l = None
    ph = pl = 0
    trend, out = 0, []
    for b in range(u + 1):
        while ph < len(sh) and sh[ph][0] <= b:
            last_h, ph = sh[ph][1], ph + 1
        while pl < len(sl) and sl[pl][0] <= b:
            last_l, pl = sl[pl][1], pl + 1
        if last_h is not None and c.c[b] > c.h[last_h]:
            out.append(dict(bar=b, dir=1, kind="BOS" if trend in (0, 1) else "CHoCH", level=float(c.h[last_h]), swing_bar=last_h))
            trend, last_h = 1, None
        if last_l is not None and c.c[b] < c.l[last_l]:
            out.append(dict(bar=b, dir=-1, kind="BOS" if trend in (0, -1) else "CHoCH", level=float(c.l[last_l]), swing_bar=last_l))
            trend, last_l = -1, None
    return out


# --------------------------------------------------------------------------- zones
def _ob_candle(c, d, start, stop):
    for j in range(start, stop - 1, -1):
        if (d == 1 and c.c[j] < c.o[j]) or (d == -1 and c.c[j] > c.o[j]):
            return j
    return None


def _fvgs(c, d, j_from, j_to, atr_b):
    out = []
    for j in range(max(j_from, 2), j_to + 1):
        if d == 1 and c.l[j] - c.h[j - 2] > 0.1 * atr_b and c.l[j + 1:j_to + 1].min(initial=np.inf) > c.h[j - 2]:
            out.append(dict(bot=float(c.h[j - 2]), top=float(c.l[j]), bar=j))
        if d == -1 and c.l[j - 2] - c.h[j] > 0.1 * atr_b and c.h[j + 1:j_to + 1].max(initial=-np.inf) < c.l[j - 2]:
            out.append(dict(bot=float(c.h[j]), top=float(c.l[j - 2]), bar=j))
    return out


def _status(c, d, top, bot, after, u):
    """fresh / tapped / mitigated / invalid on candles after `after` up to u - never moves the levels."""
    l, h, cl = c.l[after + 1:u + 1], c.h[after + 1:u + 1], c.c[after + 1:u + 1]
    if not len(cl):
        return "fresh"
    if (d == 1 and (cl < bot).any()) or (d == -1 and (cl > top).any()):
        return "invalid"
    inside = (l <= top) if d == 1 else (h >= bot)
    touches = int(np.sum(inside & np.r_[True, ~inside[:-1]]))
    return "fresh" if touches == 0 else "tapped" if touches == 1 else "mitigated"


# --------------------------------------------------------------------------- the read (levels only)
def read(c, u, symbol):
    """Structure, POIs and plan levels of one timeframe as of closed bar u. No probabilities here."""
    spread = se.SYMBOLS.get(symbol, {}).get("spread", 0.0)
    sw = swings(c, u)
    ev = events(c, u)
    R = dict(u=u, swings=sw, events=ev, dir=0, neutral=None, E=None, pois=[], plan=None, fvgs=[])
    if not ev:
        R["neutral"] = "no confirmed BOS/CHoCH in view - ranging"
        return R
    E = ev[-1]
    d, b, j0 = E["dir"], E["bar"], E["swing_bar"]
    up = d == 1
    atr_b = _local_atr(c, b)
    seg = c.l[j0:b + 1] if up else c.h[j0:b + 1]
    o_bar = j0 + int(np.argmin(seg) if up else np.argmax(seg))
    origin = float(c.l[o_bar] if up else c.h[o_bar])
    prev = [s for s in sw if s["kind"] == ("L" if up else "H") and s["bar"] < o_bar]
    better = (origin > prev[-1]["price"] if up else origin < prev[-1]["price"]) if prev else True
    origin_lab = ("HL" if better else "LL") if up else ("LH" if better else "HH")
    ext = [j for j in (c.sh if up else c.sl) if j >= b and j + K <= u]
    ext_bar = ext[0] if ext else None
    extreme = float(c.h[ext_bar] if up else c.l[ext_bar]) if ext_bar is not None else None
    R.update(E=E, dir=d, atr=atr_b, o_bar=o_bar, origin=origin, origin_lab=origin_lab, ext_bar=ext_bar,
             extreme=extreme, ext_lab="HH" if up else "LL", spread=spread)

    # protected level lost after the break -> neutral (possible reversal, no CHoCH yet)
    after = c.c[b + 1:u + 1]
    if len(after) and ((up and (after < origin).any()) or (not up and (after > origin).any())):
        R["neutral"] = (f"price closed {'below' if up else 'above'} the protected {origin_lab} {origin:.{_dg(symbol)}f} - "
                        f"possible reversal, waiting for a CHoCH to confirm")
    cut = b + CONFIRM
    R["cut"] = cut
    if u < cut:
        return R
    fv = _fvgs(c, d, o_bar, cut, atr_b)
    R["fvgs"] = fv
    leg_ext = float(c.h[o_bar:cut + 1].max() if up else c.l[o_bar:cut + 1].min())
    mid = (origin + leg_ext) / 2
    cands = []
    j_ext = _ob_candle(c, d, o_bar, max(o_bar - 5, j0))
    j_ext = o_bar if j_ext is None else j_ext
    cands.append(("Order block (extreme)", j_ext,
                  float(min(c.l[j_ext:o_bar + 1].min(), origin)) if up else float(c.l[j_ext]),
                  float(c.h[j_ext]) if up else float(max(c.h[j_ext:o_bar + 1].max(), origin))))
    j_dec = _ob_candle(c, d, b - 1, o_bar + 1)
    if j_dec is not None and j_dec != j_ext:
        cands.append(("Order block (decisional)", j_dec, float(c.l[j_dec]), float(c.h[j_dec])))
    base = [(max(c.o[j], c.c[j]), min(c.o[j], c.c[j])) for j in range(max(o_bar - 2, j0), min(o_bar + 2, b) + 1)
            if abs(c.c[j] - c.o[j]) <= 0.6 * atr_b]
    base_top = (max(x[0] for x in base) if up else origin) if base else None
    base_bot = (origin if up else min(x[1] for x in base)) if base else None
    if base_top is not None and base_top > base_bot:
        cands.append(("Demand zone" if up else "Supply zone", o_bar, float(base_bot), float(base_top)))
    swept = None
    for s_ in reversed([x for x in sw if x["kind"] == ("L" if up else "H") and o_bar - 80 <= x["bar"] < o_bar]):
        took = (up and origin < s_["price"]) or (not up and origin > s_["price"])
        if took and abs(s_["price"] - origin) <= atr_b and \
                ((up and c.c[o_bar:o_bar + 3].max() > s_["price"]) or (not up and c.c[o_bar:o_bar + 3].min() < s_["price"])):
            swept = s_
            break
        if took:
            break
    if swept:
        cands.append(("Liquidity sweep", o_bar, *((origin, swept["price"]) if up else (swept["price"], origin))))

    pois = []
    for name, j, bot, top in cands:
        if top <= bot:
            continue
        nxt = [t for t in range(j + 1, min(j + 5, cut + 1)) if t >= 2]
        disp = any((up and c.l[t] - c.h[t - 2] > 0.1 * atr_b) or (not up and c.l[t - 2] - c.h[t] > 0.1 * atr_b) for t in nxt) \
            or abs(c.c[min(j + 3, cut)] - c.o[min(j + 1, cut)]) > 1.5 * atr_b
        if name.startswith("Order block") and not disp:
            continue                                   # an order block without displacement is not valid
        conf = []
        if disp:
            conf.append("displacement (imbalance) after it")
        if (up and top <= mid) or (not up and bot >= mid):
            conf.append("in discount" if up else "in premium")
        if swept and name != "Liquidity sweep" and bot <= max(swept["price"], origin) + 0.3 * atr_b \
                and top >= min(swept["price"], origin) - 0.3 * atr_b:
            conf.append(f"liquidity swept at {swept['price']:.{_dg(symbol)}f}")
        if base_top is not None and name.startswith("Order block") and bot <= base_top and top >= base_bot:
            conf.append("overlaps the " + ("demand" if up else "supply") + " zone")
        if any(x["bot"] <= top + 0.3 * atr_b and x["top"] >= bot - 0.3 * atr_b for x in fv):
            conf.append("fair value gap confirms")
        if len(conf) < MIN_CONFLUENCE:
            continue
        st = _status(c, d, top, bot, max(cut, ext_bar or cut), u)
        pois.append(dict(type=name, bar=j, bot=bot, top=top, conf=conf, n=len(conf), status=st,
                         rank=(len(conf), name == "Order block (extreme)")))
    pois.sort(key=lambda p: p["rank"], reverse=True)
    R["pois"] = pois

    obs = [p for p in pois if p["type"].startswith("Order block")]
    if not obs:
        return R
    ob = obs[0]                                       # fixed ranking, never live status
    entry = ob["top"] if up else ob["bot"]
    sl = origin - d * (0.1 * atr_b + spread)
    risk = abs(entry - sl)
    pl = dict(dir=d, ob=ob, entry=entry, sl=sl, tp=None, rr=None, rr_net=None, status="forming")
    if extreme is not None:
        tp = extreme
        pl.update(tp=tp, rr=abs(tp - entry) / risk if risk else 0,
                  rr_net=(abs(tp - entry) - spread) / (risk + spread) if risk else 0)
        st, filled = "waiting", False
        for t in range(ext_bar + K + 1, u + 1):
            if not filled:
                if (up and c.l[t] <= entry) or (not up and c.h[t] >= entry):
                    filled, st = True, "filled"
                    if (up and c.l[t] <= sl) or (not up and c.h[t] >= sl):
                        st = "stopped"
                        break
                elif (up and c.h[t] >= tp) or (not up and c.l[t] <= tp):
                    st = "missed"
                    break
            else:
                if (up and c.l[t] <= sl) or (not up and c.h[t] >= sl):
                    st = "stopped"
                    break
                if (up and c.h[t] >= tp) or (not up and c.l[t] <= tp):
                    st = "tp hit"
                    break
        pl["status"] = st
    if ob["status"] == "invalid" and pl["status"] in ("forming", "waiting"):
        pl["status"] = "invalid"
    R["plan"] = pl
    return R


def plan_status(c, d, entry, sl, tp, start, u):
    """waiting / filled / stopped / tp hit / missed - replayed on candles from `start` to u."""
    up, filled = d == 1, False
    for t in range(max(start, 0), u + 1):
        if not filled:
            if (up and c.l[t] <= entry) or (not up and c.h[t] >= entry):
                filled = True
                if (up and c.l[t] <= sl) or (not up and c.h[t] >= sl):
                    return "stopped"
            elif (up and c.h[t] >= tp) or (not up and c.l[t] <= tp):
                return "missed"
        else:
            if (up and c.l[t] <= sl) or (not up and c.h[t] >= sl):
                return "stopped"
            if (up and c.h[t] >= tp) or (not up and c.l[t] <= tp):
                return "tp hit"
    return "filled" if filled else "waiting"


def _dg(symbol):
    return se.SYMBOLS.get(symbol, {}).get("digits", 5)


# --------------------------------------------------------------------------- probabilities from history
def zone_probability(calib, ptype, n):
    """Reaction rate of zones like this on this market + timeframe's own history (Beta(5,5) prior)."""
    if not calib:
        return None, "history not loaded yet"
    tab = calib["poi"]
    for key, what in (((ptype, min(n, 4)), f"{ptype.lower()}s with {min(n, 4)}+ confluences" if n >= 4 else f"{ptype.lower()}s with {n} confluences"),
                      ((ptype, "*"), f"all {ptype.lower()}s"), (("*", min(n, 4)), f"zones with {min(n, 4)} confluences")):
        w, m = tab.get(f"{key[0]}|{key[1]}", (0, 0))
        if m >= 10:
            p = (w + PRIOR) / (m + 2 * PRIOR)
            return int(round(100 * p)), f"{w} of {m} {what} reacted ({calib['span']})"
    return None, f"too few similar zones in {calib['span']} to measure"


# --------------------------------------------------------------------------- frozen snapshots + records
_lock = threading.Lock()
REG: dict = {}          # (symbol, tf, structure id) -> frozen snapshot
MAX_REG = 600


def _freeze(key, make):
    with _lock:
        if key not in REG:
            REG[key] = make()
            if len(REG) > MAX_REG:
                for k in sorted(REG, key=lambda k: REG[k]["first_seen"])[:100]:
                    REG.pop(k, None)
        return REG[key]


def previous(symbol, tf, current_id, n=5):
    with _lock:
        rows = [v for (s, t, i), v in REG.items() if s == symbol and t == tf and i != current_id]
    rows.sort(key=lambda v: v["first_seen"], reverse=True)
    return [dict(id=v["id"], bias=v["bias"], event=v["event"], plan=v.get("plan_snapshot"),
                 last_status=v.get("last_status"), first_seen=v["first_seen"]) for v in rows[:n]]


# --------------------------------------------------------------------------- public: one timeframe
def analyse_tf(c, tf_label, symbol, calib=None):
    """c = Lite on CLOSED candles. Everything here depends only on this timeframe."""
    dg = _dg(symbol)
    f = lambda x: None if x is None else round(float(x), dg)
    u = c.n - 1
    R = read(c, u, symbol)
    sw = R["swings"]
    out = dict(tf=tf_label, bias="NEUTRAL", dir=0, event=None, history=[], movement=None, pois=[], confirmations=[],
               plan=None, id=None, swings=[dict(label=s["label"], price=f(s["price"]), time=_t(c, s["bar"])) for s in sw[-8:]],
               calibration=calib and dict(span=calib["span"], rule=calib["rule"]))
    if not R["E"]:
        out["reason"] = "Neutral: " + R["neutral"] + "."
        return out
    E, d, up = R["E"], R["dir"], R["dir"] == 1
    sid = f"{tf_label}|{_t(c, E['bar'])}"
    out["id"] = sid
    out["event"] = dict(kind=E["kind"], dir="up" if up else "down", price=f(E["level"]), time=_t(c, E["bar"]),
                        broke=f"{'swing high' if up else 'swing low'} of {c.idx[E['swing_bar']].strftime('%d %b %H:%M')}")
    out["history"] = [dict(kind=x["kind"], dir="up" if x["dir"] == 1 else "down", price=f(x["level"]), time=_t(c, x["bar"]))
                      for x in R["events"][-5:-1]]
    with _lock:                                             # a stored snapshot overrides today's re-read
        old = REG.get((symbol, tf_label, sid))
        g0, t0 = (old or {}).get("geo"), (old or {}).get("tp")
    if g0:
        R["origin"], R["origin_lab"] = g0["origin"], g0["origin_lab"]
        cl = c.c[E["bar"] + 1:u + 1]
        lost = len(cl) and ((up and (cl < R["origin"]).any()) or (not up and (cl > R["origin"]).any()))
        R["neutral"] = (f"price closed {'below' if up else 'above'} the protected {R['origin_lab']} {R['origin']:.{dg}f} - "
                        f"possible reversal, waiting for a CHoCH to confirm") if lost else None
    if t0:
        R["extreme"], R["ext_lab"] = t0["price"], t0["label"]
        R["ext_bar"] = int(min(max(c.idx.searchsorted(t0["time"]), 0), u))
    out["key"] = dict(origin=f(R["origin"]), origin_label=R["origin_lab"], origin_time=_t(c, R["o_bar"]),
                      extreme=f(R["extreme"]), extreme_label=R["ext_lab"] if R["extreme"] is not None else None,
                      extreme_time=_t(c, R["ext_bar"]) if R["ext_bar"] is not None else None)
    seq = " → ".join(f"{s['label']} {f(s['price'])}" for s in sw[-4:])
    word = "bullish" if up else "bearish"
    if R["neutral"]:
        out["reason"] = (f"Neutral: the last break was a {E['kind']} {'up' if up else 'down'} at {f(E['level'])}, but "
                         f"{R['neutral']}. Swings: {seq}.")
    else:
        out.update(bias="BULLISH" if up else "BEARISH", dir=d)
        out["reason"] = (f"{word.title()}: {E['kind']} {'up' if up else 'down'} - a candle closed {'above' if up else 'below'} "
                         f"the swing {'high' if up else 'low'} {f(E['level'])} ({c.idx[E['bar']].strftime('%d %b %H:%M')}), "
                         f"and the protected {R['origin_lab']} {f(R['origin'])} still holds. Swings: {seq}.")

    # ---- movement
    price = float(c.c[u])
    if R["neutral"]:
        out["movement"] = dict(phase="reversal risk", direction="down" if up else "up", supports=False, reversal=True,
                               text=f"Price broke the protected {R['origin_lab']} - the {word} move failed. "
                                    f"Possible reversal: a CHoCH {'down' if up else 'up'} would confirm it.")
    elif R["ext_bar"] is None:
        run = float(c.h[E["bar"]:u + 1].max() if up else c.l[E["bar"]:u + 1].min())
        out["movement"] = dict(phase="impulsive", direction="up" if up else "down", supports=True, reversal=False,
                               text=f"Impulsive {'up' if up else 'down'} after the {E['kind']} "
                                    f"({abs(run - R['origin']) / R['atr']:.1f} ATR from the {R['origin_lab']}) - supports the {word} direction. "
                                    f"The {R['ext_lab']} is not confirmed yet, so there is no target until a pullback starts.")
    else:
        rng = abs(R["extreme"] - R["origin"])
        retr = abs(R["extreme"] - price) / rng if rng else 0
        deep = retr > 0.886
        out["movement"] = dict(phase="corrective", direction="down" if up else "up", retrace=round(retr, 3),
                               supports=not deep, reversal=deep,
                               text=(f"Corrective {'down' if up else 'up'} - {retr:.0%} pullback from the {R['ext_lab']} {f(R['extreme'])}. "
                                     + ("Very deep: close to the protected level - possible reversal, be careful."
                                        if deep else "Deep pullback (discount) - still a normal correction inside the trend."
                                        if retr >= 0.618 else "Healthy pullback - supports continuation.")))

    # ---- the snapshot is the source of truth: zone + plan prices are stored the first time they are judged
    snap = _freeze((symbol, tf_label, sid), lambda: dict(id=sid, first_seen=time.time(), bias=out["bias"],
                                                          event=out["event"], geo=None, tp=None, rule=None, probs={}))
    with _lock:
        if snap["geo"] is None and R["E"] and u >= R["cut"]:
            snap["geo"] = dict(
                origin=R["origin"], origin_lab=R["origin_lab"], cut_time=c.idx[R["cut"]],
                pois=[dict(type=p["type"], bot=p["bot"], top=p["top"], conf=p["conf"], n=p["n"], time=_t(c, p["bar"])) for p in R["pois"]],
                fvgs=[dict(bot=x["bot"], top=x["top"], time=_t(c, x["bar"])) for x in R["fvgs"]],
                plan=None if not R["plan"] else dict(type=R["plan"]["ob"]["type"], bot=R["plan"]["ob"]["bot"], top=R["plan"]["ob"]["top"],
                                                      entry=R["plan"]["entry"], sl=R["plan"]["sl"]))
        if snap["tp"] is None and R["extreme"] is not None and snap["geo"] is not None:
            snap["tp"] = dict(price=R["extreme"], label=R["ext_lab"], time=c.idx[R["ext_bar"]])
        if snap["rule"] is None and calib:
            snap["rule"] = dict(calib["rule"])
        geo, tpx = snap["geo"], snap["tp"]
        if geo and calib:                                   # measured once per zone, then frozen
            for p in geo["pois"]:
                k = (p["type"], p["bot"], p["top"])
                if snap["probs"].get(k, (None,))[0] is None:
                    snap["probs"][k] = zone_probability(calib, p["type"], p["n"])

    def bar_of(ts):                                         # index of a stored candle time in today's window
        return int(min(max(c.idx.searchsorted(ts), 0), u))

    if geo:
        out["key"]["origin"], out["key"]["origin_label"] = f(geo["origin"]), geo["origin_lab"]
        if tpx:
            out["key"]["extreme"], out["key"]["extreme_label"] = f(tpx["price"]), tpx["label"]
        after = bar_of(max(geo["cut_time"], tpx["time"]) if tpx else geo["cut_time"])
        if tpx:
            after = max(after, min(bar_of(tpx["time"]) + K, u))
        for p in geo["pois"]:
            st = _status(c, d, p["top"], p["bot"], after, u)
            pr, basis = snap["probs"].get((p["type"], p["bot"], p["top"]), (None, "history not loaded yet"))
            out["pois"].append(dict(type=p["type"], bot=f(p["bot"]), top=f(p["top"]), status=st, confluence=p["conf"],
                                    n=p["n"], probability=pr, probability_basis=basis, time=p["time"]))
        out["confirmations"] = [dict(type="Fair value gap", bot=f(x["bot"]), top=f(x["top"]), time=x["time"]) for x in geo["fvgs"]]
        if not geo["pois"]:
            out["note"] = "No valid zone in this leg (order blocks need displacement; every zone needs 2+ confluences)."
    else:
        out["note"] = f"The {E['kind']} just formed - zones are judged after {CONFIRM} candles ({max(R['cut'] - u, 1)} to go), then fixed."

    # ---- plan + TRADE / NO TRADE (prices from the snapshot; only the status follows the market)
    gp = geo and geo["plan"]
    if gp:
        spread = R["spread"]
        entry, sl = gp["entry"], gp["sl"]
        risk = abs(entry - sl)
        tp = tpx["price"] if tpx else None
        rr = abs(tp - entry) / risk if tp is not None and risk else None
        rr_net = (abs(tp - entry) - spread) / (risk + spread) if tp is not None and risk else None
        ob_now = next((p for p in out["pois"] if p["type"] == gp["type"]), None)
        status = "forming"
        if tp is not None:
            status = plan_status(c, d, entry, sl, tp, min(bar_of(tpx["time"]) + K, u) + 1, u)
        if ob_now and ob_now["status"] == "invalid" and status in ("forming", "waiting"):
            status = "invalid"
        if R["neutral"] and status in ("forming", "waiting"):
            status = "invalidated"
        prob = snap["probs"].get((gp["type"], gp["bot"], gp["top"]), (None, ""))[0]
        plan = dict(side="BUY LIMIT" if up else "SELL LIMIT", dir=d, entry=f(entry), stop_loss=f(sl), take_profit=f(tp),
                    rr=None if rr is None else round(rr, 2), rr_after_spread=None if rr_net is None else round(rr_net, 2),
                    zone=gp["type"], zone_bot=f(gp["bot"]), zone_top=f(gp["top"]), probability=prob,
                    sl_reason=f"{'below' if up else 'above'} the protected {geo['origin_lab']} {f(geo['origin'])} (+ buffer and spread)",
                    tp_reason=f"previous {tpx['label']} {f(tpx['price'])}" if tpx else None,
                    status=status, decision="NO TRADE", reasons=[])
        rule, why = snap.get("rule"), plan["reasons"]
        if tp is None:
            why.append(f"waiting for the {R['ext_lab']} to confirm - the target is not known yet")
        elif rr_net < MIN_RR:
            why.append(f"R:R after spread is only 1:{rr_net:.2f} - the structural target is too close (need 1:{MIN_RR:g})")
        if status == "invalidated":
            why.append(f"setup invalidated: price closed beyond the protected {geo['origin_lab']} {f(geo['origin'])}")
        if status in ("missed", "stopped", "tp hit", "invalid"):
            why.append({"missed": "price reached the target without filling - setup completed without us",
                        "stopped": "this setup already hit its stop loss", "tp hit": "this setup already reached its target",
                        "invalid": "price closed through the order block - zone broken"}[status])
        if ob_now and ob_now["status"] == "mitigated":
            why.append("the order block was already tapped twice - weak")
        if prob is None:
            why.append("reaction rate of this zone type not measured yet")
        elif prob < 50:
            why.append(f"zones like this reacted only {prob}% of the time on this timeframe")
        if not rule:
            why.append("rule history not loaded yet")
        elif rule["trades"] < MIN_RULE_TRADES:
            why.append(f"only {rule['trades']} past trades of this rule on this timeframe - not enough to trust it")
        elif rule["avg_r"] <= 0:
            why.append(f"this exact rule lost on this timeframe's history ({rule['trades']} trades, "
                       f"{rule['win_rate']:.0f}% won, {rule['avg_r']:+.2f}R per trade)")
        if not why and status == "waiting":
            plan["decision"] = "TRADE"
        elif not why and status == "filled":
            plan["decision"] = "IN TRADE"
        out["plan"] = plan
        with _lock:
            snap["plan_snapshot"] = {k: plan[k] for k in ("side", "entry", "stop_loss", "take_profit", "rr")}
    with _lock:
        snap["last_status"] = out["plan"]["status"] if out["plan"] else out["bias"].lower()
    out["previous"] = previous(symbol, tf_label, sid)
    return out
