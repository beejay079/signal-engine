"""
topdown.py - proper top-down analysis before ANY order.

  Step 1  BIAS      Daily + 4H must point the same way (or one neutral). Disagree -> no trade.
  Step 2  LOCATION  1H must not trend against the bias, and price must be in the right half of
                    the 1H range: buy in DISCOUNT (lower ~60%), sell in PREMIUM. No buying tops.
  Step 3  SETUP     15m must not be against the bias, and price must have PULLED BACK into the
                    lower part of the 15m leg (buys) / upper part (sells) or into a 15m zone.
  Step 4  ENTRY     5m (or 1m) gives a valid zone entry (entry_rules: zone trigger, no chasing).
  Step 5  RISK      SL beyond the 15m pullback swing (not just the entry candles), TP1 = 1:2
                    with no 1H/4H level in the way.

All 5 pass -> market order.  Steps 1-2 pass but 3-4 not yet -> limit order at a 15m zone that
sits in the right half of the 1H range.  Step 1 or 2 fails -> no trade, with the reason.
"""
import numpy as np

import entry_rules
import limit_orders as lo
import signal_engine as se

# timeframe stack for each entry timeframe
STACKS = {
    "1min":  dict(bias=["4h"], location="1h", setup="15min", entry="1min"),
    "5min":  dict(bias=["1day", "4h"], location="1h", setup="15min", entry="5min"),
    "15min": dict(bias=["1day", "4h"], location="1h", setup="15min", entry="15min"),
    "1h":    dict(bias=["1day"], location="4h", setup="1h", entry="1h"),
}
# how often each timeframe's candles are refreshed (minutes) - higher TFs change slowly
REFRESH = {"1day": 240, "4h": 60, "1h": 30, "15min": 15, "5min": 5, "1min": 1}
ROLE = {"bias": "Bias", "location": "Location", "setup": "Setup", "entry": "Entry"}
LABEL = {"1day": "Daily", "4h": "4H", "1h": "1H", "15min": "15m", "5min": "5m", "1min": "1m"}
DISCOUNT_MAX = 0.60       # buys only in the lower 60% of the 1H / 15m range (sells: upper 60%)
SETUP_SWING_BARS = 4      # SL goes beyond the low/high of the last 4 setup-TF candles
MAX_SL_SETUP_ATR = 1.5    # ... but never more than 1.5 ATR of the setup timeframe
LIMIT_EXPIRY_SETUP_BARS = 8


def frames_needed(entry_tf):
    st = STACKS[entry_tf]
    tfs = st["bias"] + [st["location"], st["setup"], st["entry"]]
    return list(dict.fromkeys(tfs))


def refresh_minutes(tf, gold=True):
    m = REFRESH.get(tf, 60)
    return m if gold or tf in ("15min", "5min", "1min") else max(m, 60)


def _dir_word(d):
    return {1: "Bullish", -1: "Bearish", 0: "Neutral"}[d]


def read(c, price):
    """Trend + structure + where price sits in the latest swing range."""
    i = c.i
    t = 0
    if c.ema50[i] > c.ema200[i] and c.c[i] > c.ema50[i]:
        t = 1
    elif c.ema50[i] < c.ema200[i] and c.c[i] < c.ema50[i]:
        t = -1
    s = int(c.structure)
    d = s if t in (0, s) else (t if s == 0 else 0)
    pos = None
    if c.sh and c.sl:
        H, L = float(c.h[c.sh[-1]]), float(c.l[c.sl[-1]])
        if H > L:
            pos = float(np.clip((price - L) / (H - L), 0, 1))
    brk = c.last_break[1] if c.last_break else None
    return dict(dir=d, trend=t, structure=s, pos=pos, last_break=brk, atr=c.atr_now)


def _favourable(pos, d):
    """Discount for buys, premium for sells."""
    if pos is None:
        return True
    return pos <= DISCOUNT_MAX if d == 1 else pos >= 1 - DISCOUNT_MAX


def _levels_against(ctxs, d, price, min_gap):
    out = []
    for c in ctxs:
        if d == 1:
            out += list(c.resistances) + [c.prev_day_high] + ([c.bear_ob[0]] if c.bear_ob else [])
        else:
            out += list(c.supports) + [c.prev_day_low] + ([c.bull_ob[1]] if c.bull_ob else [])
    return [x for x in out if x and (x - price) * d > min_gap]


