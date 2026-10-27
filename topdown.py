"""
topdown.py - professional top-down analysis built on signal_engine.py
=====================================================================

How a trade is found (example: 1m entry):

  1. BIAS      4H + 1H  -> market structure (HH/HL or LH/LL, last BOS/CHoCH)
                           + EMA50/200 trend. Decides which side we may trade.
  2. SETUP     15M      -> regime (trending / ranging), structure, and the
                           point of interest (POI) price pulled back into:
                           order block, fair value gap, S/R, previous-day
                           level, Fibonacci 50-61.8%, EMA50.
  3. ENTRY     1M       -> a FRESH trigger at that POI: CHoCH/BOS, liquidity
                           sweep + reclaim, engulfing / pin bar, breakout-retest.

  Five playbooks are tested on every check and only the best valid one is used:
     Trend pullback   - with the HTF trend, after a pullback into a POI
     Liquidity sweep  - stop-hunt below/above a key level, then reclaim
     Breakout retest  - 15M level broken, retested, held
     Range fade       - 15M range edge rejection (only when ranging)
     Momentum break   - fresh 15M breakout, entered close to the level

  All 29 strategies in signal_engine.py vote on the 15M and 1M charts; their
  agreement is part of the score (and must be at least 50%).

  Hard "don't chase" rules: no entry if price is already stretched from the
  1M EMA20, the trigger is old, price ran far from the trigger, 6+ candles in
  a row already went our way, RSI is already extreme, or price left the POI.

  Score 0-100 -> grade A+ / A / B / C.  Only A+ and A are "TRADE NOW".
  Otherwise you get WAIT (where to wait, what trigger to wait for) or NO TRADE.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

import signal_engine as se

# entry timeframe -> (top bias TF, mid bias TF, setup TF)
STACKS = {
    "1min":  ("4h", "1h", "15min"),
    "5min":  ("4h", "1h", "15min"),
    "15min": ("1day", "4h", "1h"),
    "1h":    ("1week", "1day", "4h"),
}
SIZES = {"entry": 500, "setup": 300, "mid": 300, "top": 300}
LABEL = {"1min": "1M", "5min": "5M", "15min": "15M", "1h": "1H", "4h": "4H", "1day": "D1", "1week": "W1"}
FRESH_BARS = {"1min": 6, "5min": 4, "15min": 3, "1h": 3}     # a trigger older than this is stale
MIN_RR = 2.0                 # room needed before the first strong level
GRADES = ((75, "A+"), (65, "A"), (55, "B"), (0, "C"))
TRADE_GRADES = ("A+", "A")

# don't-chase limits (entry timeframe ATR units unless noted)
MAX_EXT_EMA = 1.8            # price vs EMA20
MAX_RUN = 1.5                # price vs the trigger level
MAX_STREAK = 6               # candles in a row in our direction
RSI_CHASE = (75, 25)         # buy above 75 / sell below 25 = chasing
MAX_FROM_POI = 1.0           # setup-TF ATR: price must still be near the POI


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def fmt(x, digits):
    return f"{x:.{digits}f}"


def side_word(d):
    return {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[d]


def tf_bias(c):
    """Direction of one timeframe from structure + EMAs. Returns dict."""
    i = c.i
    seq, seq_txt = 0, "no clear swing sequence"
    if len(c.sh) >= 2 and len(c.sl) >= 2:
        hh = c.h[c.sh[-1]] > c.h[c.sh[-2]]
        hl = c.l[c.sl[-1]] > c.l[c.sl[-2]]
        if hh and hl:
            seq, seq_txt = 1, "higher highs & higher lows"
        elif not hh and not hl:
            seq, seq_txt = -1, "lower highs & lower lows"
        else:
            seq_txt = "mixed swings"
    ema_d = 0
    if c.c[i] > c.ema50[i] > c.ema200[i]:
        ema_d = 1
    elif c.c[i] < c.ema50[i] < c.ema200[i]:
        ema_d = -1
    st = c.structure
    score = 1.5 * st + seq + ema_d
    d = 1 if score >= 2 else -1 if score <= -2 else 0
    brk = ""
    if c.last_break:
        _, kind, lvl = c.last_break
        brk = f"last {kind} {'up' if st == 1 else 'down'}"
    ema_txt = {1: "above EMA50/200", -1: "below EMA50/200", 0: "EMAs mixed"}[ema_d]
    strength = "strong" if abs(score) >= 3 else "moderate" if d else "weak"
    return dict(dir=d, score=score, strength=strength,
                detail=", ".join(x for x in (seq_txt, brk, ema_txt) if x))


def combine_bias(top, mid, tl, ml):
    """Top-down decision: which side are we allowed to trade?"""
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
    """Points of interest on context c for direction d (+1 buy zones below, -1 sell zones above)."""
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
            gap_bot, gap_top = c.h[j - 2], c.l[j]
            if gap_top - gap_bot > 0.1 * a and (j == i or c.l[j + 1:i + 1].min() > gap_bot):
                add("fair value gap", gap_top, gap_bot)
                found += 1
                if found == 2:
                    break
        for s in c.supports:
            add("support", s + 0.15 * a, s - 0.15 * a)
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
            gap_bot, gap_top = c.h[j], c.l[j - 2]
            if gap_top - gap_bot > 0.1 * a and (j == i or c.h[j + 1:i + 1].max() < gap_top):
                add("fair value gap", gap_top, gap_bot)
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
        out.append(dict(strategy=name, family=fam, vote=int(v), reason=why,
                        weight=round(eff, 2), active=eff > 0))
    return out


def agreement(setup_votes, entry_votes, d):
    """Weighted agreement of all strategies with direction d (15M counts 60%, 1M 40%)."""
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
    """Engulfing or pin bar in direction d on bar j."""
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
    """Entry-timeframe triggers in direction d within the last `fresh` closed bars."""
    i, out, k = e.i, [], se.SWING_K
    # 1. CHoCH / BOS
    if e.last_break and e.structure == d and i - e.last_break[0] < fresh:
        b, kind, lvl = e.last_break
        out.append(dict(name=f"{kind} ({'bullish' if d == 1 else 'bearish'} break of structure)",
                        quality=15 if kind == "CHoCH" else 12, bar=b, level=float(lvl), fail=float(lvl),
                        kind="structure"))
    # 2. liquidity sweep + reclaim (entry swings or key higher-TF levels)
    for j in range(i, max(i - fresh, k + 2), -1):
        if d == 1:
            lv = [e.l[s] for s in e.sl if j - 60 <= s and s + k < j] + [x for x in key_levels if x]
            hit = [x for x in lv if e.l[j] < x < e.c[j]]
            if hit and e.c[j] > e.o[j]:
                lvl = max(hit)
                out.append(dict(name="liquidity sweep + reclaim", quality=14, bar=j, level=float(lvl),
                                fail=float(lvl), extreme=float(e.l[j]), kind="sweep"))
                break
        else:
            lv = [e.h[s] for s in e.sh if j - 60 <= s and s + k < j] + [x for x in key_levels if x]
            hit = [x for x in lv if e.h[j] > x > e.c[j]]
            if hit and e.c[j] < e.o[j]:
                lvl = min(hit)
                out.append(dict(name="liquidity sweep + reclaim", quality=14, bar=j, level=float(lvl),
                                fail=float(lvl), extreme=float(e.h[j]), kind="sweep"))
                break
    # 3. rejection candle (at the POI if we have one)
    for j in range(i, max(i - fresh, 1), -1):
        pat = candle_signal(e, j, d)
        if not pat:
            continue
        if poi and not (poi["bot"] - 0.3 * e.atr_now <= (e.l[j] if d == 1 else e.h[j]) <= poi["top"] + 0.3 * e.atr_now):
            continue
        out.append(dict(name=pat, quality=10, bar=j, level=float(e.c[j]),
                        fail=float(e.l[j] if d == 1 else e.h[j]), kind="candle"))
        break
    # 4. breakout & retest on the entry chart
    try:
        if se.s_breakout_retest(e)[0] == d:
            out.append(dict(name="breakout & retest", quality=12, bar=i, level=float(e.c[i]),
                            fail=float(e.l[i - 2:i + 1].min() if d == 1 else e.h[i - 2:i + 1].max()), kind="retest"))
    except Exception:
        pass
    out.sort(key=lambda t: (t["quality"], t["bar"]), reverse=True)
    return out


def chase_check(e, d, price, trig_level):
    """Returns list of reasons the move is already running (empty = fine)."""
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


def grade_of(score):
    return next(g for lim, g in GRADES if score >= lim)


# --------------------------------------------------------------------------
# risk: SL / TP
# --------------------------------------------------------------------------
def build_levels(symbol, d, price, anchor, s, e, m, t, digits, target_override=None):
    """SL beyond the structural anchor; TP1 = 2R; TP2 = next strong level (max 4R)."""
    spread = se.SYMBOLS.get(symbol, {}).get("spread", 0.0)
    buf = max(0.3 * e.atr_now, 0.1 * s.atr_now) + spread
    sl = anchor - buf if d == 1 else anchor + buf
    dist = (price - sl) * d
    min_dist = max(1.0 * e.atr_now, 0.25 * s.atr_now)
    max_dist = 1.5 * s.atr_now
    if dist < min_dist:
        dist = min_dist
    sl = price - d * dist
    problems = []
    if dist > max_dist:
        problems.append(f"stop would be {fmt(dist, digits)} wide (more than 1.5x {LABEL[s.interval]} ATR)")

    # strong levels in the way
    if d == 1:
        obs = list(s.resistances) + list(m.resistances) + list(t.resistances)
        obs += [x for x in (s.prev_day_high, m.prev_day_high) if x]
        if s.bear_ob:
            obs.append(s.bear_ob[0])
        obs = [x for x in obs if x > price + 0.2 * e.atr_now]
        nearest = min(obs) if obs else None
    else:
        obs = list(s.supports) + list(m.supports) + list(t.supports)
        obs += [x for x in (s.prev_day_low, m.prev_day_low) if x]
        if s.bull_ob:
            obs.append(s.bull_ob[1])
        obs = [x for x in obs if x < price - 0.2 * e.atr_now]
        nearest = max(obs) if obs else None
    if target_override is not None:
        nearest = target_override
    room = abs(nearest - price) / dist if nearest is not None else 9.9
    if room < MIN_RR:
        problems.append(f"only 1:{room:.1f} room before the level at {fmt(nearest, digits)}")
    tp1 = price + d * MIN_RR * dist
    if nearest is None:
        tp2 = price + d * 3 * dist
    else:
        cap = price + d * 4 * dist
        tgt = nearest - d * 0.1 * e.atr_now
        tp2 = min(tgt, cap) if d == 1 else max(tgt, cap)
        tp2 = max(tp2, tp1) if d == 1 else min(tp2, tp1)
    return dict(entry=price, sl=sl, tp1=tp1, tp2=tp2, dist=dist, room=room,
                nearest=nearest, problems=problems)


# --------------------------------------------------------------------------
# main analysis
# --------------------------------------------------------------------------
def analyse(symbol, entry_tf, frames, balance=100.0, risk_pct=2.0, cent=False):
    """
    frames: {"entry": df, "setup": df, "mid": df, "top": df} - live candles, oldest first.
    The last row of each is the forming candle and is ignored for analysis.
    """
    top_tf, mid_tf, setup_tf = STACKS[entry_tf]
    L = {"top": LABEL[top_tf], "mid": LABEL[mid_tf], "setup": LABEL[setup_tf], "entry": LABEL[entry_tf]}
    digits = se.SYMBOLS.get(symbol, {}).get("digits", 5)
    now = datetime.now(timezone.utc)
    edf = frames["entry"]
    price = float(edf["close"].iloc[-1])

    e = se.Ctx(edf.iloc[:-1], entry_tf)
    s = se.Ctx(frames["setup"].iloc[:-1], setup_tf)
    m = se.Ctx(frames["mid"].iloc[:-1], mid_tf)
    t = se.Ctx(frames["top"].iloc[:-1], top_tf)

    top_b, mid_b = tf_bias(t), tf_bias(m)
    bias_d, bias_kind, bias_pts, bias_txt = combine_bias(top_b, mid_b, L["top"], L["mid"])
    s_regime, s_regime_why = se.detect_regime(s)
    e_regime, e_regime_why = se.detect_regime(e)
    s_votes, e_votes = strategy_votes(s, s_regime), strategy_votes(e, e_regime)
    sess_pts, sess_txt = session_score(symbol, now)
    fresh = FRESH_BARS[entry_tf]
    ratio = max(1, s.minutes // e.minutes)
    win = int(min(60, max(8, 3 * ratio)))                    # entry bars covering ~3 setup candles
    lo_win = float(edf["low"].iloc[-win - 1:].min())       # includes the forming candle
    hi_win = float(edf["high"].iloc[-win - 1:].max())

    warnings, hard = [], []
    age_min = (now - edf.index[-1].to_pydatetime()).total_seconds() / 60
    if age_min > 3 * e.minutes + 5:
        hard.append(f"last candle is {age_min / 60:.1f}h old - market looks closed")
    if s_regime == "VOLATILE" or e_regime == "VOLATILE":
        hard.append(f"volatility spike on {L['setup'] if s_regime == 'VOLATILE' else L['entry']} - likely news, stand aside")
    if sess_pts < 0:
        warnings.append("Outside London/New York hours - fewer, weaker setups.")

    # premium / discount on the mid timeframe (last ~60 bars dealing range)
    rh, rl = float(m.h[-60:].max()), float(m.l[-60:].min())
    pd_pos = (price - rl) / (rh - rl) if rh > rl else 0.5

    key_lv = {1: [s.prev_day_low, m.prev_day_low] + [s.l[j] for j in s.sl[-4:]] + list(m.supports),
              -1: [s.prev_day_high, m.prev_day_high] + [s.h[j] for j in s.sh[-4:]] + list(m.resistances)}

    ladder = [
        dict(tf=L["top"], role="Bias", dir=side_word(top_b["dir"]), detail=top_b["detail"], strength=top_b["strength"]),
        dict(tf=L["mid"], role="Bias", dir=side_word(mid_b["dir"]), detail=mid_b["detail"], strength=mid_b["strength"]),
        dict(tf=L["setup"], role="Setup", dir=side_word(s.structure), detail=f"{s_regime.lower()} ({s_regime_why}), structure {side_word(s.structure).lower()}"),
        dict(tf=L["entry"], role="Entry", dir=side_word(e.structure), detail=f"{e_regime.lower()} ({e_regime_why})"),
    ]

    # ---------------- playbooks ----------------
    def evaluate(name, d, kind_pts, poi_list, anchor, need_trig_kinds=None, target=None, extra_checks=()):
        """Common scoring + gates. Returns candidate dict."""
        checks, fails = [], []

        def chk(label, ok, detail, hard_fail=True):
            checks.append(dict(item=label, ok=bool(ok), detail=detail))
            if not ok and hard_fail:
                fails.append(f"{label}: {detail}")

        chk(f"Top-down bias ({L['top']} → {L['mid']})", bias_d in (d, 0), bias_txt)
        for lab, ok, det in extra_checks:
            chk(lab, ok, det)
        poi = None
        if poi_list:
            names = sorted({p["name"] for p in poi_list})
            poi = dict(top=max(p["top"] for p in poi_list), bot=min(p["bot"] for p in poi_list), names=names)
            chk("Location (point of interest)", True, " + ".join(names))
        else:
            chk("Location (point of interest)", False, "price is not at a key zone")
        trig = find_triggers(e, d, fresh, poi, key_lv[d])
        if need_trig_kinds:
            trig = [x for x in trig if x["kind"] in need_trig_kinds]
        holding = [x for x in trig if (price - x["fail"]) * d > 0]
        tg = holding[0] if holding else None
        if tg:
            ago = e.i - tg["bar"]
            chk(f"{L['entry']} entry trigger", True, f"{tg['name']}, {ago} candle{'s' if ago != 1 else ''} ago, holding")
        elif trig:
            chk(f"{L['entry']} entry trigger", False,
                f"{trig[0]['name']} formed but price is back {'below' if d == 1 else 'above'} {fmt(trig[0]['fail'], digits)} - it failed")
        else:
            chk(f"{L['entry']} entry trigger", False, f"no fresh {L['entry']} trigger yet (last {fresh} candles)")
        bad, ext = chase_check(e, d, price, tg["level"] if tg else None)
        chk("Not chasing", not bad, "; ".join(bad) if bad else f"{ext:+.1f} ATR from {L['entry']} EMA20 - fresh")
        agr, agr_names = agreement(s_votes, e_votes, d)
        chk("Strategy confluence", agr >= 0.5, f"{agr:.0%} of weighted strategy votes agree ({len(agr_names)} strategies)")
        anc = anchor
        if tg and tg.get("extreme") is not None:
            anc = min(anc, tg["extreme"]) if d == 1 else max(anc, tg["extreme"])
        lv = build_levels(symbol, d, price, anc, s, e, m, t, digits, target)
        chk("Room to target", not lv["problems"],
            "; ".join(lv["problems"]) if lv["problems"] else
            (f"1:{lv['room']:.1f} before the next level" if lv["nearest"] is not None else "no strong level in the way"))
        chk("Session", sess_pts > 0, sess_txt, hard_fail=False)
        right_side = (d == 1 and pd_pos <= 0.5) or (d == -1 and pd_pos >= 0.5)
        chk(f"{L['mid']} premium/discount", right_side,
            f"price at {pd_pos:.0%} of the {L['mid']} range ({'discount' if pd_pos < 0.5 else 'premium'})", hard_fail=False)

        n_poi = len(poi["names"]) if poi else 0
        score = (kind_pts
                 + (0 if not n_poi else 8 if n_poi == 1 else 14 if n_poi == 2 else 20)
                 + (tg["quality"] if tg else 0)
                 + (15 * agr if agr >= 0.5 else 0)
                 + (10 if lv["room"] >= 3 else 6 if lv["room"] >= MIN_RR else 0)
                 + sess_pts
                 + (5 if right_side else -5 if (d == 1 and pd_pos > 0.85) or (d == -1 and pd_pos < 0.15) else 0))
        if bad:
            score -= 15
        score = int(max(0, min(100, round(score))))
        return dict(name=name, side=d, score=score, grade=grade_of(score), valid=not fails and not hard,
                    fails=fails, checks=checks, trigger=tg, poi=poi, levels=lv, agreement=agr,
                    agree_names=agr_names, chasing=bool(bad), passed=sum(c["ok"] for c in checks))

    cands = []

    def touched(zlist, d, tol_in=0.1, tol_out=0.25):
        """Zones that contain the recent pullback extreme and that price is still near."""
        a = s.atr_now
        out = []
        for z in zlist:
            if d == 1:
                ok = z["bot"] - tol_out * a <= lo_win <= z["top"] + tol_in * a
                ok = ok and price >= z["bot"] - 0.1 * a and price - z["top"] <= MAX_FROM_POI * a
            else:
                ok = z["bot"] - tol_in * a <= hi_win <= z["top"] + tol_out * a
                ok = ok and price <= z["top"] + 0.1 * a and z["bot"] - price <= MAX_FROM_POI * a
            if ok:
                out.append(z)
        return out

    sides = [bias_d] if bias_d else [1, -1]
    for d in sides:
        word = "buy" if d == 1 else "sell"
        pois = touched(zones(s, d, setup_tf) + zones(m, d, mid_tf), d)
        anchor_pull = lo_win if d == 1 else hi_win

        # 1. TREND PULLBACK - with the trend, after a pullback into a POI
        if bias_d == d:
            setup_ok = s.structure == d
            cands.append(evaluate(
                "Trend pullback", d, bias_pts, pois, anchor_pull,
                extra_checks=[(f"{L['setup']} structure", setup_ok,
                               f"{L['setup']} is {side_word(s.structure).lower()}" + ("" if setup_ok else f" - wait for it to turn {side_word(d).lower()}")),
                              (f"{L['setup']} regime", s_regime in ("TRENDING", "TRANSITION"),
                               f"{s_regime.lower()} ({s_regime_why})")]))

        # 2. LIQUIDITY SWEEP REVERSAL - stop-hunt of a key level, then reclaim
        if bias_d in (d, 0) or bias_kind == "htf_pullback":
            lv_list = [x for x in key_lv[d] if x]
            swept = [x for x in lv_list if (d == 1 and lo_win < x < price) or (d == -1 and hi_win > x > price)]
            lvl = (max(swept) if d == 1 else min(swept)) if swept else None
            sweep_poi = [dict(name=f"{L['setup']}/{L['mid']} liquidity at {fmt(lvl, digits)}", top=lvl + 0.1 * s.atr_now,
                              bot=lvl - 0.1 * s.atr_now)] if lvl else []
            near = lvl is not None and abs(price - lvl) <= MAX_FROM_POI * s.atr_now
            cands.append(evaluate(
                "Liquidity sweep", d, bias_pts if bias_d == d else 8, sweep_poi + pois, anchor_pull,
                need_trig_kinds=("structure", "sweep"),
                extra_checks=[("Key level swept", near,
                               f"swept {fmt(lvl, digits)} and reclaimed it" if near else f"no {L['setup']}/{L['mid']} level swept recently")]))

        # 3. BREAKOUT RETEST - level broken on the setup TF, retested, holding
        if bias_d == d:
            lvl, i_s = None, s.i
            piv = s.sh if d == 1 else s.sl
            for j in reversed(piv):
                if j > i_s - 3 or j < i_s - 100:
                    continue
                L0 = s.h[j] if d == 1 else s.l[j]
                broke = any((s.c[b - 1] - L0) * d <= 0 < (s.c[b] - L0) * d for b in range(max(j + 1, i_s - 12), i_s + 1))
                if broke:
                    lvl = float(L0)
                    break
            retest = lvl is not None and (lo_win <= lvl + 0.25 * s.atr_now if d == 1 else hi_win >= lvl - 0.25 * s.atr_now) \
                and (price - lvl) * d > 0 and abs(price - lvl) <= 0.8 * s.atr_now
            rpoi = [dict(name=f"{L['setup']} broken level {fmt(lvl, digits)}", top=lvl + 0.15 * s.atr_now,
                         bot=lvl - 0.15 * s.atr_now)] if retest else []
            cands.append(evaluate(
                "Breakout retest", d, bias_pts, rpoi + (pois if retest else []), anchor_pull,
                extra_checks=[(f"{L['setup']} breakout retested", retest,
                               f"broke {fmt(lvl, digits)}, retested and holding" if retest else f"no fresh {L['setup']} breakout being retested")]))

        # 4. RANGE FADE - only when the setup TF is ranging
        if s_regime == "RANGING" and not (bias_kind == "aligned" and bias_d == -d):
            edges = s.supports if d == 1 else s.resistances
            edge = None
            for x in edges:
                if (d == 1 and lo_win <= x + 0.2 * s.atr_now and price >= x - 0.1 * s.atr_now) or \
                   (d == -1 and hi_win >= x - 0.2 * s.atr_now and price <= x + 0.1 * s.atr_now):
                    edge = x
            if edge is None:
                band = s.bb_lo[s.i] if d == 1 else s.bb_up[s.i]
                if (d == 1 and lo_win <= band) or (d == -1 and hi_win >= band):
                    edge = float(band)
            epoi = [dict(name=f"{L['setup']} range {'low' if d == 1 else 'high'} {fmt(edge, digits)}",
                         top=edge + 0.15 * s.atr_now, bot=edge - 0.15 * s.atr_now)] if edge else []
            mid_band = float(s.bb_mid[s.i])
            target = mid_band if (mid_band - price) * d > 0 else None
            cands.append(evaluate(
                "Range fade", d, 18 if bias_d == d else 12 if bias_d == 0 else 6, epoi, anchor_pull,
                need_trig_kinds=("sweep", "candle", "structure"), target=target,
                extra_checks=[(f"{L['setup']} range edge", edge is not None,
                               f"at the range {'low' if d == 1 else 'high'}" if edge else f"price is mid-range, not at an edge")]))

        # 5. MOMENTUM BREAK - fresh setup-TF breakout, entered close to the level
        if bias_kind == "aligned" and bias_d == d:
            j = s.i
            prev = s.h[j - 20:j].max() if d == 1 else s.l[j - 20:j].min()
            fresh_break = (s.c[j] - prev) * d > 0
            close_to = fresh_break and abs(price - prev) <= 0.6 * s.atr_now and (price - prev) * d > 0
            mpoi = [dict(name=f"{L['setup']} 20-bar breakout {fmt(prev, digits)}", top=prev + 0.1 * s.atr_now,
                         bot=prev - 0.1 * s.atr_now)] if close_to else []
            anchor_m = prev - 0.4 * s.atr_now if d == 1 else prev + 0.4 * s.atr_now
            cands.append(evaluate(
                "Momentum break", d, bias_pts - 5, mpoi, anchor_m,
                need_trig_kinds=("structure", "retest", "candle"),
                extra_checks=[(f"{L['setup']} fresh breakout", close_to,
                               f"last {L['setup']} candle closed beyond {fmt(prev, digits)}, price still close to it" if close_to
                               else f"no fresh {L['setup']} breakout close to price")]))

    # ---------------- pick the best ----------------
    valid = sorted([c for c in cands if c["valid"]], key=lambda c: c["score"], reverse=True)
    best = valid[0] if valid else None
    result = dict(
        symbol=symbol, entry_tf=entry_tf, price=round(price, digits), digits=digits,
        time_utc=now.strftime("%Y-%m-%d %H:%M"),
        stack=dict(top=L["top"], mid=L["mid"], setup=L["setup"], entry=L["entry"]),
        bias=side_word(bias_d), bias_text=bias_txt, ladder=ladder,
        regime=s_regime, session=sess_txt, premium_discount=round(pd_pos, 2),
        warnings=warnings, reasons=[],
        candidates=[dict(name=c["name"], side="BUY" if c["side"] == 1 else "SELL", score=c["score"],
                         grade=c["grade"], valid=c["valid"], passed=c["passed"], total=len(c["checks"]),
                         fails=c["fails"]) for c in sorted(cands, key=lambda c: (c["valid"], c["passed"], c["score"]), reverse=True)],
        votes=dict(setup=s_votes, entry=e_votes),
    )

    def pack_levels(lv, d):
        lots, note, risk_amt = se.lot_size(symbol, lv["entry"], lv["dist"], balance, risk_pct, cent)
        if note:
            warnings.append(note)
        return dict(entry=round(lv["entry"], digits), stop_loss=round(lv["sl"], digits),
                    take_profit_1=round(lv["tp1"], digits), take_profit_2=round(lv["tp2"], digits),
                    sl_distance=round(lv["dist"], digits),
                    rr_tp1=f"1:{MIN_RR:g}", rr_tp2=f"1:{abs(lv['tp2'] - lv['entry']) / lv['dist']:.1f}",
                    lot_size=lots, risk_amount=round(risk_amt, 2))

    if hard:
        result.update(status="NO TRADE", headline="Stand aside")
        result["reasons"] = hard
        focus = None
    elif best and best["grade"] in TRADE_GRADES:
        d = best["side"]
        lv = best["levels"]
        tg = best["trigger"]
        inval = lv["sl"]
        result.update(
            status="TRADE", side="BUY" if d == 1 else "SELL",
            headline=f"{'BUY' if d == 1 else 'SELL'} now", playbook=best["name"], grade=best["grade"], score=best["score"],
            trade=pack_levels(lv, d),
            key=f"{symbol}|{best['name']}|{d}|{e.idx[tg['bar']].isoformat() if tg else ''}",
            management=[
                f"At TP1 ({fmt(lv['tp1'], digits)}) close half and move the stop to entry ({fmt(price, digits)}).",
                f"Exit early if a {L['entry']} candle closes back {'below' if d == 1 else 'above'} "
                f"{fmt(tg['fail'] if tg else price, digits)} before TP1 - the trigger failed.",
                f"The idea is wrong if price trades {'below' if d == 1 else 'above'} {fmt(inval, digits)} (your stop).",
                "Do not add to the position or move the stop further away.",
            ],
            confluence=best["agree_names"], agreement=round(100 * best["agreement"]),
        )
        focus = best
        others = [c for c in valid[1:] if c["side"] == d]
        if others:
            result["also_valid"] = [f"{c['name']} ({c['grade']})" for c in others]
    else:
        focus = best or max(cands, key=lambda c: (c["passed"], c["score"]), default=None)
        result.update(status="WAIT" if bias_d or s_regime == "RANGING" else "NO TRADE",
                      headline="Wait" if bias_d or s_regime == "RANGING" else "No trade")
        if best:
            result["reasons"].append(f"Best setup is {best['name']} grade {best['grade']} ({best['score']}/100) - "
                                     f"only A and A+ setups are traded.")
        elif focus:
            result["reasons"] += focus["fails"][:4]
        if focus and focus["chasing"]:
            result["headline"] = "Don't chase"
        if not bias_d and s_regime != "RANGING":
            result["reasons"].insert(0, f"No top-down direction: {bias_txt}.")
        # wait plan - where to wait and what to wait for
        plan = wait_plan(symbol, bias_d or (focus["side"] if focus else 0), price, s, m, t, e, setup_tf, mid_tf,
                         digits, L, balance, risk_pct, cent)
        if plan:
            result["plan"] = plan
    if focus:
        result["focus"] = dict(name=focus["name"], side="BUY" if focus["side"] == 1 else "SELL",
                               grade=focus["grade"], score=focus["score"], checks=focus["checks"])
    return result


def wait_plan(symbol, d, price, s, m, t, e, setup_tf, mid_tf, digits, L, balance, risk_pct, cent):
    """Best zone ahead of price in the bias direction, and what to wait for there."""
    if not d:
        return None
    a = s.atr_now
    zs = zones(s, d, setup_tf) + zones(m, d, mid_tf)
    if d == 1:
        ahead = [z for z in zs if price - 3 * a < z["top"] < price - 0.2 * a]
    else:
        ahead = [z for z in zs if price + 0.2 * a < z["bot"] < price + 3 * a]
    if not ahead:
        return None
    scored = []
    for z in ahead:
        grp = [y for y in ahead if y["bot"] <= z["top"] + 0.2 * a and y["top"] >= z["bot"] - 0.2 * a]
        edge = z["top"] if d == 1 else z["bot"]
        scored.append((len({y["name"] for y in grp}), -abs(price - edge), z, grp))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    conf, _, z, grp = scored[0]
    top, bot = z["top"], z["bot"]                       # the zone itself; overlaps add confluence
    names = sorted({y["name"] for y in grp})
    entry = top if d == 1 else bot
    far = bot if d == 1 else top
    lv = build_levels(symbol, d, entry, far, s, e, m, t, digits)
    lots, note, risk_amt = se.lot_size(symbol, entry, lv["dist"], balance, risk_pct, cent)
    word = "buy" if d == 1 else "sell"
    return dict(
        side="BUY" if d == 1 else "SELL", zone_top=round(top, digits), zone_bot=round(bot, digits),
        zone=" + ".join(names), confluence=len(names),
        distance=round(abs(price - entry), digits),
        wait_for=f"Let price pull back into {fmt(bot, digits)} - {fmt(top, digits)}, then wait for a {L['entry']} "
                 f"CHoCH or rejection candle there. Watch mode will alert you when it becomes an A/A+ {word}.",
        limit=None if lv["problems"] else dict(
            entry=round(entry, digits), stop_loss=round(lv["sl"], digits),
            take_profit_1=round(lv["tp1"], digits), take_profit_2=round(lv["tp2"], digits),
            lot_size=lots, risk_amount=round(risk_amt, 2)),
        limit_note="; ".join(lv["problems"]),
    )
