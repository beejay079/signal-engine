"""
entry_rules.py - only VALID entries. Applied to every BUY/SELL from signal_engine.

A market entry is allowed only if ALL of these are true:
  1. A ZONE trigger fired: order block, fair value gap, Fibonacci 50-61.8%, support/resistance
     bounce, breakout & retest, liquidity sweep, or pin bar/engulfing at a level.
     Breakout-only triggers (Turtle, session breakouts, squeeze, inside bar) and indicator
     triggers (RSI, Stochastic, Bollinger, VWAP) are not entries on their own.
  2. Not chasing: price is no more than 1.5 ATR past the 20 EMA.
  3. Not late: price has moved less than 0.5 ATR beyond the close of the trigger candle.
  4. Stop loss sits beyond the pullback swing (lowest low / highest high of the last
     5 closed candles) + 0.2 ATR buffer + spread - outside normal pullback noise.
     Never tighter than 0.8 ATR; if the swing is more than 3 ATR away the trade is skipped.
  5. TP1 = 1:2, TP2 = 1:3, and no strong level blocks the way to TP1.

If a rule fails, the signal becomes NO TRADE and the limit-order planner offers the
pullback entry at the zone instead.
"""
import signal_engine as se

ZONE_TRIGGERS = {
    "Order block", "Fair value gap", "Fibonacci golden zone", "Support/Resistance bounce",
    "Breakout & retest", "Liquidity sweep", "Pin bar / Engulfing",
}
MAX_EXTENSION_ATR = 1.5
MAX_DRIFT_ATR = 0.5
SWING_BARS = 5
BUFFER_ATR = 0.2
MIN_SL_ATR = 0.8
MAX_SL_ATR = 3.0
TRADE_KEYS = ("entry", "stop_loss", "take_profit_1", "take_profit_2", "rr_tp1", "rr_tp2",
              "sl_distance", "lot_size", "risk_amount")


def validate(result, symbol, interval, df, balance, risk, cent=False):
    if result.get("signal") not in ("BUY", "SELL"):
        return result
    d = 1 if result["signal"] == "BUY" else -1
    c = se.Ctx(df.iloc[:-1], interval)
    a, i = c.atr_now, c.i
    price = float(df["close"].iloc[-1])
    cfg = se.SYMBOLS.get(symbol, {})
    digits, spread = cfg.get("digits", 5), cfg.get("spread", 0.0)
    fmt = lambda x: f"{x:.{digits}f}"
    why = []

    zones = [t for t in result.get("triggers", []) if t in ZONE_TRIGGERS]
    if not zones:
        why.append(f"Breakout/indicator trigger only ({', '.join(result.get('triggers', []))}) - "
                   "no valid zone. Wait for the pullback to a zone.")
    ext = (price - c.ema20[i]) * d / a
    if ext > MAX_EXTENSION_ATR:
        why.append(f"Chasing: price is {ext:.1f} ATR past the 20 EMA. Wait for the pullback.")
    drift = (price - c.c[i]) * d / a
    if drift > MAX_DRIFT_ATR:
        why.append(f"Entry missed: price already moved {drift:.1f} ATR from the trigger candle.")

    if not why:
        if d == 1:
            swing = float(c.l[i - SWING_BARS + 1:i + 1].min())
            sl = swing - BUFFER_ATR * a - spread
            dist = price - sl
        else:
            swing = float(c.h[i - SWING_BARS + 1:i + 1].max())
            sl = swing + BUFFER_ATR * a + spread
            dist = sl - price
        if dist > MAX_SL_ATR * a:
            why.append(f"Pullback swing is {dist / a:.1f} ATR away - stop would be too wide.")
        else:
            dist = max(dist, MIN_SL_ATR * a)
            sl = price - d * dist
            tp1, tp2 = price + d * 2 * dist, price + d * 3 * dist
            if d == 1:
                obs = list(c.resistances) + [c.prev_day_high] + ([c.bear_ob[0]] if c.bear_ob else [])
            else:
                obs = list(c.supports) + [c.prev_day_low] + ([c.bull_ob[1]] if c.bull_ob else [])
            obs = [x for x in obs if x and (x - price) * d > 0.3 * a]
            near = min(obs, key=lambda x: abs(x - price)) if obs else None
            if near is not None and (near - tp1) * d < 0:
                why.append(f"No room for 1:2 with a safe stop - level at {fmt(near)} is before TP1.")
            else:
                if near is not None and (near - tp2) * d < 0:
                    tp2 = near - d * 0.1 * a
                lots, note, risk_amt = se.lot_size(symbol, price, dist, balance, risk, cent)
                result["warnings"] = [w for w in result.get("warnings", []) if "Min lot" not in w and "contract size" not in w]
                if note:
                    result["warnings"].append(note)
                result.update(
                    entry=round(price, digits), stop_loss=round(sl, digits),
                    take_profit_1=round(tp1, digits), take_profit_2=round(tp2, digits),
                    rr_tp1="1:2", rr_tp2=f"1:{abs(tp2 - price) / dist:.2g}",
                    sl_distance=round(dist, digits), lot_size=lots, risk_amount=round(risk_amt, 2),
                    entry_zone=" + ".join(zones),
                    invalidation=f"Exit if a candle closes {'below' if d == 1 else 'above'} {fmt(sl)}",
                    sl_reason=f"Beyond the pullback {'low' if d == 1 else 'high'} {fmt(swing)} + buffer",
                )
                return result

    # rejected -> no market entry; the limit planner will offer the zone entry
    result["rejected_signal"] = result["signal"]
    result["signal"] = "NO TRADE"
    result["reasons"] = why
    for k in TRADE_KEYS:
        result.pop(k, None)
    result["warnings"] = [w for w in result.get("warnings", []) if "Min lot" not in w]
    return result
