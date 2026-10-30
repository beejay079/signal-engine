"""
topdown.py - top-down market reading, entries and trade tracking
================================================================

Reads four charts top-down (example for a 1m entry):

  4H + 1H  BIAS   structure (HH/HL or LH/LL, BOS/CHoCH), EMA50/200, where the
                  current leg started (protected low/high), how deep the
                  pullback is, and the liquidity it is drawn to (target).
  15M      SETUP  is this an impulse, a pullback or a range; where are the
                  points of interest (order block, FVG, S/R, previous day
                  level, Fib 50-61.8%, EMA50).
  1M       ENTRY  fresh trigger at the zone (CHoCH, liquidity sweep,
                  engulfing / pin bar, breakout-retest) that is still holding.

Gives two orders:
  MARKET  only when the pullback has finished at a zone and the trigger fired
          (grade A / A+), and price has not run away (no chasing).
  LIMIT   at the best zone in the pullback, stop beyond the zone, target =
          the liquidity the market is drawn to.

Every trade it gives can be sent back (trades=[...]) and is tracked candle by
candle: limit filled / cancelled, TP1 -> half off and stop to entry, stop
trailed under new structure, or closed early if the structure turns against
it - always with the reason.

The same functions run the backtest in backtest.py, so what was tested is
what you trade.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import signal_engine as se
import structure as stx

# entry timeframe -> (top bias TF, mid bias TF, setup TF)
STACKS = {
    "1min":  ("4h", "1h", "15min"),
    "5min":  ("4h", "1h", "15min"),
    "15min": ("1day", "4h", "1h"),
    "1h":    ("1week", "1day", "4h"),
}
SIZES = {"entry": 500, "setup": 300, "mid": 300, "top": 300}
LABEL = {"1min": "1M", "5min": "5M", "15min": "15M", "1h": "1H", "4h": "4H", "1day": "D1", "1week": "W1"}
FRESH_BARS = {"1min": 6, "5min": 4, "15min": 3, "1h": 3}
GRADES = ((75, "A+"), (65, "A"), (55, "B"), (0, "C"))

# What the backtest on real gold candles showed (see BACKTEST.md). Shown on the order cards.
TESTED = {
    "limit": {"5min": "Tested on real gold (2018): 126 trades, 44% won at 1:1, average -0.24R after spread.",
              "15min": "Tested on real gold (2018): 38 trades, 47% won at 1:1, average -0.17R after spread.",
              "1min": "Not tested on 1M. On 5M this order lost (-0.24R per trade after spread).",
              "1h": "Not tested on 1H (not enough data)."},
    "market": {"5min": "Tested on real gold (2018): about 27% won, average -0.46R after spread.",
               "15min": "Tested on real gold (2018): 13 trades, 38% won, average -0.15R after spread.",
               "1min": "Not tested on 1M. On 5M market entries lost (-0.46R per trade).",
               "1h": "Not tested on 1H (not enough data)."},
    "structure": "The order-block rule (entry at the OB, stop beyond the HL/LH, target the HH/LL) has not been "
                 "backtested on real gold yet. Treat it as untested.",
    "summary": "No proven edge yet: in testing these setups were close to a coin flip, and the spread made them lose. "
               "Use a demo account and your journal before risking money.",
}

# Tunable rules.
DEFAULTS = dict(
    trade_grades=("A+", "A"),
    tp1_r=2.0,            # TP1 at this many R (half closed there)
    tp2_cap_r=4.0,        # TP2 = target liquidity, capped here
    min_room_r=2.0,       # need this much room before the first strong level
    sl_buf_e=0.3,         # stop buffer beyond structure, in entry-TF ATR (+ spread)
    sl_min_e=0.8,         # stop at least this far (entry-TF ATR) - avoids noise stops
    sl_min_spread=5,      # ...and at least 5x the spread (tight stops paid too much spread in testing)
    sl_max_s=1.2,         # stop at most this far (setup-TF ATR) - keeps it tight
    be_at_r=None,         # move stop to entry at +X R before TP1 (None = only after TP1)
    trail=True,           # after TP1 trail the stop under new entry-TF swings
    early_exit=True,      # close early if entry TF breaks structure against the trade before TP1
    flip_exit=True,       # close if the setup TF structure flips against the trade
    min_pullback=0.30,    # trend pullback: setup leg must have retraced at least this much
    limit_on=True,
    # ---- LIMIT ORDER: the rules that held up in BOTH test periods (gold 2018, 15M entry) ----
    lim_min_dist_s=0.2, lim_max_dist_s=3.0,   # zone distance from price (setup ATR)
    lim_need_pullback=True,  # only while the setup TF is actually pulling back
    lim_buf_e=0.3, lim_buf_s=0.3,             # stop beyond the zone: 0.3 entry ATR + 0.3 setup ATR + spread
    lim_min_spread=8,        # stop at least 8x the spread (tight stops lost to noise + spread)
    lim_max_s=3.0,           # stop at most 3 setup ATR
    lim_tp_r=1.0,            # take profit at 1R, whole position (61-67% win rate in testing)
    lim_full_close=True,
    lim_min_room=1.0,        # need room to the target liquidity
    lim_expiry_min=120,      # cancel if not filled in 2 hours (x2 on 1h entries)
    limit_rule="structure",  # "structure": OB entry, SL beyond HL/LH, TP at HH/LL (fixed until next BOS/CHoCH)
                             # "zone": the older tested zone rule (used by backtest.py)
    playbooks=None,       # None = all; or a tuple of names
)

MAX_EXT_EMA, MAX_RUN, MAX_STREAK, RSI_CHASE, MAX_FROM_POI = 1.8, 1.5, 6, (75, 25), 1.0


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def fmt(x, digits):
    return "-" if x is None else f"{x:.{digits}f}"


def side_word(d):
    return {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[int(d)]


def grade_of(score):
    return next(g for lim, g in GRADES if score >= lim)


def iso(ts):
    return pd.Timestamp(ts).tz_convert("UTC").isoformat() if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).tz_localize("UTC").isoformat()


def ago_text(minutes):
    if minutes < 90:
        return f"{int(minutes)} min ago"
    if minutes < 60 * 36:
        return f"{minutes / 60:.0f}h ago"
    return f"{minutes / 1440:.0f} days ago"


# --------------------------------------------------------------------------
# market structure of one timeframe
# --------------------------------------------------------------------------
def tf_bias(c):
    i = c.i
    seq = 0
    if len(c.sh) >= 2 and len(c.sl) >= 2:
        hh, hl = c.h[c.sh[-1]] > c.h[c.sh[-2]], c.l[c.sl[-1]] > c.l[c.sl[-2]]
        seq = 1 if hh and hl else -1 if (not hh and not hl) else 0
    ema_d = 1 if c.c[i] > c.ema50[i] > c.ema200[i] else -1 if c.c[i] < c.ema50[i] < c.ema200[i] else 0
    score = 1.5 * c.structure + seq + ema_d
    d = 1 if score >= 2 else -1 if score <= -2 else 0
    return dict(dir=d, score=score, strength="strong" if abs(score) >= 3 else "moderate" if d else "weak",
                seq=seq, ema=ema_d)


def read_structure(c):
    """Trend, phase (impulse / pullback / broken / range), protected level, leg, target."""
    i, a, price = c.i, c.atr_now, c.c[c.i]
    swings = sorted([(j, "H", c.h[j]) for j in c.sh] + [(j, "L", c.l[j]) for j in c.sl])
    labels, lh, ll = [], None, None
    for j, t, v in swings:
        if t == "H":
            labels.append((j, "H" if lh is None else "HH" if v > lh else "LH", float(v)))
            lh = v
        else:
            labels.append((j, "L" if ll is None else "HL" if v > ll else "LL", float(v)))
            ll = v
    out = dict(trend=int(c.structure), phase="range", protected=None, leg_lo=None, leg_hi=None, retrace=None,
               target=None, break_kind=None, break_level=None, break_bar=None, labels=[x[1] for x in labels[-4:]],
               origin_bar=None)
    if not c.last_break or c.structure == 0:
        return out
    b, kind, lvl = c.last_break
    out.update(break_kind=kind, break_level=float(lvl), break_bar=int(b))
    if c.structure == 1:
        src = [j for j in c.sh if j < b and abs(c.h[j] - lvl) < 1e-9]
        j0 = src[-1] if src else max(0, b - 20)
        seg = c.l[j0:b + 1]
        prot, ob = float(seg.min()), j0 + int(seg.argmin())
        hi = float(c.h[b:i + 1].max())
        rng = hi - prot
        retr = (hi - price) / rng if rng > 0 else 0.0
        phase = "broken" if price < prot else "impulse" if retr <= 0.2 else "pullback"
        above = [x for x in list(c.resistances) + [c.prev_day_high] if x and x > max(price, hi) + 0.1 * a]
        target = hi if phase in ("pullback", "broken") else (min(above) if above else None)
        out.update(protected=prot, leg_lo=prot, leg_hi=hi, retrace=retr, phase=phase, target=target, origin_bar=ob)
    else:
        src = [j for j in c.sl if j < b and abs(c.l[j] - lvl) < 1e-9]
        j0 = src[-1] if src else max(0, b - 20)
        seg = c.h[j0:b + 1]
        prot, ob = float(seg.max()), j0 + int(seg.argmax())
        lo = float(c.l[b:i + 1].min())
        rng = prot - lo
        retr = (price - lo) / rng if rng > 0 else 0.0
        phase = "broken" if price > prot else "impulse" if retr <= 0.2 else "pullback"
        below = [x for x in list(c.supports) + [c.prev_day_low] if x and x < min(price, lo) - 0.1 * a]
        target = lo if phase in ("pullback", "broken") else (max(below) if below else None)
        out.update(protected=prot, leg_lo=lo, leg_hi=prot, retrace=retr, phase=phase, target=target, origin_bar=ob)
    return out


def combine_bias(top, mid, tl, ml):
    t, m = top["dir"], mid["dir"]
    if t and t == m:
        return t, "aligned", 25, f"{tl} and {ml} both {side_word(t).lower()}"
    if t and not m:
        return t, "top_only", 18, f"{tl} {side_word(t).lower()}, {ml} consolidating"
    if m and not t:
        return m, "mid_only", 10, f"{tl} neutral, {ml} {side_word(m).lower()}"
    if t and m == -t:
        return t, "htf_pullback", 14, f"{tl} {side_word(t).lower()}, {ml} pulling back against it"
    return 0, "none", 8, f"{tl} and {ml} have no clear direction"


def zones(c, d, tf):
    a, i, out = c.atr_now, c.i, []
    lab = LABEL.get(tf, tf)

    def add(name, top, bot):
        if top > bot:
            out.append(dict(name=f"{lab} {name}", top=float(top), bot=float(bot), tf=tf))

    if d == 1:
        if c.bull_ob and i - c.bull_ob[2] <= 150:
            add("order block", c.bull_ob[1], c.bull_ob[0])
        found = 0
        for j in range(i, max(i - 80, 2), -1):
            gb, gt = c.h[j - 2], c.l[j]
            if gt - gb > 0.1 * a and (j == i or c.l[j + 1:i + 1].min() > gb):
                add("fair value gap", gt, gb)
                found += 1
                if found == 2:
                    break
        for s_ in c.supports:
            add("support", s_ + 0.15 * a, s_ - 0.15 * a)
        if c.prev_day_low:
            add("previous-day low", c.prev_day_low + 0.15 * a, c.prev_day_low - 0.15 * a)
        if c.sh and c.sl and c.sh[-1] > c.sl[-1]:
            H, L = c.h[c.sh[-1]], c.l[c.sl[-1]]
            add("Fib 50-61.8%", H - 0.5 * (H - L), H - 0.618 * (H - L))
        if c.ema50[i] > c.ema200[i]:
            add("EMA50", c.ema50[i] + 0.2 * a, c.ema50[i] - 0.2 * a)
    else:
        if c.bear_ob and i - c.bear_ob[2] <= 150:
            add("order block", c.bear_ob[1], c.bear_ob[0])
        found = 0
        for j in range(i, max(i - 80, 2), -1):
            gb, gt = c.h[j], c.l[j - 2]
            if gt - gb > 0.1 * a and (j == i or c.h[j + 1:i + 1].max() < gt):
                add("fair value gap", gt, gb)
                found += 1
                if found == 2:
                    break
        for r in c.resistances:
            add("resistance", r + 0.15 * a, r - 0.15 * a)
        if c.prev_day_high:
            add("previous-day high", c.prev_day_high + 0.15 * a, c.prev_day_high - 0.15 * a)
        if c.sh and c.sl and c.sl[-1] > c.sh[-1]:
            H, L = c.h[c.sh[-1]], c.l[c.sl[-1]]
            add("Fib 50-61.8%", L + 0.618 * (H - L), L + 0.5 * (H - L))
        if c.ema50[i] < c.ema200[i]:
            add("EMA50", c.ema50[i] + 0.2 * a, c.ema50[i] - 0.2 * a)
    return out


def strategy_votes(c, regime):
    weights = se.REGIME_WEIGHTS.get(regime) or {"trend": 1, "breakout": 1, "price_action": 1, "reversion": 1}
    out = []
    for name, fam, w, trig, fn in se.STRATEGIES:
        try:
            v, why = fn(c)
        except Exception:
            v, why = 0, "n/a"
        eff = w * weights.get(fam, 0.0)
        out.append(dict(strategy=name, family=fam, vote=int(v), reason=why, weight=round(eff, 2), active=eff > 0))
    return out


def agreement(setup_votes, entry_votes, d):
    agree = against = 0.0
    names = []
    for votes, k in ((setup_votes, 0.6), (entry_votes, 0.4)):
        for v in votes:
            if not v["active"] or v["vote"] == 0:
                continue
            if v["vote"] == d:
                agree += v["weight"] * k
                names.append(v["strategy"])
            else:
                against += v["weight"] * k
    tot = agree + against
    return (agree / tot if tot else 0.0), sorted(set(names))


def candle_signal(c, j, d):
    o, h, l, cl = c.o[j], c.h[j], c.l[j], c.c[j]
    rng, body = h - l, abs(cl - o)
    if rng <= 0:
        return None
    po, pc = c.o[j - 1], c.c[j - 1]
    if d == 1:
        if min(o, cl) - l >= 2 * body and min(o, cl) - l >= 0.6 * rng:
            return "bullish pin bar"
        if pc < po and cl > o and cl >= po and o <= pc:
            return "bullish engulfing"
    else:
        if h - max(o, cl) >= 2 * body and h - max(o, cl) >= 0.6 * rng:
            return "bearish pin bar"
        if pc > po and cl < o and cl <= po and o >= pc:
            return "bearish engulfing"
    return None


def find_triggers(e, d, fresh, poi=None, key_levels=()):
    i, out, k = e.i, [], se.SWING_K
    if e.last_break and e.structure == d and i - e.last_break[0] < fresh:
        b, kind, lvl = e.last_break
        out.append(dict(name=f"{kind} ({'bullish' if d == 1 else 'bearish'} break of structure)",
                        quality=15 if kind == "CHoCH" else 12, bar=b, level=float(lvl), fail=float(lvl), kind="structure"))
    for j in range(i, max(i - fresh, k + 2), -1):
        if d == 1:
            lv = [e.l[s_] for s_ in e.sl if j - 60 <= s_ and s_ + k < j] + [x for x in key_levels if x]
            hit = [x for x in lv if e.l[j] < x < e.c[j]]
            if hit and e.c[j] > e.o[j]:
                lvl = max(hit)
                out.append(dict(name="liquidity sweep + reclaim", quality=14, bar=j, level=float(lvl), fail=float(lvl),
                                extreme=float(e.l[j]), kind="sweep"))
                break
        else:
            lv = [e.h[s_] for s_ in e.sh if j - 60 <= s_ and s_ + k < j] + [x for x in key_levels if x]
            hit = [x for x in lv if e.h[j] > x > e.c[j]]
            if hit and e.c[j] < e.o[j]:
                lvl = min(hit)
                out.append(dict(name="liquidity sweep + reclaim", quality=14, bar=j, level=float(lvl), fail=float(lvl),
                                extreme=float(e.h[j]), kind="sweep"))
                break
    for j in range(i, max(i - fresh, 1), -1):
        pat = candle_signal(e, j, d)
        if not pat:
            continue
        if poi and not (poi["bot"] - 0.3 * e.atr_now <= (e.l[j] if d == 1 else e.h[j]) <= poi["top"] + 0.3 * e.atr_now):
            continue
        out.append(dict(name=pat, quality=10, bar=j, level=float(e.c[j]), fail=float(e.l[j] if d == 1 else e.h[j]), kind="candle"))
        break
    try:
        if se.s_breakout_retest(e)[0] == d:
            out.append(dict(name="breakout & retest", quality=12, bar=i, level=float(e.c[i]),
                            fail=float(e.l[i - 2:i + 1].min() if d == 1 else e.h[i - 2:i + 1].max()), kind="retest"))
    except Exception:
        pass
    out.sort(key=lambda t: (t["quality"], t["bar"]), reverse=True)
    return out


def chase_check(e, d, price, trig_level):
    i, a, bad = e.i, e.atr_now, []
    ext = d * (price - e.ema20[i]) / a
    if ext > MAX_EXT_EMA:
        bad.append(f"price is {ext:.1f} ATR from the EMA20 - move already stretched")
    if trig_level is not None:
        run = d * (price - trig_level) / a
        if run > MAX_RUN:
            bad.append(f"price already ran {run:.1f} ATR past the trigger")
    streak = 0
    for j in range(i, max(i - 12, 0), -1):
        if (e.c[j] - e.o[j]) * d > 0:
            streak += 1
        else:
            break
    if streak >= MAX_STREAK:
        bad.append(f"{streak} candles in a row already went this way")
    r = e.rsi14[i]
    if (d == 1 and r > RSI_CHASE[0]) or (d == -1 and r < RSI_CHASE[1]):
        bad.append(f"RSI {r:.0f} is already extreme")
    return bad, ext


def session_score(symbol, now):
    if not se.SYMBOLS.get(symbol, {}).get("fx_session"):
        return 7, "24/7 market"
    h = now.hour
    if 12 <= h < 16:
        return 10, "London/New York overlap"
    if 7 <= h < 12:
        return 8, "London session"
    if 16 <= h < 20:
        return 6, "New York session"
    return -5, "Asian / off-hours (thin liquidity)"


# --------------------------------------------------------------------------
# VIEW - everything from the higher timeframes (reused until they change)
# --------------------------------------------------------------------------
class View:
    def __init__(self, symbol, entry_tf, t, m, s):
        top_tf, mid_tf, setup_tf = STACKS[entry_tf]
        self.symbol, self.entry_tf, self.tfs = symbol, entry_tf, (top_tf, mid_tf, setup_tf)
        self.L = {"top": LABEL[top_tf], "mid": LABEL[mid_tf], "setup": LABEL[setup_tf], "entry": LABEL[entry_tf]}
        self.t, self.m, self.s = t, m, s
        self.digits = se.SYMBOLS.get(symbol, {}).get("digits", 5)
        self.spread = se.SYMBOLS.get(symbol, {}).get("spread", 0.0)
        self.top_b, self.mid_b = tf_bias(t), tf_bias(m)
        self.bias, self.bias_kind, self.bias_pts, self.bias_txt = combine_bias(self.top_b, self.mid_b, self.L["top"], self.L["mid"])
        self.st = dict(top=read_structure(t), mid=read_structure(m), setup=read_structure(s))
        self.s_regime, self.s_regime_why = se.detect_regime(s)
        self.s_votes = strategy_votes(s, self.s_regime)
        self.zones = {d: zones(s, d, setup_tf) + zones(m, d, mid_tf) for d in (1, -1)}
        self.key_lv = {1: [s.prev_day_low, m.prev_day_low] + [s.l[j] for j in s.sl[-4:]] + list(m.supports),
                       -1: [s.prev_day_high, m.prev_day_high] + [s.h[j] for j in s.sh[-4:]] + list(m.resistances)}
        self.rh, self.rl = float(m.h[-60:].max()), float(m.l[-60:].min())

    def obstacles(self, d, price, min_gap):
        s, m, t = self.s, self.m, self.t
        if d == 1:
            obs = list(s.resistances) + list(m.resistances) + list(t.resistances)
            obs += [x for x in (s.prev_day_high, m.prev_day_high) if x]
            if s.bear_ob:
                obs.append(s.bear_ob[0])
            obs = [x for x in obs if x > price + min_gap]
            return min(obs) if obs else None
        obs = list(s.supports) + list(m.supports) + list(t.supports)
        obs += [x for x in (s.prev_day_low, m.prev_day_low) if x]
        if s.bull_ob:
            obs.append(s.bull_ob[1])
        obs = [x for x in obs if x < price - min_gap]
        return max(obs) if obs else None


# --------------------------------------------------------------------------
# SL / TP
# --------------------------------------------------------------------------
def build_levels(view, d, entry, anchor, atr_e, P, target=None):
    """Stop beyond the structural anchor, TP1 = tp1_r R, TP2 = target liquidity (capped)."""
    s = view.s
    buf = max(P["sl_buf_e"] * atr_e, 0.05 * s.atr_now) + view.spread
    dist = max((entry - (anchor - d * buf)) * d, P["sl_min_e"] * atr_e, 0.15 * s.atr_now, P["sl_min_spread"] * view.spread)
    sl = entry - d * dist
    problems = []
    if dist > P["sl_max_s"] * s.atr_now:
        problems.append(f"stop would be {fmt(dist, view.digits)} wide - too far from structure")
    nearest = view.obstacles(d, entry, 0.2 * atr_e)
    if target is not None and (target - entry) * d > 0:
        nearest = target if nearest is None else (min(nearest, target) if d == 1 else max(nearest, target))
    room = abs(nearest - entry) / dist if nearest is not None else 9.9
    if room < P["min_room_r"]:
        problems.append(f"only 1:{room:.1f} room before {fmt(nearest, view.digits)}")
    tp1 = entry + d * P["tp1_r"] * dist
    tgt = view.st["setup"]["target"]
    if tgt is None or (tgt - entry) * d <= 0:
        tgt = nearest
    tp2 = entry + d * 3 * dist if tgt is None else tgt - d * 0.1 * atr_e
    cap = entry + d * P["tp2_cap_r"] * dist
    tp2 = min(tp2, cap) if d == 1 else max(tp2, cap)
    tp2 = max(tp2, tp1) if d == 1 else min(tp2, tp1)
    return dict(entry=float(entry), sl=float(sl), tp1=float(tp1), tp2=float(tp2), dist=float(dist), room=float(room),
                nearest=nearest, problems=problems)


# --------------------------------------------------------------------------
# MARKET STORY - where it came from, what it is doing, where it is going
# --------------------------------------------------------------------------
def market_story(view, price, now):
    L, dg, st = view.L, view.digits, view.st
    f = lambda x: fmt(x, dg)
    T, M, S = st["top"], st["mid"], st["setup"]

    def phase_txt(x, lab):
        if x["trend"] == 0 or x["phase"] == "range":
            return f"{lab} has no clear structure (ranging)"
        up = x["trend"] == 1
        word = "uptrend" if up else "downtrend"
        if x["phase"] == "impulse":
            return f"{lab} {word}, moving in an impulse near its {'high' if up else 'low'} ({f(x['leg_hi'] if up else x['leg_lo'])})"
        if x["phase"] == "pullback":
            return f"{lab} {word}, pulling back {x['retrace']:.0%} of the last leg ({'discount' if x['retrace'] >= 0.5 else 'shallow'})"
        return f"{lab} {word} but price is beyond its protected {'low' if up else 'high'} {f(x['protected'])} - structure breaking"

    def origin(x, c, lab):
        if x["origin_bar"] is None:
            return None
        mins = (now - c.idx[x["origin_bar"]].to_pydatetime()).total_seconds() / 60
        up = x["trend"] == 1
        return (f"The {lab} {'up' if up else 'down'}-move started at {f(x['protected'])} ({ago_text(mins)}) "
                f"and broke {'above' if up else 'below'} {f(x['break_level'])} ({x['break_kind']}).")

    lines = [phase_txt(T, L["top"]) + ".", phase_txt(M, L["mid"]) + ".", phase_txt(S, L["setup"]) + "."]
    came = origin(M, view.m, L["mid"]) or origin(T, view.t, L["top"])
    d = view.bias
    if d:
        word = "bullish" if d == 1 else "bearish"
        x = M if M["trend"] == d else T
        inval = x["protected"]
        tgt = S["target"] if S["trend"] == d and S["target"] else x["target"] or T["target"]
        tgt_txt = f(tgt) if tgt else ("new highs" if d == 1 else "new lows")
        poi = None
        zs = [z for z in view.zones[d] if (d == 1 and z["top"] < price) or (d == -1 and z["bot"] > price)]
        if zs:
            z = max(zs, key=lambda z: z["top"]) if d == 1 else min(zs, key=lambda z: z["bot"])
            poi = z
        if S["trend"] == d and S["phase"] == "impulse":
            doing = f"{L['setup']} is in an impulse {'up' if d == 1 else 'down'} - not a place to enter, a pullback is due."
            nxt = (f"Wait for a pullback into {f(poi['bot'])}-{f(poi['top'])} ({poi['name']}), then continuation "
                   f"towards {tgt_txt}." if poi else f"Wait for a pullback before joining towards {tgt_txt}.")
        elif S["trend"] == d and S["phase"] == "pullback":
            doing = f"{L['setup']} is pulling back inside the {word} trend ({S['retrace']:.0%} of the leg)."
            nxt = (f"Pullback likely ends around {f(poi['bot'])}-{f(poi['top'])} ({poi['name']}), then continuation to "
                   f"{tgt_txt} (liquidity at the leg {'high' if d == 1 else 'low'})." if poi
                   else f"Look for the pullback to end and price to continue to {tgt_txt}.")
        elif S["trend"] == -d:
            doing = (f"{L['setup']} is moving against the {word} bias - this is a pullback on the higher timeframe "
                     f"unless {f(inval)} breaks.")
            nxt = (f"Wait for {L['setup']} to turn {word} again (a CHoCH), ideally from {f(poi['bot'])}-{f(poi['top'])} "
                   f"({poi['name']})." if poi else f"Wait for {L['setup']} to turn {word} again (a CHoCH).")
        else:
            doing = f"{L['setup']} is unclear while the higher timeframes are {word}."
            nxt = f"Wait for {L['setup']} structure to form in the {word} direction."
        inv = f"The {word} view is wrong if price {'closes below' if d == 1 else 'closes above'} {f(inval)} on the {L['mid'] if x is M else L['top']}." if inval else None
        alt = (f"If that breaks, expect a deeper move towards {f(view.obstacles(-d, price, 0) )}." if inval and view.obstacles(-d, price, 0) else None)
        headline = f"{'Bullish' if d == 1 else 'Bearish'} - {('pullback in progress' if (S['trend'] == -d or S['phase'] == 'pullback') else 'impulse, wait for pullback' if S['phase'] == 'impulse' else 'trend')}"
    else:
        doing = f"Higher timeframes disagree or are ranging ({view.bias_txt})."
        nxt = f"Range between {f(view.rl)} and {f(view.rh)} on the {L['mid']} - only fade the edges, or wait for a breakout."
        inv, alt, headline = None, None, "No clear direction"
    return dict(headline=headline, structure=lines, came_from=came, doing=doing, next=nxt,
                invalidation=inv, alternative=alt)


# --------------------------------------------------------------------------
# MARKET ENTRY - playbooks scored on the entry chart
# --------------------------------------------------------------------------
def market_setup(view, e, price, lo_win, hi_win, now, P):
    s, m, t, L, dg = view.s, view.m, view.t, view.L, view.digits
    f = lambda x: fmt(x, dg)
    e_regime, e_regime_why = se.detect_regime(e)
    e_votes = strategy_votes(e, e_regime)
    sess_pts, sess_txt = session_score(view.symbol, now)
    fresh = FRESH_BARS[view.entry_tf]
    pd_pos = (price - view.rl) / (view.rh - view.rl) if view.rh > view.rl else 0.5
    bias_d, bias_kind, bias_pts = view.bias, view.bias_kind, view.bias_pts
    S = view.st["setup"]
    hard = []
    if view.s_regime == "VOLATILE" or e_regime == "VOLATILE":
        hard.append(f"volatility spike on {L['setup'] if view.s_regime == 'VOLATILE' else L['entry']} - likely news, stand aside")

    def evaluate(name, d, kind_pts, poi_list, anchor, need_kinds=None, target=None, extra=()):
        checks, fails = [], []

        def chk(label, ok, detail, hard_fail=True):
            checks.append(dict(item=label, ok=bool(ok), detail=detail))
            if not ok and hard_fail:
                fails.append(f"{label}: {detail}")

        chk(f"Top-down bias ({L['top']} → {L['mid']})", bias_d in (d, 0), view.bias_txt)
        for lab, ok, det in extra:
            chk(lab, ok, det)
        poi = None
        if poi_list:
            names = sorted({p["name"] for p in poi_list})
            poi = dict(top=max(p["top"] for p in poi_list), bot=min(p["bot"] for p in poi_list), names=names)
            chk("Location (point of interest)", True, " + ".join(names))
        else:
            chk("Location (point of interest)", False, "price is not at a key zone")
        trig = find_triggers(e, d, fresh, poi, view.key_lv[d])
        if need_kinds:
            trig = [x for x in trig if x["kind"] in need_kinds]
        holding = [x for x in trig if (price - x["fail"]) * d > 0]
        tg = holding[0] if holding else None
        if tg:
            ago = e.i - tg["bar"]
            chk(f"{L['entry']} entry trigger", True, f"{tg['name']}, {ago} candle{'s' if ago != 1 else ''} ago, holding")
        elif trig:
            chk(f"{L['entry']} entry trigger", False, f"{trig[0]['name']} formed but price is back {'below' if d == 1 else 'above'} {f(trig[0]['fail'])} - it failed")
        else:
            chk(f"{L['entry']} entry trigger", False, f"no fresh {L['entry']} trigger yet (last {fresh} candles)")
        bad, ext = chase_check(e, d, price, tg["level"] if tg else None)
        chk("Not chasing", not bad, "; ".join(bad) if bad else f"{ext:+.1f} ATR from {L['entry']} EMA20 - fresh")
        agr, agr_names = agreement(view.s_votes, e_votes, d)
        chk("Strategy confluence", agr >= 0.5, f"{agr:.0%} of weighted strategy votes agree ({len(agr_names)} strategies)")
        anc = anchor
        if tg and tg.get("extreme") is not None:
            anc = min(anc, tg["extreme"]) if d == 1 else max(anc, tg["extreme"])
        lv = build_levels(view, d, price, anc, e.atr_now, P, target)
        chk("Stop & room", not lv["problems"], "; ".join(lv["problems"]) if lv["problems"] else
            f"stop {f(lv['dist'])} beyond structure, 1:{min(lv['room'], 9.9):.1f} room")
        chk("Session", sess_pts > 0, sess_txt, hard_fail=False)
        right = (d == 1 and pd_pos <= 0.5) or (d == -1 and pd_pos >= 0.5)
        chk(f"{L['mid']} premium/discount", right, f"price at {pd_pos:.0%} of the {L['mid']} range ({'discount' if pd_pos < 0.5 else 'premium'})", hard_fail=False)
        n_poi = len(poi["names"]) if poi else 0
        score = (kind_pts + (0 if not n_poi else 8 if n_poi == 1 else 14 if n_poi == 2 else 20)
                 + (tg["quality"] if tg else 0) + (15 * agr if agr >= 0.5 else 0)
                 + (10 if lv["room"] >= 3 else 6 if lv["room"] >= P["min_room_r"] else 0) + sess_pts
                 + (5 if right else -5 if (d == 1 and pd_pos > 0.85) or (d == -1 and pd_pos < 0.15) else 0)
                 - (15 if bad else 0))
        score = int(max(0, min(100, round(score))))
        return dict(name=name, side=d, score=score, grade=grade_of(score), valid=not fails and not hard, fails=fails,
                    anchor=float(anc), near_valid=not hard and all(x.startswith("Stop & room") for x in fails),
                    checks=checks, trigger=tg, poi=poi, levels=lv, agreement=agr, agree_names=agr_names,
                    chasing=bool(bad), passed=sum(c["ok"] for c in checks))

    a = s.atr_now

    def touched(zlist, d):
        out = []
        for z in zlist:
            if d == 1:
                ok = z["bot"] - 0.25 * a <= lo_win <= z["top"] + 0.1 * a and price >= z["bot"] - 0.1 * a and price - z["top"] <= MAX_FROM_POI * a
            else:
                ok = z["bot"] - 0.1 * a <= hi_win <= z["top"] + 0.25 * a and price <= z["top"] + 0.1 * a and z["bot"] - price <= MAX_FROM_POI * a
            if ok:
                out.append(z)
        return out

    allow = P.get("playbooks")
    cands = []
    for d in ([bias_d] if bias_d else [1, -1]):
        pois = touched(view.zones[d], d)
        anchor = lo_win if d == 1 else hi_win
        if bias_d == d and (not allow or "Trend pullback" in allow):
            ok_s = S["trend"] == d
            deep = S["retrace"] is not None and S["retrace"] >= P["min_pullback"] and S["phase"] != "broken"
            cands.append(evaluate("Trend pullback", d, bias_pts, pois, anchor, extra=[
                (f"{L['setup']} structure", ok_s, f"{L['setup']} is {side_word(S['trend']).lower()}" + ("" if ok_s else f" - wait for it to turn {side_word(d).lower()}")),
                (f"{L['setup']} pullback depth", deep, f"retraced {S['retrace']:.0%} of the leg" if S["retrace"] is not None else "no leg yet"),
                (f"{L['setup']} regime", view.s_regime in ("TRENDING", "TRANSITION"), f"{view.s_regime.lower()} ({view.s_regime_why})")]))
        if (bias_d in (d, 0) or bias_kind == "htf_pullback") and (not allow or "Liquidity sweep" in allow):
            lvls = [x for x in view.key_lv[d] if x]
            swept = [x for x in lvls if (d == 1 and lo_win < x < price) or (d == -1 and hi_win > x > price)]
            lvl = (max(swept) if d == 1 else min(swept)) if swept else None
            sp = [dict(name=f"{L['setup']}/{L['mid']} liquidity {f(lvl)}", top=lvl + 0.1 * a, bot=lvl - 0.1 * a)] if lvl else []
            near = lvl is not None and abs(price - lvl) <= MAX_FROM_POI * a
            cands.append(evaluate("Liquidity sweep", d, bias_pts if bias_d == d else 8, sp + pois, anchor,
                                  need_kinds=("structure", "sweep"),
                                  extra=[("Key level swept", near, f"swept {f(lvl)} and reclaimed it" if near else f"no {L['setup']}/{L['mid']} level swept recently")]))
        if bias_d == d and (not allow or "Breakout retest" in allow):
            lvl, i_s = None, s.i
            for j in reversed(s.sh if d == 1 else s.sl):
                if j > i_s - 3 or j < i_s - 100:
                    continue
                L0 = s.h[j] if d == 1 else s.l[j]
                if any((s.c[b - 1] - L0) * d <= 0 < (s.c[b] - L0) * d for b in range(max(j + 1, i_s - 12), i_s + 1)):
                    lvl = float(L0)
                    break
            rt = lvl is not None and (lo_win <= lvl + 0.25 * a if d == 1 else hi_win >= lvl - 0.25 * a) \
                and (price - lvl) * d > 0 and abs(price - lvl) <= 0.8 * a
            rp = [dict(name=f"{L['setup']} broken level {f(lvl)}", top=lvl + 0.15 * a, bot=lvl - 0.15 * a)] if rt else []
            cands.append(evaluate("Breakout retest", d, bias_pts, rp + (pois if rt else []), anchor,
                                  extra=[(f"{L['setup']} breakout retested", rt, f"broke {f(lvl)}, retested and holding" if rt else f"no fresh {L['setup']} breakout being retested")]))
        if view.s_regime == "RANGING" and not (bias_kind == "aligned" and bias_d == -d) and (not allow or "Range fade" in allow):
            edge = None
            for x in (s.supports if d == 1 else s.resistances):
                if (d == 1 and lo_win <= x + 0.2 * a and price >= x - 0.1 * a) or (d == -1 and hi_win >= x - 0.2 * a and price <= x + 0.1 * a):
                    edge = x
            if edge is None:
                band = s.bb_lo[s.i] if d == 1 else s.bb_up[s.i]
                if (d == 1 and lo_win <= band) or (d == -1 and hi_win >= band):
                    edge = float(band)
            ep = [dict(name=f"{L['setup']} range {'low' if d == 1 else 'high'} {f(edge)}", top=edge + 0.15 * a, bot=edge - 0.15 * a)] if edge else []
            mid_band = float(s.bb_mid[s.i])
            cands.append(evaluate("Range fade", d, 18 if bias_d == d else 12 if bias_d == 0 else 6, ep, anchor,
                                  need_kinds=("sweep", "candle", "structure"),
                                  target=mid_band if (mid_band - price) * d > 0 else None,
                                  extra=[(f"{L['setup']} range edge", edge is not None, "at the range edge" if edge else "price is mid-range")]))
        if bias_kind == "aligned" and bias_d == d and (not allow or "Momentum break" in allow):
            j = s.i
            prev = s.h[j - 20:j].max() if d == 1 else s.l[j - 20:j].min()
            close_to = (s.c[j] - prev) * d > 0 and abs(price - prev) <= 0.6 * a and (price - prev) * d > 0
            mp = [dict(name=f"{L['setup']} 20-bar breakout {f(prev)}", top=prev + 0.1 * a, bot=prev - 0.1 * a)] if close_to else []
            cands.append(evaluate("Momentum break", d, bias_pts - 5, mp, prev - 0.4 * a if d == 1 else prev + 0.4 * a,
                                  need_kinds=("structure", "retest", "candle"),
                                  extra=[(f"{L['setup']} fresh breakout", close_to, f"just broke {f(prev)}" if close_to else f"no fresh {L['setup']} breakout")]))
    valid = sorted([c for c in cands if c["valid"]], key=lambda c: c["score"], reverse=True)
    return dict(best=valid[0] if valid else None, valid=valid, cands=cands, hard=hard, e_votes=e_votes,
                e_regime=e_regime, e_regime_why=e_regime_why, sess=sess_txt, sess_pts=sess_pts, pd_pos=pd_pos)


# --------------------------------------------------------------------------
# LIMIT ORDER - best zone in the pullback
# --------------------------------------------------------------------------
def limit_levels(view, d, entry, far, atr_e, P):
    """Stop beyond the zone with a buffer, one take-profit at lim_tp_r R (the tested rule)."""
    s, S = view.s, view.st["setup"]
    buf = P["lim_buf_e"] * atr_e + P["lim_buf_s"] * s.atr_now + view.spread
    dist = max((entry - (far - d * buf)) * d, P["sl_min_e"] * atr_e, P["lim_min_spread"] * view.spread)
    problems = []
    if dist > P["lim_max_s"] * s.atr_now:
        problems.append(f"stop would be {fmt(dist, view.digits)} wide - zone too far from structure")
    tgt = S["target"] if S["trend"] == d else None
    room = abs(tgt - entry) / dist if tgt is not None and (tgt - entry) * d > 0 else 9.9
    if room < P["lim_min_room"]:
        problems.append(f"only 1:{room:.1f} room to the target {fmt(tgt, view.digits)}")
    tp1 = entry + d * P["lim_tp_r"] * dist
    tp2 = entry + d * min(P["tp2_cap_r"], max(P["lim_tp_r"], room - 0.1 if tgt is not None and room < 9 else 3)) * dist
    return dict(entry=float(entry), sl=float(entry - d * dist), tp1=float(tp1), tp2=float(tp2), dist=float(dist),
                room=float(room), target=tgt, problems=problems)


def limit_setup(view, price, atr_e, now, P):
    d = view.bias
    if not d:
        return None, "no top-down direction, so no limit order"
    a, S, M = view.s.atr_now, view.st["setup"], view.st["mid"]
    if P["lim_need_pullback"] and not (S["trend"] == d and S["phase"] == "pullback"):
        what = ("in an impulse - the pullback has not started" if S["trend"] == d and S["phase"] == "impulse"
                else f"not trending {side_word(d).lower()}")
        return None, f"{view.L['setup']} is {what}"
    inval = M["protected"] if M["trend"] == d else view.st["top"]["protected"]
    zs = view.zones[d]
    if d == 1:
        ahead = [z for z in zs if price - P["lim_max_dist_s"] * a < z["top"] < price - P["lim_min_dist_s"] * a and (not inval or z["bot"] > inval)]
    else:
        ahead = [z for z in zs if price + P["lim_min_dist_s"] * a < z["bot"] < price + P["lim_max_dist_s"] * a and (not inval or z["top"] < inval)]
    if not ahead:
        return None, f"no zone between price and the {view.L['mid']} invalidation level"
    scored = []
    for z in ahead:
        names = sorted({y["name"] for y in ahead if y["bot"] <= z["top"] + 0.15 * a and y["top"] >= z["bot"] - 0.15 * a})
        disc = S["leg_lo"] is not None and S["leg_hi"] is not None and (
            (d == 1 and z["top"] <= S["leg_lo"] + 0.5 * (S["leg_hi"] - S["leg_lo"])) or
            (d == -1 and z["bot"] >= S["leg_hi"] - 0.5 * (S["leg_hi"] - S["leg_lo"])))
        scored.append((len(names), -abs(price - (z["top"] if d == 1 else z["bot"])), z, names, disc))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    why = "every zone has a problem"
    exp_min = P["lim_expiry_min"] * (2 if view.entry_tf == "1h" else 1)
    for conf, _, z, names, disc in scored:
        width = z["top"] - z["bot"]
        entry = (z["top"] if d == 1 else z["bot"]) if width <= 0.6 * a else (z["top"] + z["bot"]) / 2
        far = z["bot"] if d == 1 else z["top"]
        lv = limit_levels(view, d, entry, far, atr_e, P)
        if lv["problems"]:
            why = lv["problems"][0]
            continue
        return dict(side=d, entry=lv["entry"], sl=lv["sl"], tp1=lv["tp1"], tp2=lv["tp2"], dist=lv["dist"],
                    zone_top=z["top"], zone_bot=z["bot"], zone=" + ".join(names), confluence=len(names),
                    discount=disc, expires=now + timedelta(minutes=exp_min), invalidation=inval, target=lv["target"]), None
    return None, why


def stx_word(d):
    return {1: "bullish", -1: "bearish", 0: "neutral"}[int(d)]


def structure_limit(view, reads, bias, bias_txt, now):
    """The pending order from market structure: entry at the best OB, SL beyond the HL/LH, TP at the HH/LL.
    Uses the setup timeframe first, then the middle one. Levels come straight from structure.py (fixed)."""
    if not bias:
        return None, f"no top-down structure agreement ({bias_txt})"
    why = []
    for x in reads:
        pl = x.get("plan")
        if not pl:
            why.append(f"{x['tf']}: {x.get('no_plan') or 'no plan'}")
            continue
        if pl["dir"] != bias:
            why.append(f"{x['tf']} structure is {x['bias'].lower()}, against the top-down bias")
            continue
        if not pl["valid"] or pl["status"] not in ("waiting",):
            why.append(f"{x['tf']}: " + (pl["notes"][0] if pl["notes"] else f"plan is {pl['status']}"))
            continue
        d, k = pl["dir"], x["key"]
        dist = abs(pl["entry"] - pl["stop_loss"])
        lim = dict(side=d, entry=pl["entry"], sl=pl["stop_loss"], tp1=pl["take_profit"], tp2=pl["take_profit"], dist=dist,
                   zone_top=pl["zone_top"], zone_bot=pl["zone_bot"], zone=f"{x['tf']} {pl['zone']}",
                   confluence=len(next((p["confluence"] for p in x["pois"] if p["type"] == pl["zone"]), [])),
                   discount=True, expires=now + timedelta(days=7), invalidation=k["origin"], target=pl["take_profit"],
                   rule="structure", tf=x["tf"], rr=pl["rr"], probability=pl["probability"],
                   sl_reason=pl["sl_reason"], tp_reason=pl["tp_reason"], structure_id=x["id"],
                   sl_label=k["origin_label"], anchor=k["origin"], tp_label=k["extreme_label"])
        return lim, None
    return None, "; ".join(why) if why else "no valid structure plan"


# --------------------------------------------------------------------------
# TRADE TRACKING
# --------------------------------------------------------------------------
def new_trade(view, kind, d, lv, now, playbook, extra=None):
    tr = dict(id=uuid.uuid4().hex[:10], symbol=view.symbol, tf=view.entry_tf, kind=kind, side=int(d),
              entry=float(lv["entry"]), sl=float(lv["sl"]), sl0=float(lv["sl"]), tp1=float(lv["tp1"]), tp2=float(lv["tp2"]),
              dist=float(lv["dist"]), status="pending" if kind == "limit" else "open", playbook=playbook,
              created=iso(now), filled_at=None if kind == "limit" else iso(now), last_ts=iso(now),
              realized_r=0.0, part=0.0, events=[], digits=view.digits)
    if extra:
        tr.update(extra)
    return tr


def _swings_low_high(h, l, k=3):
    highs, lows = [], []
    for i in range(k, len(h) - k):
        if h[i] >= h[i - k:i + k + 1].max():
            highs.append(i)
        if l[i] <= l[i - k:i + k + 1].min():
            lows.append(i)
    return highs, lows


def manage(trade, bars, view, P, now, price=None, atr_e=None):
    """
    Walk the closed entry-TF candles after trade['last_ts'] and update the trade.
    bars: DataFrame of CLOSED entry candles (UTC index). Returns the advice dict.
    Mutates trade (status, sl, tp, events, realized_r).
    """
    d, dg = trade["side"], trade.get("digits", view.digits)
    f = lambda x: fmt(x, dg)
    ev = trade["events"]
    L = view.L
    last = pd.Timestamp(trade["last_ts"])
    new = bars[bars.index > last]
    R = trade["dist"]
    spread = view.spread

    def log(ts, text):
        ev.append(dict(time=iso(ts), text=text))

    def close(ts, px, why):
        rem = 1 - trade["part"]
        trade["realized_r"] += rem * d * (px - trade["entry"]) / R
        trade["realized_r"] = round(trade["realized_r"] - spread / R, 3)     # spread cost
        trade["status"], trade["closed_at"], trade["exit"] = "closed", iso(ts), float(px)
        log(ts, f"{why} - result {trade['realized_r']:+.2f}R")

    hs = ls = None
    for ts, row in new.iterrows():
        hi, lo, cl = row["high"], row["low"], row["close"]
        st = trade["status"]
        if st == "pending":
            if pd.Timestamp(trade["expires"]) <= ts:
                trade["status"] = "cancelled"
                log(ts, "Cancelled: limit not filled in time")
                break
            touched = lo <= trade["entry"] if d == 1 else hi >= trade["entry"]
            ran = hi >= trade["tp2"] if d == 1 else lo <= trade["tp2"]
            if ran and not touched:
                trade["status"] = "cancelled"
                log(ts, f"Cancelled: price reached the target {f(trade['tp2'])} without filling - setup played out without us")
                break
            if not touched:
                continue
            trade["status"], trade["filled_at"] = "open", iso(ts)
            log(ts, f"Limit filled at {f(trade['entry'])}")
            st = "open"
        if st in ("open", "partial"):
            trade["mfe"] = max(trade.get("mfe", 0.0), d * ((hi if d == 1 else lo) - trade["entry"]) / R)
            trade["mae"] = min(trade.get("mae", 0.0), d * ((lo if d == 1 else hi) - trade["entry"]) / R)
            hit_sl = lo <= trade["sl"] if d == 1 else hi >= trade["sl"]
            hit_tp1 = st == "open" and (hi >= trade["tp1"] if d == 1 else lo <= trade["tp1"])
            hit_tp2 = hi >= trade["tp2"] if d == 1 else lo <= trade["tp2"]
            if hit_sl:                                   # worst case first
                be = abs(trade["sl"] - trade["entry"]) < 0.05 * R
                close(ts, trade["sl"], "Stopped at entry (break-even)" if be and st == "partial" else
                      "Trailing stop hit" if st == "partial" else "Stop loss hit")
                break
            if hit_tp1 and trade.get("full_close"):
                close(ts, trade["tp1"], f"Take profit hit {f(trade['tp1'])}")
                break
            if hit_tp1:
                trade["part"] = 0.5
                trade["realized_r"] += 0.5 * d * (trade["tp1"] - trade["entry"]) / R
                trade["sl"] = trade["entry"] + d * spread
                trade["status"] = st = "partial"
                log(ts, f"TP1 hit {f(trade['tp1'])}: closed half (+{0.5 * P['tp1_r']:.1f}R), stop moved to entry {f(trade['sl'])}")
            if st == "partial" and hit_tp2:
                close(ts, trade["tp2"], f"TP2 hit {f(trade['tp2'])}")
                break
            # break-even before TP1 (optional)
            if st == "open" and P["be_at_r"] and d * (hi if d == 1 else lo) - d * trade["entry"] >= P["be_at_r"] * R \
                    and (trade["sl"] - trade["entry"]) * d < 0:
                trade["sl"] = trade["entry"] + d * spread
                log(ts, f"Price +{P['be_at_r']:g}R - stop moved to entry {f(trade['sl'])}")
            # structure on the entry TF since the fill
            since = bars[(bars.index >= pd.Timestamp(trade["filled_at"])) & (bars.index <= ts)]
            if len(since) >= 8:
                hs, ls = _swings_low_high(since["high"].values, since["low"].values)
                if d == 1 and ls:
                    sw = float(since["low"].values[ls[-1]])
                elif d == -1 and hs:
                    sw = float(since["high"].values[hs[-1]])
                else:
                    sw = None
                if sw is not None:
                    if st == "partial" and P["trail"]:
                        new_sl = sw - d * (0.15 * R + spread)
                        if (new_sl - trade["sl"]) * d > 0.05 * R:
                            trade["sl"] = new_sl
                            log(ts, f"Stop trailed to {f(new_sl)} under the new {L['entry']} {'higher low' if d == 1 else 'lower high'}")
                    if st == "open" and P["early_exit"] and not trade.get("full_close") and (cl - sw) * d < 0 and (cl - trade["entry"]) * d < 0 \
                            and (sw - trade["entry"]) * d < 0:
                        close(ts, cl, f"Closed early: {L['entry']} broke structure against the trade (closed {'below' if d == 1 else 'above'} {f(sw)})")
                        break
        trade["last_ts"] = iso(ts)
    # setup-TF structure flip (checked on the current view)
    S = view.st["setup"]
    if trade["status"] in ("pending", "open", "partial") and P["flip_exit"] and S["trend"] == -d and S["break_bar"] is not None:
        brk_time = view.s.idx[S["break_bar"]]
        if brk_time > pd.Timestamp(trade["created"]) and S["break_kind"] == "CHoCH":
            why = f"{L['setup']} structure turned {side_word(-d).lower()} (CHoCH {'below' if d == 1 else 'above'} {f(S['break_level'])})"
            ts = bars.index[-1] if len(bars) else pd.Timestamp(now)
            if trade.get("full_close"):
                if not trade.get("flip_warned"):
                    trade["flip_warned"] = True
                    log(ts, f"Warning: {why}. Tested rule is to keep the order/stop as it is - "
                            f"close manually only if you want to be extra careful.")
            elif trade["status"] == "pending":
                trade["status"] = "cancelled"
                log(ts, f"Cancelled: {why}")
            elif trade["status"] == "open" and price is not None:
                close(ts, price, f"Closed: {why}")
            elif trade["status"] == "partial" and price is not None:
                close(ts, price, f"Closed rest: {why}")
    return advice(trade, view, price)


def advice(trade, view, price):
    d, dg, st = trade["side"], trade.get("digits", 5), trade["status"]
    f = lambda x: fmt(x, dg)
    R = trade["dist"]
    S = view.st["setup"]
    if st == "pending":
        gap = abs(price - trade["entry"]) if price is not None else None
        return dict(action="KEEP LIMIT", text=f"Structure still {side_word(d).lower()} on {view.L['setup']}. "
                    f"Price is {f(gap)} from your limit at {f(trade['entry'])}.")
    if st == "cancelled":
        return dict(action="CANCEL", text=trade["events"][-1]["text"] if trade["events"] else "Cancelled")
    if st == "closed":
        return dict(action="CLOSED", text=trade["events"][-1]["text"] if trade["events"] else "Closed")
    pnl = d * (price - trade["entry"]) / R if price is not None else 0
    with_us = S["trend"] == d
    text = (f"{'In profit' if pnl > 0 else 'Below entry' if pnl < 0 else 'At entry'} ({pnl:+.1f}R). "
            f"{view.L['setup']} structure {'still ' + side_word(d).lower() if with_us else 'not confirming yet'}. "
            f"Stop {f(trade['sl'])}, {'TP2' if st == 'partial' else 'TP1'} {f(trade['tp2'] if st == 'partial' else trade['tp1'])}.")
    last = trade["events"][-1] if trade["events"] else None
    action = "HOLD"
    if last and last["text"].startswith(("TP1", "Stop trailed", "Price +")) and pd.Timestamp(last["time"]) >= pd.Timestamp(trade["last_ts"]) - pd.Timedelta(minutes=3 * view.s.minutes):
        action = "UPDATE STOP"
    return dict(action=action, text=text)


# --------------------------------------------------------------------------
# LIVE ANALYSIS
# --------------------------------------------------------------------------
def build_view(symbol, entry_tf, frames):
    top_tf, mid_tf, setup_tf = STACKS[entry_tf]
    return View(symbol, entry_tf,
                se.Ctx(frames["top"].iloc[:-1], top_tf),
                se.Ctx(frames["mid"].iloc[:-1], mid_tf),
                se.Ctx(frames["setup"].iloc[:-1], setup_tf))


def analyse(symbol, entry_tf, frames, balance=100.0, risk_pct=1.0, cent=False, trades=None, now=None, P=None):
    P = {**DEFAULTS, **(P or {})}
    now = now or datetime.now(timezone.utc)
    view = build_view(symbol, entry_tf, frames)
    edf = frames["entry"]
    price = float(edf["close"].iloc[-1])
    e = se.Ctx(edf.iloc[:-1], entry_tf)
    L, dg = view.L, view.digits
    ratio = max(1, view.s.minutes // e.minutes)
    win = int(min(60, max(8, 3 * ratio)))
    lo_win, hi_win = float(edf["low"].iloc[-win - 1:].min()), float(edf["high"].iloc[-win - 1:].max())

    ms = market_setup(view, e, price, lo_win, hi_win, now, P)
    top_tf, mid_tf, setup_tf = view.tfs
    T = stx.analyse_tf(view.t, L["top"], symbol)
    M = stx.analyse_tf(view.m, L["mid"], symbol, T["dir"], L["top"])
    Sx = stx.analyse_tf(view.s, L["setup"], symbol, M["dir"] or T["dir"], L["mid"])
    Ex = stx.analyse_tf(e, L["entry"], symbol, Sx["dir"], L["setup"])
    for x, role in ((T, "Bias"), (M, "Bias"), (Sx, "Setup"), (Ex, "Entry")):
        x["role"] = role
    s_bias = T["dir"] if T["dir"] and M["dir"] in (0, T["dir"]) else (M["dir"] if not T["dir"] else 0)
    s_bias_txt = (f"{L['top']} and {L['mid']} structure both {stx_word(s_bias)}" if s_bias and T["dir"] == M["dir"]
                  else f"{L['top']} {stx_word(T['dir'])}, {L['mid']} {stx_word(M['dir'])}" if s_bias
                  else f"{L['top']} {stx_word(T['dir'])} vs {L['mid']} {stx_word(M['dir'])} - no top-down agreement")
    if P["limit_on"] and P["limit_rule"] == "structure":
        lim, lim_why = structure_limit(view, (Sx, Ex, M), s_bias, s_bias_txt, now)
    else:
        lim, lim_why = limit_setup(view, price, e.atr_now, now, P) if P["limit_on"] else (None, "off")
    story = market_story(view, price, now)

    warnings, reasons = [], list(ms["hard"])
    age_min = (now - edf.index[-1].to_pydatetime()).total_seconds() / 60
    if age_min > 3 * e.minutes + 5:
        reasons.insert(0, f"last candle is {age_min / 60:.1f}h old - market looks closed")
    if ms["sess_pts"] < 0:
        warnings.append("Outside London/New York hours - fewer, weaker setups.")

    ladder = []
    for key, c, b in (("top", view.t, view.top_b), ("mid", view.m, view.mid_b)):
        x = view.st[key]
        ladder.append(dict(tf=L[key], role="Bias", dir=side_word(b["dir"]),
                           detail=f"{x['phase'] if x['trend'] else 'range'}" + (f", protected {'low' if x['trend'] == 1 else 'high'} {fmt(x['protected'], dg)}" if x["protected"] else "")
                           + (f", swings {' '.join(x['labels'])}" if x["labels"] else "")))
    S = view.st["setup"]
    ladder.append(dict(tf=L["setup"], role="Setup", dir=side_word(S["trend"]),
                       detail=f"{view.s_regime.lower()}, {S['phase']}" + (f" {S['retrace']:.0%}" if S["phase"] == "pullback" else "")))
    ladder.append(dict(tf=L["entry"], role="Entry", dir=side_word(e.structure), detail=f"{ms['e_regime'].lower()} ({ms['e_regime_why']})"))

    result = dict(structure=[T, M, Sx, Ex], structure_bias=side_word(s_bias), structure_bias_text=s_bias_txt,
                  symbol=symbol, entry_tf=entry_tf, price=round(price, dg), digits=dg, time_utc=now.strftime("%Y-%m-%d %H:%M"),
                  tested=dict(limit=TESTED["limit"].get(entry_tf), market=TESTED["market"].get(entry_tf), summary=TESTED["summary"]),
                  stack=L, bias=side_word(view.bias), bias_text=view.bias_txt, story=story, ladder=ladder,
                  regime=view.s_regime, session=ms["sess"], warnings=warnings, reasons=reasons,
                  votes=dict(setup=view.s_votes, entry=ms["e_votes"]),
                  candidates=[dict(name=c["name"], side="BUY" if c["side"] == 1 else "SELL", score=c["score"], grade=c["grade"],
                                   valid=c["valid"], passed=c["passed"], total=len(c["checks"]), fails=c["fails"])
                              for c in sorted(ms["cands"], key=lambda c: (c["valid"], c["passed"], c["score"]), reverse=True)])

    def pack(lv):
        lots, note, risk_amt = se.lot_size(symbol, lv["entry"], lv["dist"], balance, risk_pct, cent)
        if note and note not in warnings:
            warnings.append(note)
        return dict(entry=round(lv["entry"], dg), stop_loss=round(lv["sl"], dg), take_profit_1=round(lv["tp1"], dg),
                    take_profit_2=round(lv["tp2"], dg), sl_distance=round(lv["dist"], dg), rr_tp1=f"1:{P['tp1_r']:g}",
                    rr_tp2=f"1:{abs(lv['tp2'] - lv['entry']) / lv['dist']:.1f}", lot_size=lots, risk_amount=round(risk_amt, 2))

    # ---- tracked trades first: they are managed, and they block new signals on this market ----
    tracked = []
    for tr in (trades or []):
        if tr.get("symbol") != symbol or tr.get("tf") != entry_tf or tr.get("status") in ("closed", "cancelled"):
            continue
        adv = manage(tr, edf.iloc[:-1], view, P, now, price, e.atr_now)
        tracked.append(dict(trade=tr, advice=adv))
    active = [t for t in tracked if t["trade"]["status"] in ("pending", "open", "partial")]
    result["tracked"] = tracked

    best = ms["best"]
    focus = best or max(ms["cands"], key=lambda c: (c["passed"], c["score"]), default=None)
    if focus:
        result["focus"] = dict(name=focus["name"], side="BUY" if focus["side"] == 1 else "SELL", grade=focus["grade"],
                               score=focus["score"], checks=focus["checks"])

    if reasons:
        result.update(status="NO TRADE", headline="Stand aside")
    elif active:
        result.update(status="TRACKING", headline="Managing your trade")
        reasons.append("You already have a trade on this market - new signals are paused until it is closed.")
    elif best and best["grade"] in P["trade_grades"]:
        d, lv = best["side"], best["levels"]
        tg = best["trigger"]
        tr = new_trade(view, "market", d, lv, now, best["name"],
                       extra=dict(fail_level=tg["fail"] if tg else None))
        story["next"] = (f"Entry trigger fired: {tg['name'] if tg else 'confirmation'} on {L['entry']} at "
                         f"{' + '.join(best['poi']['names']) if best['poi'] else 'the zone'}. "
                         f"Expected move: {'up' if d == 1 else 'down'} towards {fmt(lv['tp2'], dg)}.")
        result.update(status="TRADE", side="BUY" if d == 1 else "SELL", headline=f"{'BUY' if d == 1 else 'SELL'} now",
                      playbook=best["name"], grade=best["grade"], score=best["score"], trade=pack(lv),
                      confluence=best["agree_names"], agreement=round(100 * best["agreement"]), new_trade=tr,
                      management=[f"At TP1 ({fmt(lv['tp1'], dg)}) half is closed and the stop moves to entry.",
                                  f"Then the stop trails under each new {L['entry']} {'higher low' if d == 1 else 'lower high'}.",
                                  f"Closed early if {L['entry']} breaks structure against you before TP1, "
                                  f"or if {L['setup']} turns {side_word(-d).lower()}.",
                                  "Tap 'I took it' so the app tracks it and tells you every change."])
    else:
        result.update(status="WAIT" if view.bias or view.s_regime == "RANGING" else "NO TRADE",
                      headline="Wait" if view.bias or view.s_regime == "RANGING" else "No trade")
        if best:
            reasons.append(f"Best market setup is {best['name']} grade {best['grade']} ({best['score']}/100) - only A and A+ are traded.")
        elif focus:
            reasons += focus["fails"][:4]
        if focus and focus["chasing"]:
            result["headline"] = "Don't chase"

    if lim and not active and not result.get("status") == "NO TRADE":
        d = lim["side"]
        tr = new_trade(view, "limit", d, lim, now, "Structure OB limit" if lim.get("rule") == "structure" else "Pullback limit",
                       extra=dict(expires=iso(lim["expires"]), zone_top=lim["zone_top"], zone_bot=lim["zone_bot"],
                                  full_close=P["lim_full_close"]))
        pk = pack(lim)
        pk["rr_tp1"] = f"1:{lim['rr']:.2f}" if lim.get("rr") else f"1:{P['lim_tp_r']:g}"
        structural = lim.get("rule") == "structure"
        result["limit"] = dict(side="BUY LIMIT" if d == 1 else "SELL LIMIT", **pk, single_tp=P["lim_full_close"],
                               zone=lim["zone"], zone_top=round(lim["zone_top"], dg), zone_bot=round(lim["zone_bot"], dg),
                               confluence=lim["confluence"], discount=lim["discount"],
                               distance=round(abs(price - lim["entry"]), dg),
                               expires="next BOS/CHoCH" if structural else lim["expires"].strftime("%H:%M UTC"),
                               cancel_if=([f"price reaches the {lim['tp_label']} {fmt(lim['tp1'], dg)} before filling",
                                           f"a {lim['tf']} CHoCH {'down' if d == 1 else 'up'} (structure turns {side_word(-d).lower()})",
                                           f"a candle closes {'below' if d == 1 else 'above'} the {lim['sl_label']} {fmt(lim['anchor'], dg)}"]
                                          if structural else
                                          [f"price reaches the target {fmt(lim['tp2'], dg)} before filling",
                                           f"{L['setup']} structure turns {side_word(-d).lower()}",
                                           f"not filled by {lim['expires'].strftime('%H:%M UTC')}"]),
                               rule=lim.get("rule", "zone"), from_tf=lim.get("tf"), probability=lim.get("probability"),
                               sl_reason=lim.get("sl_reason"), tp_reason=lim.get("tp_reason"), structure_id=lim.get("structure_id"),
                               new_trade=tr)
    elif not lim:
        result["limit_note"] = f"No limit order: {lim_why}."
    if lim and lim.get("rule") == "structure":
        result["tested"]["limit"] = TESTED["structure"]
    return result