def analyze(symbol, entry_tf, frames, balance, risk, cent=False):
    st = STACKS[entry_tf]
    entry_df = frames[entry_tf]
    price = float(entry_df["close"].iloc[-1])
    cfg = se.SYMBOLS.get(symbol, {})
    digits, spread = cfg.get("digits", 5), cfg.get("spread", 0.0)
    fmt = lambda x: f"{x:.{digits}f}"

    # engine on the entry timeframe, filtered by its usual higher timeframe
    r = se.generate_signal(symbol, entry_tf, balance, risk, cent=cent, df=entry_df,
                           htf_df=frames[se.HIGHER_TF[entry_tf]])
    entry_rules.validate(r, symbol, entry_tf, entry_df, balance, risk, cent)

    ctx = {tf: se.Ctx(frames[tf].iloc[:-1], tf) for tf in frames_needed(entry_tf)}
    rd = {tf: read(c, price) for tf, c in ctx.items()}
    steps = []

    # ---- 1. bias
    dirs = [rd[tf]["dir"] for tf in st["bias"]]
    nz = [x for x in dirs if x]
    bias = 0 if (not nz or len(set(nz)) > 1) else nz[0]
    for tf in st["bias"]:
        x = rd[tf]
        steps.append(dict(tf=LABEL[tf], role="Bias", dir=_dir_word(x["dir"]), ok=bias != 0 and x["dir"] != -bias,
                          detail=f"structure {_dir_word(x['structure']).lower()}"
                                 f"{' (' + x['last_break'] + ')' if x['last_break'] else ''}, EMA trend {_dir_word(x['trend']).lower()}"))
    bias_ok = bias != 0

    # ---- 2. location
    L = rd[st["location"]]
    loc_ok = bias_ok and L["dir"] != -bias and _favourable(L["pos"], bias)
    zone_word = "discount" if bias == 1 else "premium"
    steps.append(dict(tf=LABEL[st["location"]], role="Location", dir=_dir_word(L["dir"]), ok=loc_ok,
                      detail=(f"price at {L['pos'] * 100:.0f}% of the range" if L["pos"] is not None else "no clear range")
                             + (f" - needs {zone_word}" if bias else "")))

    # ---- 3. setup (pullback on 15m)
    S = rd[st["setup"]]
    setup_ok = loc_ok and S["dir"] != -bias and _favourable(S["pos"], bias)
    steps.append(dict(tf=LABEL[st["setup"]], role="Setup", dir=_dir_word(S["dir"]), ok=setup_ok,
                      detail=(f"pulled back to {S['pos'] * 100:.0f}% of the leg" if S["pos"] is not None else "no leg")
                             + (" - pullback done" if setup_ok else " - wait for the pullback")))

    # ---- 4. entry
    want = {1: "BUY", -1: "SELL"}.get(bias)
    entry_ok = setup_ok and r.get("signal") == want
    if r.get("signal") in ("BUY", "SELL"):
        e_detail = f"{r['signal']} at {r.get('entry_zone', '')}"
    elif r.get("rejected_signal"):
        e_detail = f"{r['rejected_signal']} rejected: {r['reasons'][0] if r.get('reasons') else ''}"
    else:
        e_detail = "no zone reaction yet"
    if st["entry"] != st["setup"]:
        steps.append(dict(tf=LABEL[st["entry"]], role="Entry", dir=r.get("signal", "NO TRADE").title(),
                          ok=entry_ok, detail=e_detail))
    else:
        steps[-1]["detail"] += f"; entry: {e_detail}"
        steps[-1]["ok"] = steps[-1]["ok"] and entry_ok

    reasons = []
    if not bias_ok:
        reasons.append("Higher timeframes disagree or are neutral - sit out, no order.")
    elif not loc_ok:
        if L["dir"] == -bias:
            reasons.append(f"{LABEL[st['location']]} is trending against the {_dir_word(bias).lower()} bias - wait until it turns.")
        else:
            reasons.append(f"Price is at {L['pos'] * 100:.0f}% of the {LABEL[st['location']]} range - not in {zone_word}. "
                           f"{'Buying high' if bias == 1 else 'Selling low'} gets stopped; wait for the pullback.")
    sc, a_set = ctx[st["setup"]], ctx[st["setup"]].atr_now
    htf_ctxs = [ctx[tf] for tf in st["bias"]] + [ctx[st["location"]]]

    # ---- 5. risk on a full pass: SL beyond the setup-TF pullback swing, room on 1H/4H
    if entry_ok:
        d = bias
        i = sc.i
        swing = float(sc.l[i - SETUP_SWING_BARS + 1:i + 1].min()) if d == 1 else float(sc.h[i - SETUP_SWING_BARS + 1:i + 1].max())
        sl_setup = swing - d * (0.1 * a_set + spread)
        use_setup = (price - sl_setup) * d > r["sl_distance"]
        dist = (price - sl_setup) * d if use_setup else r["sl_distance"]
        if dist > MAX_SL_SETUP_ATR * a_set:
            entry_ok = False
            reasons.append(f"Stop beyond the {LABEL[st['setup']]} pullback would be too wide - wait for a limit at the zone.")
        else:
            tp1, tp2 = price + d * 2 * dist, price + d * 3 * dist
            obs = _levels_against(htf_ctxs + [sc], d, price, 0.3 * ctx[entry_tf].atr_now)
            near = min(obs, key=lambda x: abs(x - price)) if obs else None
            if near is not None and (near - tp1) * d < 0:
                entry_ok = False
                reasons.append(f"Higher-timeframe level at {fmt(near)} blocks TP1 (1:2).")
            else:
                if near is not None and (near - tp2) * d < 0:
                    tp2 = near - d * 0.1 * ctx[entry_tf].atr_now
                lots, note, risk_amt = se.lot_size(symbol, price, dist, balance, risk, cent)
                r["warnings"] = [w for w in r.get("warnings", []) if "Min lot" not in w and "contract size" not in w]
                if note:
                    r["warnings"].append(note)
                r.update(stop_loss=round(price - d * dist, digits), take_profit_1=round(tp1, digits),
                         take_profit_2=round(tp2, digits), rr_tp2=f"1:{abs(tp2 - price) / dist:.2g}",
                         sl_distance=round(dist, digits), lot_size=lots, risk_amount=round(risk_amt, 2),
                         sl_reason=(f"Beyond the {LABEL[st['setup']]} pullback {'low' if d == 1 else 'high'} {fmt(swing)}"
                                    if use_setup else r.get("sl_reason", "")))
        steps.append(dict(tf="Risk", role="Risk", dir="1:2" if entry_ok else "-", ok=entry_ok,
                          detail=r.get("sl_reason", "") if entry_ok else (reasons[-1] if reasons else "")))

    if not entry_ok and r.get("signal") in ("BUY", "SELL"):
        r["rejected_signal"] = r["signal"]
        r["signal"] = "NO TRADE"
        for k in entry_rules.TRADE_KEYS + ("entry_zone", "sl_reason", "invalidation"):
            r.pop(k, None)
        r["warnings"] = [w for w in r.get("warnings", []) if "Min lot" not in w]

    # ---- limit order at a setup-TF zone when bias + location are good
    r["limit_orders"], r["limit_note"] = [], ""
    if r["signal"] == "NO TRADE" and loc_ok and r.get("regime") != "VOLATILE":
        d = bias
        Lc = ctx[st["location"]]

        def accept(entry, sl, tp1, tp2):
            if Lc.sh and Lc.sl:
                H, Lo = float(Lc.h[Lc.sh[-1]]), float(Lc.l[Lc.sl[-1]])
                if H > Lo and not _favourable((entry - Lo) / (H - Lo), d):
                    return False, tp2, f"zone is not in the 1H {zone_word}"
            obs = _levels_against(htf_ctxs, d, max(entry, price) if d == 1 else min(entry, price), 0.3 * a_set)
            near = min(obs, key=lambda x: abs(x - entry)) if obs else None
            if near is not None and (near - tp1) * d < 0:
                return False, tp2, f"1H/4H level at {fmt(near)} blocks TP1"
            if near is not None and (near - tp2) * d < 0:
                tp2 = near - d * 0.1 * a_set
            return True, tp2, ""

        plan, err = lo._plan(sc, symbol, d, price, balance, risk, cent, digits, accept=accept)
        if plan:
            plan["expires_in_minutes"] = LIMIT_EXPIRY_SETUP_BARS * sc.minutes
            plan["bias"] = f"top-down {want}: " + " / ".join(f"{s['tf']} {s['dir'].lower()}" for s in steps[:len(st['bias']) + 1])
            plan["topdown_ok"] = True
            plan["zone"] = f"{LABEL[st['setup']]} " + plan["zone"]
            r["limit_orders"].append(plan)
        else:
            r["limit_note"] = f"No limit order: {err}."
    elif r["signal"] == "NO TRADE":
        r["limit_note"] = reasons[0] if reasons else ""

    if r["signal"] == "NO TRADE":
        r["reasons"] = reasons + [x for x in r.get("reasons", []) if x not in reasons]
        if bias_ok and loc_ok and not r["limit_orders"] and not r.get("reasons"):
            r["reasons"] = ["Waiting for the 15m pullback and a 5m zone entry."]
    passed = sum(1 for s_ in steps if s_["ok"])
    r["topdown"] = dict(bias=_dir_word(bias), steps=steps, passed=passed, total=len(steps),
                        perfect=r["signal"] in ("BUY", "SELL"),
                        stack=" → ".join(LABEL[t] for t in frames_needed(entry_tf)))
    r["topdown_dirs"] = {tf: rd[tf]["dir"] for tf in rd}        # used by trade management
    return r
