"""
structure.py - market structure of ONE timeframe, with fixed price levels.

For every timeframe it reports:
  1. Bias        bullish / bearish from the last valid BOS / CHoCH, with the reason.
  2. Structure   the last BOS or CHoCH (exact price + time) and the swing points
                 HH / HL / LH / LL with exact prices.
  3. Movement    impulsive (still extending after the break) or corrective (pulling back
                 from the confirmed HH / LL), its direction and whether it supports the bias.
  4. POIs        order blocks, liquidity sweep, supply/demand base, with exact ranges,
                 the confluences behind them and an estimated reaction probability.
                 Fair value gaps are used only as confirmation.
  5. Plan        limit at the best order block, SL below the HL (buy) / above the LH (sell),
                 TP at the previous HH (buy) / LL (sell), exact prices and R:R.

FIXED LEVELS: every price above comes from candles that have already closed, anchored to the
last BOS/CHoCH (its swing points, its order blocks, and the ATR measured on the break candle).
Nothing is measured from the current price, so the levels cannot move while price moves.
They change only when a NEW BOS or CHoCH closes on this timeframe - that is a new structure,
and it gets a new analysis (with a new id) that must pass the same validity checks.

Swings are confirmed fractals (3 candles each side, as in signal_engine). A BOS/CHoCH needs a
candle CLOSE beyond the last confirmed swing - a wick alone is a liquidity sweep, not a break.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import signal_engine as se

K = se.SWING_K
STATS_FILE = Path(__file__).with_name("poi_stats.json")   # written by calibrate_poi.py (optional)
MIN_CONFLUENCE = 2          # a POI needs at least this many confluences to be shown
MIN_RR = 1.0                # plans below this R:R are shown as "not worth it"
CONFIRM = 4                 # candles after a BOS/CHoCH before its zones are judged (then fixed)


def _stats():
    try:
        return json.loads(STATS_FILE.read_text())
    except Exception:
        return None


def _t(c, j):
    return c.idx[j].isoformat()


def _local_atr(c, b, n=14):
    """Average true range of the n candles ending at b - depends only on those candles."""
    j0 = max(1, b - n + 1)
    tr = [max(c.h[j] - c.l[j], abs(c.h[j] - c.c[j - 1]), abs(c.l[j] - c.c[j - 1])) for j in range(j0, b + 1)]
    return float(np.mean(tr)) if tr else float(c.h[b] - c.l[b])


# --------------------------------------------------------------------------- swings + events
def swings(c):
    """Confirmed swing points labelled HH / LH / HL / LL (vs the previous swing of the same kind)."""
    pts = sorted([(j, "H", float(c.h[j])) for j in c.sh] + [(j, "L", float(c.l[j])) for j in c.sl])
    out, ph, pl = [], None, None
    for j, t, v in pts:
        if t == "H":
            lab = "H" if ph is None else ("HH" if v > ph else "LH")
            ph = v
        else:
            lab = "L" if pl is None else ("HL" if v > pl else "LL")
            pl = v
        out.append(dict(bar=j, kind=t, label=lab, price=v, time=_t(c, j)))
    return out


def events(c):
    """Every BOS / CHoCH: a candle CLOSE beyond the last confirmed swing high / low."""
    sh = [(j + K, j) for j in c.sh]
    sl = [(j + K, j) for j in c.sl]
    last_h = last_l = None
    ph = pl = 0
    trend, out = 0, []
    for b in range(len(c.c)):
        while ph < len(sh) and sh[ph][0] <= b:
            last_h, ph = sh[ph][1], ph + 1
        while pl < len(sl) and sl[pl][0] <= b:
            last_l, pl = sl[pl][1], pl + 1
        if last_h is not None and c.c[b] > c.h[last_h]:
            out.append(dict(bar=b, dir=1, kind="BOS" if trend == 1 else "CHoCH", level=float(c.h[last_h]),
                            swing_bar=last_h, time=_t(c, b)))
            trend, last_h = 1, None
        if last_l is not None and c.c[b] < c.l[last_l]:
            out.append(dict(bar=b, dir=-1, kind="BOS" if trend == -1 else "CHoCH", level=float(c.l[last_l]),
                            swing_bar=last_l, time=_t(c, b)))
            trend, last_l = -1, None
    if out and len(out) == 1:
        out[0]["kind"] = "BOS"          # first break in the window: no earlier trend to "change"
    return out


# --------------------------------------------------------------------------- POIs
def _fvgs(c, d, j_from, j_to, atr_b, upto=None):
    """Fair value gaps in the leg still unfilled at `upto` (confirmation only)."""
    upto = c.i if upto is None else upto
    out = []
    for j in range(max(j_from, 2), j_to + 1):
        if d == 1 and c.l[j] - c.h[j - 2] > 0.1 * atr_b:
            bot, top = float(c.h[j - 2]), float(c.l[j])
            if c.l[j + 1:upto + 1].min(initial=np.inf) > bot:
                out.append(dict(bot=bot, top=top, bar=j))
        if d == -1 and c.l[j - 2] - c.h[j] > 0.1 * atr_b:
            bot, top = float(c.h[j]), float(c.l[j - 2])
            if c.h[j + 1:upto + 1].max(initial=-np.inf) < top:
                out.append(dict(bot=bot, top=top, bar=j))
    return out


def _ob_candle(c, d, start, stop):
    """Last opposite-colour candle searching backwards from start to stop (inclusive)."""
    for j in range(start, stop - 1, -1):
        if (d == 1 and c.c[j] < c.o[j]) or (d == -1 and c.c[j] > c.o[j]):
            return j
    return None


def _status(c, d, top, bot, after):
    """fresh / tapped / mitigated / invalid, judged on candles after `after` (levels never change)."""
    seg_l, seg_h, seg_c = c.l[after + 1:c.i + 1], c.h[after + 1:c.i + 1], c.c[after + 1:c.i + 1]
    if not len(seg_c):
        return "fresh", 0
    if d == 1:
        if (seg_c < bot).any():
            return "invalid", 0
        touches = int(np.sum((seg_l <= top) & np.r_[True, seg_l[:-1] > top]))
    else:
        if (seg_c > top).any():
            return "invalid", 0
        touches = int(np.sum((seg_h >= bot) & np.r_[True, seg_h[:-1] < bot]))
    return ("fresh" if touches == 0 else "tapped" if touches == 1 else "mitigated"), touches


def _probability(n_conf, status, stats):
    """Estimated chance price reacts at the zone. Uses measured rates if calibrate_poi.py was run."""
    if stats:
        row = stats.get("by_confluence", {}).get(str(min(n_conf, 5)))
        if row and row.get("n", 0) >= 30:
            p = row["rate"] * (0.8 if status == "tapped" else 1.0)
            return int(round(100 * p)), f"measured on {row['n']} past zones ({stats.get('source', 'history')})"
    # Conservative until measured: on random price data zones 'react' (+1R before breaking) ~50% of the time,
    # so confluence can only add a little on top of that. Run calibrate_poi.py on real candles to replace this.
    p = 44 + 4 * n_conf - (8 if status == "tapped" else 0)
    return int(max(35, min(65, p))), "estimate from confluence - not measured yet"


# --------------------------------------------------------------------------- one timeframe
def analyse_tf(c, tf_label, symbol, htf_dir=0, htf_label=None):
    """Full structure read of one timeframe. c = signal_engine.Ctx on CLOSED candles."""
    digits = se.SYMBOLS.get(symbol, {}).get("digits", 5)
    spread = se.SYMBOLS.get(symbol, {}).get("spread", 0.0)
    r = lambda x: None if x is None else round(float(x), digits)
    i, price = c.i, float(c.c[c.i])
    sw = swings(c)
    ev = events(c)
    out = dict(tf=tf_label, bias="NEUTRAL", dir=0, swings=[dict(label=s["label"], price=r(s["price"]), time=s["time"])
                                                              for s in sw[-8:]],
               event=None, movement=None, pois=[], confirmations=[], plan=None, id=None)
    if not ev:
        out["reason"] = "No confirmed break of structure in view yet - ranging."
        return out
    E = ev[-1]
    d, b, j0 = E["dir"], E["bar"], E["swing_bar"]
    atr_b = _local_atr(c, b)                                  # fixed: ATR of the 14 candles up to the break
    up = d == 1
    out.update(dir=d, bias="BULLISH" if up else "BEARISH",
               event=dict(kind=E["kind"], dir="up" if up else "down", price=r(E["level"]), time=E["time"],
                          broke=f"{'swing high' if up else 'swing low'} of {c.idx[j0].strftime('%d %b %H:%M')}"),
               history=[dict(kind=x["kind"], dir="up" if x["dir"] == 1 else "down", price=r(x["level"]), time=x["time"])
                        for x in ev[-5:-1]])

    # ---- the leg: origin (protected HL / LH) -> break -> impulse extreme (HH / LL)
    seg = c.l[j0:b + 1] if up else c.h[j0:b + 1]
    o_bar = j0 + int(np.argmin(seg) if up else np.argmax(seg))
    origin = float(c.l[o_bar] if up else c.h[o_bar])
    prev_same = [s for s in sw if s["kind"] == ("L" if up else "H") and s["bar"] < o_bar]
    if prev_same:
        better = origin > prev_same[-1]["price"] if up else origin < prev_same[-1]["price"]
        origin_lab = ("HL" if better else "LL") if up else ("LH" if better else "HH")
    else:
        origin_lab = "HL" if up else "LH"
    ext_bars = [j for j in (c.sh if up else c.sl) if j >= b]
    ext_bar = ext_bars[0] if ext_bars else None
    extreme = float(c.h[ext_bar] if up else c.l[ext_bar]) if ext_bar is not None else None
    ext_lab = "HH" if up else "LL"

    # ---- bias reason
    lab_seq = " → ".join(f"{s['label']} {r(s['price'])}" for s in sw[-4:])
    out["reason"] = (f"{E['kind']} {'up' if up else 'down'}: a candle closed {'above' if up else 'below'} the "
                     f"{'swing high' if up else 'swing low'} {r(E['level'])} ({c.idx[b].strftime('%d %b %H:%M')}). "
                     f"Protected {origin_lab} {r(origin)} holds" + (f". Swings: {lab_seq}." if lab_seq else "."))
    out["key"] = dict(origin=r(origin), origin_label=origin_lab, origin_time=_t(c, o_bar),
                      extreme=r(extreme), extreme_label=ext_lab if extreme is not None else None,
                      extreme_time=_t(c, ext_bar) if ext_bar is not None else None)

    # ---- impulsive or corrective
    if ext_bar is None:
        run = float(c.h[b:i + 1].max() if up else c.l[b:i + 1].min())
        size = abs(run - origin) / atr_b if atr_b else 0
        out["movement"] = dict(phase="impulsive", direction="up" if up else "down", supports=htf_dir in (0, d),
                               text=f"Impulsive {'up' if up else 'down'} - still extending after the {E['kind']} "
                                    f"({size:.1f} ATR from the {origin_lab}). The {ext_lab} is not confirmed yet, "
                                    f"so the take profit is not set - a pullback is needed first.")
    else:
        rng = abs(extreme - origin)
        retr = abs(extreme - price) / rng if rng else 0
        zone = "discount" if retr >= 0.5 else "premium"
        out["movement"] = dict(phase="corrective", direction="down" if up else "up", retrace=round(retr, 3),
                               supports=htf_dir in (0, d),
                               text=f"Corrective {'down' if up else 'up'} - pulling back {retr:.0%} of the leg from the "
                                    f"{ext_lab} {r(extreme)} (price in {zone}). In a {out['bias'].lower()} structure "
                                    f"this is the pullback where entries form.")
    if htf_dir and htf_dir != d and out["movement"]:
        out["movement"]["text"] += f" Against the {htf_label or 'higher timeframe'} bias - counter-trend."
        out["movement"]["supports"] = False

    # ---- POIs (demand for bullish, supply for bearish) - judged ONCE, CONFIRM candles after the break
    stats = _stats()
    cut = b + CONFIRM                                            # fixed cut-off for this structure
    out["confirmations"] = []
    if i < cut:
        out["no_plan"] = (f"The {E['kind']} just formed - zones are judged after {CONFIRM} candles "
                          f"({cut - i} to go), then they stay fixed.")
        out["pois"], out["plan"] = [], None
        out["id"] = f"{tf_label}|{E['time']}"
        return out
    fv = _fvgs(c, d, o_bar, cut, atr_b, upto=cut)
    out["confirmations"] = [dict(type="Fair value gap", bot=r(x["bot"]), top=r(x["top"]), time=_t(c, x["bar"]))
                            for x in fv]
    leg_ext = float(c.h[o_bar:cut + 1].max() if up else c.l[o_bar:cut + 1].min())
    mid = (origin + leg_ext) / 2                                  # discount / premium line (fixed)
    cands = []
    j_ext = _ob_candle(c, d, o_bar, max(o_bar - 5, j0))
    if j_ext is None:
        j_ext = o_bar
    cands.append(("Order block (extreme)", j_ext,
                  float(min(c.l[j_ext:o_bar + 1].min(), origin)) if up else float(c.l[j_ext]),
                  float(c.h[j_ext]) if up else float(max(c.h[j_ext:o_bar + 1].max(), origin))))
    j_dec = _ob_candle(c, d, b - 1, o_bar + 1)
    if j_dec is not None and j_dec != j_ext:
        cands.append(("Order block (decisional)", j_dec, float(c.l[j_dec]), float(c.h[j_dec])))
    base_top = base_bot = None                     # supply / demand base: small candles at the origin
    lo_j, hi_j = max(o_bar - 2, j0), min(o_bar + 2, b)
    bodies = [(max(c.o[j], c.c[j]), min(c.o[j], c.c[j])) for j in range(lo_j, hi_j + 1) if abs(c.c[j] - c.o[j]) <= 0.6 * atr_b]
    if bodies:
        base_top = max(x[0] for x in bodies) if up else origin
        base_bot = origin if up else min(x[1] for x in bodies)
    swept = None                       # the origin raided an earlier swing low/high, then structure broke
    for s_ in reversed([x for x in sw if x["kind"] == ("L" if up else "H") and o_bar - 80 <= x["bar"] < o_bar]):
        took = (up and origin < s_["price"]) or (not up and origin > s_["price"])
        near = abs(s_["price"] - origin) <= 1.0 * atr_b
        back = (up and c.c[o_bar:o_bar + 3].max() > s_["price"]) or (not up and c.c[o_bar:o_bar + 3].min() < s_["price"])
        if took and near and back:
            swept = s_
            break
        if took:
            break
    if base_top is not None and base_top > base_bot:
        cands.append(("Demand zone (base)" if up else "Supply zone (base)", o_bar, float(base_bot), float(base_top)))
    if swept:
        lo_, hi_ = (origin, swept["price"]) if up else (swept["price"], origin)
        cands.append(("Liquidity sweep", o_bar, float(lo_), float(hi_)))

    pois = []
    for name, j, bot, top in cands:
        if top <= bot:
            continue
        nxt = [t for t in range(j + 1, min(j + 5, cut + 1)) if t >= 2]
        disp = any((up and c.l[t] - c.h[t - 2] > 0.1 * atr_b) or (not up and c.l[t - 2] - c.h[t] > 0.1 * atr_b) for t in nxt) \
            or abs(c.c[min(j + 3, cut)] - c.o[min(j + 1, cut)]) > 1.5 * atr_b
        if name.startswith("Order block") and not disp:
            continue                                            # an OB without displacement is not valid
        conf = [f"caused the {E['kind']}"]
        if disp:
            conf.append("displacement after it (imbalance)")
        if (up and top <= mid) or (not up and bot >= mid):
            conf.append("in discount" if up else "in premium")
        if swept and name != "Liquidity sweep" and bot <= max(swept["price"], origin) + 0.3 * atr_b \
                and top >= min(swept["price"], origin) - 0.3 * atr_b:
            conf.append(f"liquidity swept at {r(swept['price'])}")
        if base_top is not None and name.startswith("Order block") and bot <= base_top and top >= base_bot:
            conf.append("overlaps the " + ("demand" if up else "supply") + " base")
        if any(x["bot"] <= top + 0.3 * atr_b and x["top"] >= bot - 0.3 * atr_b for x in fv):
            conf.append("fair value gap confirms")
        n = len(conf) - 1                                        # 'caused the BOS/CHoCH' is the baseline
        if n < MIN_CONFLUENCE:
            continue
        st, touches = _status(c, d, top, bot, max(cut, ext_bar or cut))     # live status - never moves the levels
        n_live = n + (1 if htf_dir == d else 0)
        prob, how = _probability(n_live, st, stats)
        notes = ([f"{htf_label or 'higher timeframe'} agrees"] if htf_dir == d else
                 [f"against the {htf_label or 'higher timeframe'}"] if htf_dir == -d else [])
        pois.append(dict(type=name, bot=r(bot), top=r(top), status=st, confluence=conf[1:] + notes, n=n,
                         probability=prob, probability_basis=how, time=_t(c, j),
                         _bar=j, _bot=bot, _top=top, _score=(n, name == "Order block (extreme)")))
    pois.sort(key=lambda p: p["_score"], reverse=True)

    # ---- plan: limit at the best order block, SL beyond the protected HL/LH, TP at the HH/LL
    obs = [p for p in pois if p["type"].startswith("Order block")]
    plan = None
    if obs:
        ob = obs[0]                        # chosen by the fixed confluence ranking, never by live status
        entry = ob["_top"] if up else ob["_bot"]
        sl = origin - (0.1 * atr_b + spread) if up else origin + (0.1 * atr_b + spread)
        risk = abs(entry - sl)
        plan = dict(side="BUY LIMIT" if up else "SELL LIMIT", dir=d, entry=r(entry), stop_loss=r(sl),
                    sl_reason=f"{'below' if up else 'above'} the {origin_lab} {r(origin)} (+ buffer)",
                    zone=ob["type"], zone_bot=ob["bot"], zone_top=ob["top"], probability=ob["probability"],
                    take_profit=None, tp_reason=None, rr=None, status="forming", valid=False, notes=[])
        if extreme is None:
            plan["notes"].append(f"Take profit waits for the {ext_lab} to be confirmed (impulse still running).")
        else:
            tp = extreme
            rr = abs(tp - entry) / risk if risk else 0
            plan.update(take_profit=r(tp), tp_reason=f"previous {ext_lab} {r(extreme)}", rr=round(rr, 2),
                        valid=rr >= MIN_RR)
            if rr < MIN_RR:
                plan["notes"].append(f"R:R only 1:{rr:.2f} - not worth taking.")
            # status from candles after the HH/LL was confirmed (levels stay as they are)
            after = ext_bar + K
            st, filled = "waiting", False
            for t in range(after + 1, i + 1):
                lo_, hi_ = c.l[t], c.h[t]
                if not filled:
                    if (up and lo_ <= entry) or (not up and hi_ >= entry):
                        filled, st = True, "filled"
                        if (up and lo_ <= sl) or (not up and hi_ >= sl):
                            st = "stopped"
                            break
                    elif (up and hi_ >= tp) or (not up and lo_ <= tp):
                        st = "missed"
                        break
                else:
                    if (up and lo_ <= sl) or (not up and hi_ >= sl):
                        st = "stopped"
                        break
                    if (up and hi_ >= tp) or (not up and lo_ <= tp):
                        st = "tp hit"
                        break
            plan["status"] = st
            if st in ("missed", "stopped", "tp hit"):
                plan["valid"] = False
                plan["notes"].append({"missed": "Price reached the target without filling - wait for the next structure.",
                                      "stopped": "This plan already hit its stop.",
                                      "tp hit": "This plan already reached its target."}[st])
        if ob["status"] == "invalid":
            plan["valid"] = False
            plan["notes"].append("Price closed through this order block - the zone is broken. Wait for the next BOS/CHoCH.")
        if ob["status"] == "mitigated":
            plan["valid"] = False
            plan["notes"].append("The order block was already tapped twice - weak, skipped.")
        if htf_dir and htf_dir != d:
            plan["valid"] = False
            plan["notes"].append(f"Counter to the {htf_label or 'higher timeframe'} bias - not taken.")
    else:
        out["no_plan"] = "No valid order block in this leg (needs displacement, an unbroken zone and 2+ confluences)."
    for p in pois:
        for k in ("_bar", "_bot", "_top", "_score"):
            p.pop(k, None)
    out["pois"] = pois
    out["plan"] = plan
    out["id"] = f"{tf_label}|{E['time']}"                     # changes only with a new BOS / CHoCH
    return out
