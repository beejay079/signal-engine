"""
limit_orders.py - when there's no market entry, plan a LIMIT order instead.

Uses the same candles and engine (signal_engine.py is not modified):
  1. Bias: the strategies' weighted vote, else the higher-timeframe trend.
     If they disagree -> no limit (mixed market).
     If no bias and the market is RANGING -> buy limit at support AND sell limit at resistance.
  2. Zones in the bias direction, close to price (scalper-friendly):
       order block, unfilled fair value gap, support/resistance cluster,
       previous-day high/low, 50-61.8% Fibonacci zone.
     Zones that overlap are merged into "confluence" - more overlap = better.
  3. Entry at the first touch of the zone, SL beyond the zone, TP1 = 1:2, TP2 = 1:3
     (capped before the next strong level). Rejected if a level blocks TP1.
  4. Expires after EXPIRY_BARS candles - cancel if not filled.
"""
import signal_engine as se

EXPIRY_BARS = 15        # 1m -> 15 min, 5m -> 75 min, 15m -> 3h45
MIN_DIST_ATR = 0.3      # zone must be at least this far from price
MAX_DIST_ATR = 4.0      # ...and no further than this (no long waits)
MIN_BIAS = 0.60         # weighted agreement needed to call a bias


def _bias(result):
    act = [v for v in result.get("votes", []) if v["active"]]
    bw = sum(v["weight"] for v in act if v["vote"] == 1)
    sw = sum(v["weight"] for v in act if v["vote"] == -1)
    vote_dir, agree = 0, 0.0
    if bw + sw > 0:
        vote_dir = 1 if bw > sw else -1 if sw > bw else 0
        agree = max(bw, sw) / (bw + sw)
    if agree < MIN_BIAS:
        vote_dir = 0
    htf = {"BULLISH": 1, "BEARISH": -1}.get(result.get("htf_bias"), 0)
    if vote_dir and htf == -vote_dir:
        return 0, f"strategies say {'BUY' if vote_dir == 1 else 'SELL'} but {result['htf']} trend is against it"
    if vote_dir:
        return vote_dir, f"{agree:.0%} of strategies"
    if htf:
        return htf, f"{result['htf']} trend"
    return 0, "no clear bias"


def _zones(c, d, price):
    """Candidate zones (name, top, bottom) on the side we want to enter from."""
    a, i = c.atr_now, c.i
    z = []
    if d == 1:
        if c.bull_ob:
            z.append(("Bullish order block", c.bull_ob[1], c.bull_ob[0]))
        found = 0
        for j in range(i - 1, max(i - 60, 2), -1):
            if c.l[j] > c.h[j - 2] and c.l[j + 1:i + 1].min(initial=float("inf")) > c.h[j - 2]:
                z.append(("Bullish FVG", c.l[j], c.h[j - 2]))
                found += 1
                if found == 3:
                    break
        z += [("Support", s + 0.15 * a, s - 0.15 * a) for s in c.supports]
        if c.prev_day_low:
            z.append(("Previous day low", c.prev_day_low + 0.15 * a, c.prev_day_low - 0.15 * a))
        if c.sh and c.sl and c.sh[-1] > c.sl[-1]:
            H, L = c.h[c.sh[-1]], c.l[c.sl[-1]]
            z.append(("Fib 50-61.8%", H - 0.5 * (H - L), H - 0.618 * (H - L)))
        lo_ok, hi_ok = price - MAX_DIST_ATR * a, price - MIN_DIST_ATR * a
        return [x for x in z if x[1] > x[2] and lo_ok < x[1] < hi_ok]
    else:
        if c.bear_ob:
            z.append(("Bearish order block", c.bear_ob[1], c.bear_ob[0]))
        found = 0
        for j in range(i - 1, max(i - 60, 2), -1):
            if c.h[j] < c.l[j - 2] and c.h[j + 1:i + 1].max(initial=float("-inf")) < c.l[j - 2]:
                z.append(("Bearish FVG", c.l[j - 2], c.h[j]))
                found += 1
                if found == 3:
                    break
        z += [("Resistance", r + 0.15 * a, r - 0.15 * a) for r in c.resistances]
        if c.prev_day_high:
            z.append(("Previous day high", c.prev_day_high + 0.15 * a, c.prev_day_high - 0.15 * a))
        if c.sh and c.sl and c.sl[-1] > c.sh[-1]:
            H, L = c.h[c.sh[-1]], c.l[c.sl[-1]]
            z.append(("Fib 50-61.8%", L + 0.618 * (H - L), L + 0.5 * (H - L)))
        lo_ok, hi_ok = price + MIN_DIST_ATR * a, price + MAX_DIST_ATR * a
        return [x for x in z if x[1] > x[2] and lo_ok < x[2] < hi_ok]


