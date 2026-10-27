"""
manage.py - HOLD or WITHDRAW alerts for trades in progress (the site's setups and your own trades).

Event alerts (each sent once):
  ✅ Limit filled            -> you are in, HOLD
  🔒 Reached +1R             -> move SL to breakeven
  🎯 TP1 hit                 -> close half / move SL to entry, hold for TP2
  ❌ Stopped out             -> setup failed, no revenge trade
  ⌛ Limit expired / missed / zone broken -> cancel the pending order in MT5
  ⚠️ WITHDRAW               -> 15m structure or 1H trend turned against you, or no momentum

Status updates (Gold, every 15 min while a trade is open):
  HOLD ✅      structure still with you
  CAUTION ⚠️  close to SL or 15m turned neutral
  WITHDRAW ❌  15m/1H turned against you or the bias flipped
"""
import time

import topdown as td

STALL_SETUP_BARS = 6           # 6 x 15m = 90 min with no progress -> consider closing


def _fmt(x, price):
    return f"{x:.5f}" if price < 50 else f"{x:.2f}"


def _assess(o, r, tf):
    """Return (verdict, progress_R, list of (key, reason) withdraw warnings)."""
    d = 1 if o["side"].startswith("BUY") else -1
    price = float(r["price"])
    risk_d = abs(o["entry"] - o["stop_loss"]) or 1e-9
    prog = (price - o["entry"]) * d / risk_d
    dirs = r.get("topdown_dirs", {})
    st = td.STACKS.get(tf) or td.STACKS["5min"]
    setup_dir, loc_dir = dirs.get(st["setup"], 0), dirs.get(st["location"], 0)
    bias = {"Bullish": 1, "Bearish": -1}.get(r.get("topdown", {}).get("bias"), 0)
    warns = []
    if setup_dir == -d and prog < 0.5:
        warns.append(("w_setup", f"{td.LABEL[st['setup']]} structure turned {'bearish' if d == 1 else 'bullish'} against your {o['side'].split()[0]}"))
    if loc_dir == -d:
        warns.append(("w_loc", f"{td.LABEL[st['location']]} trend turned against your trade"))
    if bias == -d:
        warns.append(("w_bias", "the higher-timeframe bias flipped against your trade"))
    opened = o.get("filled_at") or o.get("created") or time.time()
    if time.time() - opened > STALL_SETUP_BARS * td.REFRESH.get(st["setup"], 15) * 60 and prog < 0.3:
        warns.append(("w_stall", f"no momentum after {STALL_SETUP_BARS * td.REFRESH.get(st['setup'], 15)} min"))
    if prog <= -1:
        warns.append(("w_sl", "price is at or beyond your stop loss - check MT5"))
    if warns:
        verdict = "WITHDRAW ❌"
    elif prog <= -0.6 or setup_dir == 0:
        verdict = "CAUTION ⚠️"
    else:
        verdict = "HOLD ✅"
    return verdict, prog, warns


def check(name, symbol, tf, setup, r, updates_every=15):
    """Look at one setup (site setup or your own trade). Returns a list of messages to push."""
    msgs = []
    if not setup:
        return msgs
    price = float(r["price"])
    f = lambda x: _fmt(x, price)
    for o in setup["orders"]:
        ev = o.setdefault("events", [])
        side = o["side"].split()[0]
        head = f"{name} {o['side']} @ {f(o['entry'])}"

        def once(key, title, body, prio=4, tags=None):
            if key not in ev:
                ev.append(key)
                msgs.append((title, body, prio, tags or []))

        st = o["status"]
        if o["kind"] == "limit" and st in ("FILLED", "TP1 HIT", "STOPPED"):
            once("filled", f"✅ Filled — {head}",
                 f"You are in the trade. HOLD.\nSL {f(o['stop_loss'])} · TP1 {f(o['take_profit_1'])} · TP2 {f(o['take_profit_2'])}", 5, ["white_check_mark"])
        if st == "TP1 HIT":
            once("tp1", f"🎯 TP1 hit — {head}",
                 f"Take profit on half (or all). Move SL to entry {f(o['entry'])} and hold the rest for TP2 {f(o['take_profit_2'])}.", 5, ["dart"])
        elif st == "STOPPED":
            once("stopped", f"❌ Stopped out — {head}",
                 "The setup failed. No revenge trade - wait for the next top-down setup.", 4, ["x"])
        elif st in ("EXPIRED", "MISSED", "INVALID"):
            why = {"EXPIRED": "was not filled in time", "MISSED": "price ran to target without filling",
                   "INVALID": "price broke the zone before filling"}[st]
            once("dead", f"⌛ Cancel your pending order — {head}", f"The limit {why}. Delete it in MT5.", 4, ["hourglass"])
        elif st == "CANCELLED":
            once("cancel", f"⌛ Cancel the other limit — {head}", "The opposite side filled. Delete this pending order in MT5.", 4, ["hourglass"])
        if st not in ("FILLED", "IN TRADE"):
            continue

        verdict, prog, warns = _assess(o, r, tf)
        if prog >= 1.0:
            once("be", f"🔒 +1R reached — {head}",
                 f"Move your SL to breakeven ({f(o['entry'])}). Price {f(price)}.", 4, ["lock"])
        for key, reason in warns:
            once(key, f"⚠️ WITHDRAW? — {head}",
                 f"{reason[0].upper() + reason[1:]}.\nPrice {f(price)} ({prog:+.1f}R). Close now or cut the size. SL {f(o['stop_loss'])}.",
                 5, ["warning"])
        o.setdefault("last_update", time.time())        # first status update 15 min after entering
        if updates_every and time.time() - o["last_update"] >= updates_every * 60:
            o["last_update"] = time.time()
            if "update0" in ev or o["kind"] != "limit" or "filled" in ev:
                ev.append("update0")
                msgs.append((f"{verdict} {name} {side} — {prog:+.1f}R",
                             f"Price {f(price)} · entry {f(o['entry'])} · SL {f(o['stop_loss'])} · TP1 {f(o['take_profit_1'])}\n"
                             + (", ".join(w[1] for w in warns) + "." if warns else
                                "Structure still with you." if verdict.startswith("HOLD") else "Watch closely."),
                             3, ["chart_with_upwards_trend" if side == "BUY" else "chart_with_downwards_trend"]))
        o["advice"] = dict(verdict=verdict, progress=round(prog, 2), warnings=[w[1] for w in warns], time=int(time.time()))
    return msgs