def _plan(c, symbol, d, price, balance, risk, cent, digits):
    a = c.atr_now
    spread = se.SYMBOLS.get(symbol, {}).get("spread", 0.0)
    zones = _zones(c, d, price)
    if not zones:
        return None, f"No {'support' if d == 1 else 'resistance'} zone within {MAX_DIST_ATR:g} ATR of price"

    # Score each zone by how many other zones overlap it (confluence), then by closeness
    scored = []
    for name, top, bot in zones:
        group = [x for x in zones if x[2] <= top + 0.3 * a and x[1] >= bot - 0.3 * a]
        names = sorted({x[0] for x in group})
        entry = top if d == 1 else bot                  # first touch of the zone
        far = min(x[2] for x in group) if d == 1 else max(x[1] for x in group)
        scored.append((len(names), -abs(price - entry), entry, far, names))
    scored.sort(reverse=True)

    reason = "Every zone has a level blocking 1:2"
    for conf, _, entry, far, names in scored:
        if d == 1:
            sl = far - 0.2 * a - spread
            dist = min(max(entry - sl, 0.5 * a), 2.5 * a)
            sl = entry - dist
            obstacles = list(c.resistances) + [c.prev_day_high] + ([c.bear_ob[0]] if c.bear_ob else [])
            obstacles = [x for x in obstacles if x and x > max(price, entry) + 0.3 * a]
        else:
            sl = far + 0.2 * a + spread
            dist = min(max(sl - entry, 0.5 * a), 2.5 * a)
            sl = entry + dist
            obstacles = list(c.supports) + [c.prev_day_low] + ([c.bull_ob[1]] if c.bull_ob else [])
            obstacles = [x for x in obstacles if x and x < min(price, entry) - 0.3 * a]
        tp1, tp2 = entry + d * 2 * dist, entry + d * 3 * dist
        nearest = min(obstacles, key=lambda x: abs(x - entry)) if obstacles else None
        if nearest is not None and (nearest - tp1) * d < 0:
            continue                                     # level before TP1 -> try next zone
        if nearest is not None and (nearest - tp2) * d < 0:
            tp2 = nearest - d * 0.1 * a
        lots, note, risk_amt = se.lot_size(symbol, entry, dist, balance, risk, cent)
        return dict(
            side="BUY LIMIT" if d == 1 else "SELL LIMIT",
            entry=round(entry, digits), stop_loss=round(sl, digits),
            take_profit_1=round(tp1, digits), take_profit_2=round(tp2, digits),
            rr_tp1="1:2", rr_tp2=f"1:{abs(tp2 - entry) / dist:.2g}",
            sl_distance=round(dist, digits), distance_from_price=round(abs(price - entry), digits),
            lot_size=lots, risk_amount=round(risk_amt, 2), lot_note=note,
            zone=" + ".join(names), confluence=conf,
            expires_in_minutes=EXPIRY_BARS * c.minutes,
        ), None
    return None, reason


def add_limit_orders(result, symbol, interval, df, balance, risk, cent=False):
    """Adds result['limit_orders'] (list) and result['limit_note'] to a NO TRADE result."""
    result["limit_orders"], result["limit_note"] = [], ""
    if result.get("signal") != "NO TRADE":
        return result
    if result.get("regime") == "VOLATILE":
        result["limit_note"] = "Market too volatile for pending orders - stand aside."
        return result

    c = se.Ctx(df.iloc[:-1], interval)
    price = float(df["close"].iloc[-1])
    digits = se.SYMBOLS.get(symbol, {}).get("digits", 5)
    d, why = _bias(result)

    sides = [d] if d else ([1, -1] if result.get("regime") == "RANGING" else [])
    if not sides:
        result["limit_note"] = f"No limit order: {why}."
        return result

    notes = []
    for side in sides:
        plan, err = _plan(c, symbol, side, price, balance, risk, cent, digits)
        if plan:
            plan["bias"] = why if d else "range: trade both edges, cancel the other when one fills"
            result["limit_orders"].append(plan)
        else:
            notes.append(err)
    if not result["limit_orders"]:
        result["limit_note"] = "No limit order: " + "; ".join(notes) + "."
    return result
