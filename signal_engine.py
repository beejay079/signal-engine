#!/usr/bin/env python3
"""
signal_engine.py  -  Multi-strategy, regime-aware trading signal engine
=======================================================================

What happens when you ask for a signal:
  1. Pulls live candles from Twelve Data (your timeframe + one higher timeframe).
  2. Detects the market regime: TRENDING, RANGING, TRANSITION or VOLATILE.
  3. Runs all 29 strategies. Each one votes BUY (+1), SELL (-1) or NEUTRAL (0).
  4. Only strategies that suit the current regime are allowed to count.
  5. A signal fires only if ALL of these pass:
       - enough strategies agree, and few disagree
       - at least one TRIGGER strategy (a real entry event) agrees
       - the higher timeframe is not against it
       - there is room for at least 1:2 before the next strong level
  6. SL comes from market structure (swing points) + ATR. TP1 = 1:2,
     TP2 = 1:3 (or just before the next strong level). Lot size is
     calculated from your balance and risk %.

Usage
-----
  export TWELVE_DATA_API_KEY=your_key
  python3 signal_engine.py                                  # XAU/USD, 15min
  python3 signal_engine.py --symbol EUR/USD --tf 1h
  python3 signal_engine.py --balance 100 --risk 2 --cent    # Exness cent lots
  python3 signal_engine.py --all                            # scan all 5 pairs
  python3 signal_engine.py --json                           # JSON output
  python3 signal_engine.py --scalp                          # 15min direction + 5min entry
  python3 signal_engine.py --scalp --tf 1min                # 15min direction + 1min entry
  python3 signal_engine.py --demo                           # offline test, fake data

From your own code (FastAPI / bot):
  from signal_engine import generate_signal, generate_scalp_signal
  result = generate_signal("XAU/USD", "15min", balance=100, risk_pct=2)
  scalp  = generate_scalp_signal("XAU/USD", "1min", balance=100, risk_pct=2)

Needs: python3, pandas, numpy.  Not financial advice - backtest and demo first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
# contract = units per 1.00 lot, spread = typical spread in price units
SYMBOLS = {
    "XAU/USD": dict(contract=100, digits=2, spread=0.30, fx_session=True),
    "EUR/USD": dict(contract=100_000, digits=5, spread=0.00010, fx_session=True),
    "GBP/USD": dict(contract=100_000, digits=5, spread=0.00012, fx_session=True),
    "USD/JPY": dict(contract=100_000, digits=3, spread=0.012, fx_session=True),
    "BTC/USD": dict(contract=1, digits=2, spread=15.0, fx_session=False),
}
TF_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "45min": 45,
              "1h": 60, "2h": 120, "4h": 240, "1day": 1440}
HIGHER_TF = {"1min": "15min", "5min": "1h", "15min": "4h", "30min": "4h",
             "45min": "4h", "1h": "4h", "2h": "1day", "4h": "1day", "1day": "1week"}

# Which strategy families are allowed to vote in each regime (and how strongly)
REGIME_WEIGHTS = {
    "TRENDING":   {"trend": 1.0, "breakout": 1.0, "price_action": 1.0, "reversion": 0.0},
    "RANGING":    {"trend": 0.0, "breakout": 0.5, "price_action": 1.0, "reversion": 1.0},
    "TRANSITION": {"trend": 0.7, "breakout": 0.8, "price_action": 1.0, "reversion": 0.7},
}
# How much agreement each regime needs before a signal fires
REGIME_RULES = {
    "TRENDING":   dict(min_votes=5, min_agreement=0.70),
    "RANGING":    dict(min_votes=3, min_agreement=0.70),
    "TRANSITION": dict(min_votes=5, min_agreement=0.80),
}
SWING_K = 3  # bars each side to confirm a swing high/low

# Scalp mode: enter NOW at market, tight structure stop, quick 1:2 target
SCALP = dict(
    higher_tf={"1min": "15min", "5min": "15min", "15min": "1h"},
    swing_lookback=10,      # only the last 10 candles decide the stop
    sl_min_atr=0.5,         # stop never tighter than 0.5 ATR (noise)
    sl_max_atr=1.2,         # stop never wider than 1.2 ATR -> TP1 stays close
    buffer_atr=0.1,         # small buffer beyond the swing
    max_spread_share=0.20,  # reject if spread is >20% of the stop
    tp2_rr=2.5,
)


# --------------------------------------------------------------------------
# DATA
# --------------------------------------------------------------------------
def fetch_candles(symbol: str, interval: str, api_key: str, outputsize: int = 500) -> pd.DataFrame:
    """Live OHLC candles from Twelve Data, oldest first, UTC index."""
    params = dict(symbol=symbol, interval=interval, outputsize=outputsize,
                  apikey=api_key, timezone="UTC", order="ASC")
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=25) as resp:
        data = json.load(resp)
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"Twelve Data error ({symbol} {interval}): {data.get('message', data)}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def demo_candles(interval: str, n: int = 500, seed: int = 7, start_price: float = 2650.0,
                 drift: float = 0.0) -> pd.DataFrame:
    """Fake candles (range then trend) for offline testing only."""
    rng = np.random.default_rng(seed)
    mins = TF_MINUTES.get(interval, 10080)
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor(f"{mins}min"), periods=n, freq=f"{mins}min")
    vol = start_price * 0.0012
    steps = rng.normal(0, vol, n)
    steps[: n // 2] -= np.linspace(0, 0, n // 2)                  # first half: range
    steps[n // 2:] += vol * 0.35 + drift                          # second half: up-trend
    close = start_price + np.cumsum(steps)
    close[: n // 2] = start_price + 6 * vol * np.sin(np.linspace(0, 9, n // 2)) + rng.normal(0, vol, n // 2)
    close[n // 2:] = close[n // 2 - 1] + np.cumsum(steps[n // 2:])
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, vol * 0.6, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.6, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


# --------------------------------------------------------------------------
# INDICATORS
# --------------------------------------------------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def true_range(df):
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up, dn = df["high"].diff(), -df["low"].diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / tr
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0), pdi.fillna(0), mdi.fillna(0)


def supertrend(df, n=10, mult=3.0):
    a = atr(df, n).values
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * a, hl2 - mult * a
    fub, flb = ub.copy(), lb.copy()
    d = np.ones(len(c))
    for i in range(1, len(c)):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        d[i] = 1 if c[i] > fub[i - 1] else (-1 if c[i] < flb[i - 1] else d[i - 1])
    return d


def parabolic_sar(df, step=0.02, max_af=0.2):
    h, l = df["high"].values, df["low"].values
    n = len(h)
    d = np.ones(n)
    sar, ep, af, up = l[0], h[0], step, True
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_af)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, max_af)
        d[i] = 1 if up else -1
    return d


def heikin_ashi(df):
    ha_c = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_o = np.empty(len(df))
    ha_o[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    hc = ha_c.values
    for i in range(1, len(df)):
        ha_o[i] = (ha_o[i - 1] + hc[i - 1]) / 2
    ha_h = np.maximum.reduce([df["high"].values, ha_o, hc])
    ha_l = np.minimum.reduce([df["low"].values, ha_o, hc])
    return ha_o, ha_h, ha_l, hc


def find_swings(h, l, k=SWING_K):
    """Confirmed swing highs/lows (index of the pivot). Confirmed k bars later."""
    highs, lows = [], []
    for i in range(k, len(h) - k):
        if h[i] >= h[i - k:i + k + 1].max() and h[i] > h[i - k:i].max():
            highs.append(i)
        if l[i] <= l[i - k:i + k + 1].min() and l[i] < l[i - k:i].min():
            lows.append(i)
    return highs, lows


def cluster_levels(prices, tol, min_touches=2):
    """Group nearby swing prices into support/resistance levels."""
    out, group = [], []
    for p in sorted(prices):
        if group and p - group[-1] > tol:
            if len(group) >= min_touches:
                out.append(float(np.mean(group)))
            group = []
        group.append(p)
    if len(group) >= min_touches:
        out.append(float(np.mean(group)))
    return out


# --------------------------------------------------------------------------
# CONTEXT - everything the strategies need, computed once
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, df: pd.DataFrame, interval: str):
        self.df, self.interval = df, interval
        self.minutes = TF_MINUTES.get(interval, 1440)
        self.o, self.h = df["open"].values, df["high"].values
        self.l, self.c = df["low"].values, df["close"].values
        self.idx = df.index
        self.i = len(df) - 1
        cl = df["close"]

        self.ema9, self.ema20, self.ema21 = ema(cl, 9).values, ema(cl, 20).values, ema(cl, 21).values
        self.ema50, self.ema200 = ema(cl, 50).values, ema(cl, 200).values
        self.rsi14, self.rsi2 = rsi(cl, 14).values, rsi(cl, 2).values
        self.atr_s = atr(df, 14)
        self.atr = self.atr_s.values
        self.atr_now = float(self.atr[-1])
        self.adx, self.pdi, self.mdi = (x.values for x in adx(df, 14))

        mid, sd = cl.rolling(20).mean(), cl.rolling(20).std()
        self.bb_mid, self.bb_up, self.bb_lo = mid.values, (mid + 2 * sd).values, (mid - 2 * sd).values
        bbw = ((mid + 2 * sd) - (mid - 2 * sd)) / mid
        self.bbw_rank = bbw.rolling(120, min_periods=40).rank(pct=True).values

        lo14, hi14 = df["low"].rolling(14).min(), df["high"].rolling(14).max()
        k = 100 * (cl - lo14) / (hi14 - lo14).replace(0, np.nan)
        self.stoch_k = k.rolling(3).mean().values
        self.stoch_d = k.rolling(3).mean().rolling(3).mean().values

        m = ema(cl, 12) - ema(cl, 26)
        self.macd, self.macd_sig = m.values, ema(m, 9).values

        self.st_dir = supertrend(df)
        self.psar_dir = parabolic_sar(df)
        self.ha = heikin_ashi(df)

        tenkan = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
        kijun = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
        self.tenkan, self.kijun = tenkan.values, kijun.values
        self.span_a = ((tenkan + kijun) / 2).shift(26).values
        self.span_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26).values

        # Session-anchored mean price (real VWAP if volume exists)
        tp = (df["high"] + df["low"] + df["close"]) / 3
        day = df.index.normalize()
        if "volume" in df and df["volume"].fillna(0).sum() > 0:
            v = df["volume"].fillna(0)
            self.vwap = ((tp * v).groupby(day).cumsum() / v.groupby(day).cumsum().replace(0, np.nan)).values
        else:
            self.vwap = tp.groupby(day).expanding().mean().reset_index(level=0, drop=True).values

        # Swings, levels, structure
        self.sh, self.sl = find_swings(self.h, self.l)
        recent = max(0, self.i - 200)
        tol = 0.5 * self.atr_now
        self.resistances = cluster_levels([self.h[j] for j in self.sh if j >= recent], tol)
        self.supports = cluster_levels([self.l[j] for j in self.sl if j >= recent], tol)

        days = sorted(set(day))
        self.prev_day_high = self.prev_day_low = None
        if len(days) >= 2 and self.minutes < 1440:
            prev = df[day == days[-2]]
            self.prev_day_high, self.prev_day_low = float(prev["high"].max()), float(prev["low"].min())

        self._market_structure()

    def _market_structure(self):
        """Walk the chart: track breaks of swing highs/lows (BOS/CHoCH) and order blocks."""
        k = SWING_K
        sh = [(j + k, self.h[j]) for j in self.sh]
        sl = [(j + k, self.l[j]) for j in self.sl]
        last_sh = last_sl = None
        ph = pl = 0
        bias, self.last_break = 0, None
        self.bull_ob = self.bear_ob = None
        for b in range(len(self.c)):
            while ph < len(sh) and sh[ph][0] <= b:
                last_sh, ph = sh[ph], ph + 1
            while pl < len(sl) and sl[pl][0] <= b:
                last_sl, pl = sl[pl], pl + 1
            if last_sh and self.c[b] > last_sh[1]:
                kind = "BOS" if bias == 1 else "CHoCH"
                bias, self.last_break = 1, (b, kind, last_sh[1])
                for j in range(b - 1, max(b - 20, 0) - 1, -1):  # last bearish candle before impulse
                    if self.c[j] < self.o[j]:
                        self.bull_ob = (self.l[j], self.h[j], j)
                        break
                last_sh = None
            if last_sl and self.c[b] < last_sl[1]:
                kind = "BOS" if bias == -1 else "CHoCH"
                bias, self.last_break = -1, (b, kind, last_sl[1])
                for j in range(b - 1, max(b - 20, 0) - 1, -1):  # last bullish candle before impulse
                    if self.c[j] > self.o[j]:
                        self.bear_ob = (self.l[j], self.h[j], j)
                        break
                last_sl = None
            if self.bull_ob and self.c[b] < self.bull_ob[0]:
                self.bull_ob = None
            if self.bear_ob and self.c[b] > self.bear_ob[1]:
                self.bear_ob = None
        self.structure = bias


# --------------------------------------------------------------------------
# STRATEGIES  - each returns (vote, reason).  vote: +1 buy, -1 sell, 0 none
# --------------------------------------------------------------------------
# ---- TREND -----------------------------------------------------------------
def s_ema_cross(c):
    i = c.i
    if c.ema9[i] > c.ema21[i] and c.c[i] > c.ema50[i]:
        return 1, "EMA9 above EMA21, price above EMA50"
    if c.ema9[i] < c.ema21[i] and c.c[i] < c.ema50[i]:
        return -1, "EMA9 below EMA21, price below EMA50"
    return 0, "EMAs mixed"


def s_ema_ribbon(c):
    i = c.i
    if c.ema20[i] > c.ema50[i] > c.ema200[i] and c.ema50[i] > c.ema50[i - 5]:
        return 1, "EMA 20>50>200 stacked and rising"
    if c.ema20[i] < c.ema50[i] < c.ema200[i] and c.ema50[i] < c.ema50[i - 5]:
        return -1, "EMA 20<50<200 stacked and falling"
    return 0, "EMA ribbon not aligned"


def s_golden_cross(c):
    i = c.i
    return (1, "EMA50 above EMA200 (golden)") if c.ema50[i] > c.ema200[i] else (-1, "EMA50 below EMA200 (death)")


def s_supertrend(c):
    return (1, "Supertrend up") if c.st_dir[c.i] > 0 else (-1, "Supertrend down")


def s_ichimoku(c):
    i = c.i
    if np.isnan(c.span_a[i]) or np.isnan(c.span_b[i]):
        return 0, "Not enough data"
    top, bot = max(c.span_a[i], c.span_b[i]), min(c.span_a[i], c.span_b[i])
    if c.c[i] > top and c.tenkan[i] > c.kijun[i]:
        return 1, "Price above cloud, Tenkan > Kijun"
    if c.c[i] < bot and c.tenkan[i] < c.kijun[i]:
        return -1, "Price below cloud, Tenkan < Kijun"
    return 0, "Inside/around cloud"


def s_adx_di(c):
    i = c.i
    if c.adx[i] > 20 and c.pdi[i] > c.mdi[i]:
        return 1, f"ADX {c.adx[i]:.0f}, +DI leads"
    if c.adx[i] > 20 and c.mdi[i] > c.pdi[i]:
        return -1, f"ADX {c.adx[i]:.0f}, -DI leads"
    return 0, f"ADX {c.adx[i]:.0f} weak"


def s_macd(c):
    i = c.i
    hist, prev = c.macd[i] - c.macd_sig[i], c.macd[i - 1] - c.macd_sig[i - 1]
    if hist > 0 and hist >= prev:
        return 1, "MACD above signal, momentum rising"
    if hist < 0 and hist <= prev:
        return -1, "MACD below signal, momentum falling"
    return 0, "MACD momentum fading"


def s_psar(c):
    return (1, "SAR dots below price") if c.psar_dir[c.i] > 0 else (-1, "SAR dots above price")


def s_heikin_ashi(c):
    o, h, l, cl = c.ha
    i, tol = c.i, 1e-9
    bull = all(cl[j] > o[j] for j in range(i - 2, i + 1)) and l[i] >= min(o[i], cl[i]) - tol
    bear = all(cl[j] < o[j] for j in range(i - 2, i + 1)) and h[i] <= max(o[i], cl[i]) + tol
    if bull:
        return 1, "3 bullish HA candles, no lower wick"
    if bear:
        return -1, "3 bearish HA candles, no upper wick"
    return 0, "HA mixed"


# ---- MEAN REVERSION --------------------------------------------------------
def s_bollinger_bounce(c):
    i = c.i
    if c.l[i] < c.bb_lo[i] < c.c[i] and c.c[i] > c.o[i] or (c.c[i - 1] < c.bb_lo[i - 1] and c.c[i] > c.bb_lo[i]):
        return 1, "Rejected lower Bollinger band"
    if c.h[i] > c.bb_up[i] > c.c[i] and c.c[i] < c.o[i] or (c.c[i - 1] > c.bb_up[i - 1] and c.c[i] < c.bb_up[i]):
        return -1, "Rejected upper Bollinger band"
    return 0, "Inside bands"


def s_rsi2_connors(c):
    i = c.i
    if c.rsi2[i] < 10 and c.c[i] > c.ema200[i]:
        return 1, f"RSI(2) {c.rsi2[i]:.0f} oversold above EMA200"
    if c.rsi2[i] > 90 and c.c[i] < c.ema200[i]:
        return -1, f"RSI(2) {c.rsi2[i]:.0f} overbought below EMA200"
    return 0, f"RSI(2) {c.rsi2[i]:.0f}"


def s_rsi_reversal(c):
    r, i = c.rsi14, c.i
    for j in range(i - 2, i + 1):
        if r[j - 1] < 30 <= r[j]:
            return 1, "RSI crossed back above 30"
        if r[j - 1] > 70 >= r[j]:
            return -1, "RSI crossed back below 70"
    return 0, f"RSI {r[i]:.0f}"


def s_stochastic(c):
    k, d, i = c.stoch_k, c.stoch_d, c.i
    for j in (i - 1, i):
        if k[j - 1] <= d[j - 1] and k[j] > d[j] and k[j] < 25:
            return 1, "Stochastic bullish cross in oversold"
        if k[j - 1] >= d[j - 1] and k[j] < d[j] and k[j] > 75:
            return -1, "Stochastic bearish cross in overbought"
    return 0, f"Stoch %K {k[i]:.0f}"


def s_vwap_reversion(c):
    i = c.i
    dist = (c.c[i] - c.vwap[i]) / c.atr_now
    if dist < -1.5 and c.c[i] > c.o[i]:
        return 1, f"{abs(dist):.1f} ATR below session mean, turning up"
    if dist > 1.5 and c.c[i] < c.o[i]:
        return -1, f"{dist:.1f} ATR above session mean, turning down"
    return 0, f"{dist:+.1f} ATR from session mean"


def s_sr_bounce(c):
    i, tol = c.i, 0.3 * c.atr_now
    for s in c.supports:
        if c.l[i] <= s + tol and c.c[i] > s and c.c[i] > c.o[i]:
            return 1, f"Bounced off support {s:.{5 if s < 50 else 2}f}"
    for r in c.resistances:
        if c.h[i] >= r - tol and c.c[i] < r and c.c[i] < c.o[i]:
            return -1, f"Rejected resistance {r:.{5 if r < 50 else 2}f}"
    return 0, "Not at a key level"


def s_rsi_divergence(c):
    i = c.i
    lows = [j for j in c.sl if j >= i - 60]
    highs = [j for j in c.sh if j >= i - 60]
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if i - b <= 8 and c.l[b] < c.l[a] and c.rsi14[b] > c.rsi14[a] + 2:
            return 1, "Bullish RSI divergence (lower low, higher RSI)"
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if i - b <= 8 and c.h[b] > c.h[a] and c.rsi14[b] < c.rsi14[a] - 2:
            return -1, "Bearish RSI divergence (higher high, lower RSI)"
    return 0, "No divergence"


def s_double_top_bottom(c):
    i, tol = c.i, 0.3 * c.atr_now
    lows = [j for j in c.sl if j >= i - 80]
    highs = [j for j in c.sh if j >= i - 80]
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if b - a >= 5 and abs(c.l[a] - c.l[b]) <= tol:
            neck = c.h[a:b + 1].max()
            if any(c.c[j - 1] <= neck < c.c[j] for j in range(max(b + 1, i - 4), i + 1)):
                return 1, "Double bottom, neckline broken"
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if b - a >= 5 and abs(c.h[a] - c.h[b]) <= tol:
            neck = c.l[a:b + 1].min()
            if any(c.c[j - 1] >= neck > c.c[j] for j in range(max(b + 1, i - 4), i + 1)):
                return -1, "Double top, neckline broken"
    return 0, "No pattern"


# ---- BREAKOUT --------------------------------------------------------------
def s_donchian_turtle(c):
    i = c.i
    for j in (i - 1, i):
        if c.c[j] > c.h[j - 20:j].max():
            return 1, "Closed above 20-bar high (Turtle)"
        if c.c[j] < c.l[j - 20:j].min():
            return -1, "Closed below 20-bar low (Turtle)"
    return 0, "Inside 20-bar channel"


def _session_breakout(c, start_h, end_h, trade_from, trade_to, name):
    if c.minutes > 60:
        return 0, "Intraday timeframes only"
    i = c.i
    now = c.idx[i]
    if not (trade_from <= now.hour < trade_to):
        return 0, f"Outside {name} window"
    today = c.idx.normalize() == now.normalize()
    hours = c.idx.hour
    box = today & (hours >= start_h) & (hours < end_h)
    if box.sum() < 2:
        return 0, f"No {name} range yet"
    hi, lo = c.h[box].max(), c.l[box].min()
    buf = 0.1 * c.atr_now
    if c.c[i] > hi + buf:
        return 1, f"Broke above {name} range"
    if c.c[i] < lo - buf:
        return -1, f"Broke below {name} range"
    return 0, f"Inside {name} range"


def s_london_breakout(c):
    return _session_breakout(c, 0, 7, 7, 11, "Asian")


def s_ny_opening_range(c):
    return _session_breakout(c, 13, 14, 14, 17, "NY opening")


def s_bb_squeeze(c):
    i = c.i
    squeezed = np.nanmin(c.bbw_rank[i - 10:i]) < 0.15 if i > 10 else False
    if squeezed and c.c[i] > c.bb_up[i]:
        return 1, "Volatility squeeze released upward"
    if squeezed and c.c[i] < c.bb_lo[i]:
        return -1, "Volatility squeeze released downward"
    return 0, "No squeeze breakout"


def s_breakout_retest(c):
    i, tol = c.i, 0.3 * c.atr_now
    for j in reversed(c.sh):
        if j > i - 15 or j < i - 100:
            continue
        lvl = c.h[j]
        broke = any(c.c[b - 1] <= lvl < c.c[b] for b in range(i - 15, i - 1))
        if broke and min(c.l[i - 2:i + 1]) <= lvl + tol and c.c[i] > lvl and c.c[i] > c.o[i]:
            return 1, "Broke resistance and retested it as support"
    for j in reversed(c.sl):
        if j > i - 15 or j < i - 100:
            continue
        lvl = c.l[j]
        broke = any(c.c[b - 1] >= lvl > c.c[b] for b in range(i - 15, i - 1))
        if broke and max(c.h[i - 2:i + 1]) >= lvl - tol and c.c[i] < lvl and c.c[i] < c.o[i]:
            return -1, "Broke support and retested it as resistance"
    return 0, "No retest"


def s_inside_bar(c):
    i = c.i
    mom, ins = i - 2, i - 1
    if c.h[ins] <= c.h[mom] and c.l[ins] >= c.l[mom]:
        if c.c[i] > c.h[mom]:
            return 1, "Inside bar broken upward"
        if c.c[i] < c.l[mom]:
            return -1, "Inside bar broken downward"
    return 0, "No inside bar break"


# ---- PRICE ACTION / SMC ----------------------------------------------------
def s_market_structure(c):
    if not c.last_break:
        return 0, "No structure yet"
    _, kind, lvl = c.last_break
    if c.structure == 1:
        return 1, f"Bullish structure (last {kind} above {lvl:.{5 if lvl < 50 else 2}f})"
    return -1, f"Bearish structure (last {kind} below {lvl:.{5 if lvl < 50 else 2}f})"


def s_order_block(c):
    i = c.i
    if c.bull_ob and i - c.bull_ob[2] <= 100:
        lo, hi, _ = c.bull_ob
        if min(c.l[i - 2:i + 1]) <= hi and c.c[i] > lo and c.c[i] > c.o[i]:
            return 1, f"Reacting from bullish order block {lo:.2f}-{hi:.2f}"
    if c.bear_ob and i - c.bear_ob[2] <= 100:
        lo, hi, _ = c.bear_ob
        if max(c.h[i - 2:i + 1]) >= lo and c.c[i] < hi and c.c[i] < c.o[i]:
            return -1, f"Reacting from bearish order block {lo:.2f}-{hi:.2f}"
    return 0, "Not at an order block"


def s_fair_value_gap(c):
    i = c.i
    for j in range(i - 1, i - 30, -1):
        if c.l[j] > c.h[j - 2]:  # bullish gap
            bot, top = c.h[j - 2], c.l[j]
            if c.l[j + 1:i].min(initial=np.inf) < bot:
                continue  # already filled
            if c.l[i] <= top and c.c[i] >= bot and c.c[i] > c.o[i]:
                return 1, "Bounced inside bullish FVG"
        if c.h[j] < c.l[j - 2]:  # bearish gap
            bot, top = c.h[j], c.l[j - 2]
            if c.h[j + 1:i].max(initial=-np.inf) > top:
                continue
            if c.h[i] >= bot and c.c[i] <= top and c.c[i] < c.o[i]:
                return -1, "Rejected inside bearish FVG"
    return 0, "No FVG reaction"


def s_liquidity_sweep(c):
    i = c.i
    lows = [c.l[j] for j in c.sl if i - 60 <= j < i - SWING_K]
    highs = [c.h[j] for j in c.sh if i - 60 <= j < i - SWING_K]
    if c.prev_day_low:
        lows.append(c.prev_day_low)
    if c.prev_day_high:
        highs.append(c.prev_day_high)
    for lvl in lows:
        if c.l[i] < lvl < c.c[i] and c.c[i] > c.o[i]:
            return 1, f"Swept liquidity below {lvl:.{5 if lvl < 50 else 2}f} and closed back above"
    for lvl in highs:
        if c.h[i] > lvl > c.c[i] and c.c[i] < c.o[i]:
            return -1, f"Swept liquidity above {lvl:.{5 if lvl < 50 else 2}f} and closed back below"
    return 0, "No sweep"


def s_fib_golden_zone(c):
    i = c.i
    if not c.sh or not c.sl:
        return 0, "No swings"
    hi_j, lo_j = c.sh[-1], c.sl[-1]
    H, L = c.h[hi_j], c.l[lo_j]
    if hi_j > lo_j and H > L:   # last leg up -> look to buy the pullback
        z_top, z_bot = H - 0.5 * (H - L), H - 0.618 * (H - L)
        if min(c.l[i - 2:i + 1]) <= z_top and c.c[i] > z_bot and c.c[i] < H and c.c[i] > c.o[i]:
            return 1, "Bounced from 50-61.8% Fibonacci zone"
    if lo_j > hi_j and H > L:   # last leg down -> look to sell the pullback
        z_bot, z_top = L + 0.5 * (H - L), L + 0.618 * (H - L)
        if max(c.h[i - 2:i + 1]) >= z_bot and c.c[i] < z_top and c.c[i] > L and c.c[i] < c.o[i]:
            return -1, "Rejected from 50-61.8% Fibonacci zone"
    return 0, "Not in golden zone"


def s_candle_patterns(c):
    i = c.i
    o, h, l, cl = c.o[i], c.h[i], c.l[i], c.c[i]
    rng, body = h - l, abs(cl - o)
    if rng <= 0:
        return 0, "Flat candle"
    near_sup = any(abs(l - s) <= 0.5 * c.atr_now for s in c.supports) or l <= c.bb_lo[i] or abs(l - c.ema50[i]) <= 0.3 * c.atr_now
    near_res = any(abs(h - r) <= 0.5 * c.atr_now for r in c.resistances) or h >= c.bb_up[i] or abs(h - c.ema50[i]) <= 0.3 * c.atr_now
    lower_wick, upper_wick = min(o, cl) - l, h - max(o, cl)
    bull_pin = lower_wick >= 2 * body and lower_wick >= 0.6 * rng
    bear_pin = upper_wick >= 2 * body and upper_wick >= 0.6 * rng
    po, pc = c.o[i - 1], c.c[i - 1]
    bull_eng = pc < po and cl > o and cl >= po and o <= pc
    bear_eng = pc > po and cl < o and cl <= po and o >= pc
    if (bull_pin or bull_eng) and near_sup:
        return 1, ("Bullish pin bar" if bull_pin else "Bullish engulfing") + " at a level"
    if (bear_pin or bear_eng) and near_res:
        return -1, ("Bearish pin bar" if bear_pin else "Bearish engulfing") + " at a level"
    return 0, "No pattern at a level"


# name, family, weight, is_trigger, function
#   is_trigger = True  -> an actual entry event (needed for timing)
#   is_trigger = False -> a "state" (tells direction, not timing)
STRATEGIES = [
    ("EMA 9/21 cross",           "trend",        1.0, False, s_ema_cross),
    ("EMA ribbon 20/50/200",     "trend",        1.0, False, s_ema_ribbon),
    ("Golden/Death cross",       "trend",        0.7, False, s_golden_cross),
    ("Supertrend",               "trend",        1.0, False, s_supertrend),
    ("Ichimoku cloud",           "trend",        1.0, False, s_ichimoku),
    ("ADX + DI",                 "trend",        1.0, False, s_adx_di),
    ("MACD momentum",            "trend",        0.7, False, s_macd),
    ("Parabolic SAR",            "trend",        0.7, False, s_psar),
    ("Heikin Ashi trend",        "trend",        0.7, False, s_heikin_ashi),
    ("Bollinger bounce",         "reversion",    1.0, True,  s_bollinger_bounce),
    ("RSI-2 (Connors)",          "reversion",    1.0, True,  s_rsi2_connors),
    ("RSI 30/70 reversal",       "reversion",    1.0, True,  s_rsi_reversal),
    ("Stochastic reversal",      "reversion",    1.0, True,  s_stochastic),
    ("VWAP / session mean",      "reversion",    1.0, True,  s_vwap_reversion),
    ("Support/Resistance bounce", "reversion",   1.2, True,  s_sr_bounce),
    ("RSI divergence",           "reversion",    1.2, True,  s_rsi_divergence),
    ("Double top/bottom",        "reversion",    1.2, True,  s_double_top_bottom),
    ("Donchian (Turtle)",        "breakout",     1.0, True,  s_donchian_turtle),
    ("London breakout",          "breakout",     1.0, True,  s_london_breakout),
    ("NY opening range",         "breakout",     1.0, True,  s_ny_opening_range),
    ("Bollinger squeeze",        "breakout",     1.0, True,  s_bb_squeeze),
    ("Breakout & retest",        "breakout",     1.3, True,  s_breakout_retest),
    ("Inside bar breakout",      "breakout",     0.8, True,  s_inside_bar),
    ("Market structure BOS/CHoCH", "price_action", 1.5, False, s_market_structure),
    ("Order block",              "price_action", 1.2, True,  s_order_block),
    ("Fair value gap",           "price_action", 1.0, True,  s_fair_value_gap),
    ("Liquidity sweep",          "price_action", 1.2, True,  s_liquidity_sweep),
    ("Fibonacci golden zone",    "price_action", 1.0, True,  s_fib_golden_zone),
    ("Pin bar / Engulfing",      "price_action", 1.0, True,  s_candle_patterns),
]


def higher_tf(interval, scalp=False):
    if scalp:
        return SCALP["higher_tf"].get(interval, HIGHER_TF.get(interval, "1day"))
    return HIGHER_TF.get(interval, "1day")


# --------------------------------------------------------------------------
# REGIME + HIGHER TIMEFRAME
# --------------------------------------------------------------------------
def detect_regime(c: Ctx):
    i = c.i
    atr_ratio = c.atr[i] / np.nanmean(c.atr[max(0, i - 100):i])
    last_range = (c.h[i] - c.l[i]) / c.atr[i - 1]
    adx_now = c.adx[i]
    if atr_ratio > 1.8 or last_range > 3.0:
        return "VOLATILE", f"volatility spike (ATR x{atr_ratio:.1f}) - likely news"
    if adx_now >= 25:
        return "TRENDING", f"ADX {adx_now:.0f}"
    if adx_now <= 20:
        return "RANGING", f"ADX {adx_now:.0f}"
    return "TRANSITION", f"ADX {adx_now:.0f} (between 20 and 25)"


def htf_bias(df: pd.DataFrame | None):
    if df is None or len(df) < 60:
        return 0, "no data"
    cl = df["close"]
    slow = 200 if len(df) >= 220 else 50
    fast = 50 if slow == 200 else 20
    ef, es = ema(cl, fast).iloc[-1], ema(cl, slow).iloc[-1]
    st = supertrend(df)[-1]
    votes = [1 if ef > es else -1, 1 if cl.iloc[-1] > ef else -1, int(st)]
    s = sum(votes)
    if s >= 2:
        return 1, f"bullish (EMA{fast}/{slow}, Supertrend)"
    if s <= -2:
        return -1, f"bearish (EMA{fast}/{slow}, Supertrend)"
    return 0, "mixed"


# --------------------------------------------------------------------------
# RISK: SL / TP / LOT
# --------------------------------------------------------------------------
def build_trade(c: Ctx, symbol, direction, price, min_rr, tp2_rr, scalp=False):
    cfg = SYMBOLS.get(symbol, dict(spread=0.0))
    a, spread = c.atr_now, cfg["spread"]
    i = c.i
    look, buf = (SCALP["swing_lookback"], SCALP["buffer_atr"]) if scalp else (30, 0.25)
    lo_cap, hi_cap = (SCALP["sl_min_atr"], SCALP["sl_max_atr"]) if scalp else (0.8, 3.0)
    default = 1.0 if scalp else 1.5

    if direction == 1:
        if scalp:   # nearest low of the last few candles
            ref = [c.l[i - look + 1:i + 1].min()]
        else:
            ref = [c.l[j] for j in c.sl if j >= i - look]
        ref = [x for x in ref if x < price]
        sl = (max(ref) - buf * a - spread) if ref else price - default * a
        dist = price - sl
    else:
        if scalp:
            ref = [c.h[i - look + 1:i + 1].max()]
        else:
            ref = [c.h[j] for j in c.sh if j >= i - look]
        ref = [x for x in ref if x > price]
        sl = (min(ref) + buf * a + spread) if ref else price + default * a
        dist = sl - price
    dist = min(max(dist, lo_cap * a), hi_cap * a)  # not too tight (noise), not too wide
    sl = price - dist if direction == 1 else price + dist

    tp1 = price + direction * min_rr * dist
    tp2 = price + direction * tp2_rr * dist

    # Strong levels in the way (clustered S/R, previous day H/L, opposing order block)
    if direction == 1:
        obstacles = list(c.resistances) + ([c.prev_day_high] if c.prev_day_high else [])
        if c.bear_ob:
            obstacles.append(c.bear_ob[0])
        obstacles = [x for x in obstacles if x > price + 0.3 * a]
        nearest = min(obstacles) if obstacles else None
        blocked = nearest is not None and nearest < tp1
        if nearest and tp1 <= nearest < tp2:
            tp2 = max(tp1, nearest - 0.1 * a)
    else:
        obstacles = list(c.supports) + ([c.prev_day_low] if c.prev_day_low else [])
        if c.bull_ob:
            obstacles.append(c.bull_ob[1])
        obstacles = [x for x in obstacles if x < price - 0.3 * a]
        nearest = max(obstacles) if obstacles else None
        blocked = nearest is not None and nearest > tp1
        if nearest and tp2 < nearest <= tp1:
            tp2 = min(tp1, nearest + 0.1 * a)

    # Rough time-to-target: price moves ~1 ATR per candle on average, but in
    # zig-zags, so covering D usually takes about (D / ATR)^2 candles.
    est_bars = max(1, int(round((min_rr * dist / a) ** 2)))
    return dict(entry=price, sl=sl, tp1=tp1, tp2=tp2, risk_distance=dist,
                rr_tp1=min_rr, rr_tp2=round(abs(tp2 - price) / dist, 2),
                blocked=blocked, nearest_obstacle=nearest, est_bars=est_bars,
                spread_share=spread / dist if dist else 1.0)


def lot_size(symbol, entry, risk_distance, balance, risk_pct, cent=False):
    cfg = SYMBOLS.get(symbol, dict(contract=100_000))
    risk_amount = balance * risk_pct / 100
    loss_per_lot = risk_distance * cfg["contract"]
    if symbol.startswith("USD/"):           # quote currency isn't USD -> convert
        loss_per_lot /= entry
    raw = risk_amount / loss_per_lot
    lots = np.floor(raw * 100) / 100
    note = ""
    if lots < 0.01:
        lots = 0.01
        real = loss_per_lot * 0.01
        note = f"Min lot 0.01 risks ${real:.2f} ({100 * real / balance:.1f}% of balance) - above your {risk_pct}%."
        risk_amount = real
    if cent:
        return round(lots * 100, 2), f"{note} (cent-account lots)".strip(), risk_amount
    return round(float(lots), 2), note, risk_amount


# --------------------------------------------------------------------------
# MAIN ENGINE
# --------------------------------------------------------------------------
def generate_signal(symbol="XAU/USD", interval="15min", balance=100.0, risk_pct=2.0,
                    api_key=None, min_rr=2.0, tp2_rr=3.0, cent=False,
                    df=None, htf_df=None, scalp=False):
    """Run every strategy on the latest market data and return a signal dict."""
    api_key = api_key or os.getenv("TWELVE_DATA_API_KEY")
    if df is None:
        if not api_key:
            raise RuntimeError("Set TWELVE_DATA_API_KEY or pass --apikey")
        df = fetch_candles(symbol, interval, api_key)
        htf_df = fetch_candles(symbol, higher_tf(interval, scalp), api_key, 300)

    if len(df) < 250:
        raise RuntimeError(f"Need at least 250 candles, got {len(df)}")

    price = float(df["close"].iloc[-1])          # live price (forming candle)
    closed = df.iloc[:-1]                         # strategies use CLOSED candles only
    c = Ctx(closed, interval)
    digits = SYMBOLS.get(symbol, {}).get("digits", 5)
    now = datetime.now(timezone.utc)
    warnings = []

    # Stale data / market closed
    age_min = (now - df.index[-1].to_pydatetime()).total_seconds() / 60
    if age_min > 3 * c.minutes + 5:
        warnings.append(f"Last candle is {age_min / 60:.1f}h old - market may be closed.")
    off_session = SYMBOLS.get(symbol, {}).get("fx_session") and not (7 <= now.hour < 20)
    if off_session and not scalp:
        warnings.append("Outside London/NY hours - lower liquidity, signals are weaker.")
    if scalp:
        tp2_rr = SCALP["tp2_rr"]

    regime, regime_why = detect_regime(c)
    htf_dir, htf_why = htf_bias(htf_df)

    # Run all strategies
    votes = []
    weights = REGIME_WEIGHTS.get(regime, {})
    for name, fam, w, trig, fn in STRATEGIES:
        try:
            v, why = fn(c)
        except Exception as e:  # one broken strategy must not kill the signal
            v, why = 0, f"error: {e}"
        eff = w * weights.get(fam, 0.0)
        votes.append(dict(strategy=name, family=fam, vote=int(v), reason=why,
                          weight=round(eff, 2), active=eff > 0, trigger=trig))

    active = [v for v in votes if v["active"]]
    buy_w = sum(v["weight"] for v in active if v["vote"] == 1)
    sell_w = sum(v["weight"] for v in active if v["vote"] == -1)
    buy_n = sum(1 for v in active if v["vote"] == 1)
    sell_n = sum(1 for v in active if v["vote"] == -1)

    result = dict(symbol=symbol, interval=interval, time_utc=now.strftime("%Y-%m-%d %H:%M"),
                  price=round(price, digits), regime=regime, regime_reason=regime_why,
                  mode="SCALP" if scalp else "SWING",
                  htf=higher_tf(interval, scalp), htf_bias={1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[htf_dir],
                  htf_reason=htf_why, buy_votes=buy_n, sell_votes=sell_n,
                  signal="NO TRADE", reasons=[], warnings=warnings, votes=votes)

    if regime == "VOLATILE":
        result["reasons"].append(f"Market is too volatile ({regime_why}). Stand aside.")
        return result

    direction = 1 if buy_w > sell_w else -1 if sell_w > buy_w else 0
    agree_w, against_w = (buy_w, sell_w) if direction == 1 else (sell_w, buy_w)
    agree_n = buy_n if direction == 1 else sell_n
    agreement = agree_w / (agree_w + against_w) if (agree_w + against_w) else 0
    triggers = [v["strategy"] for v in active if v["trigger"] and v["vote"] == direction and direction != 0]
    rules = REGIME_RULES[regime]
    result["confidence"] = round(100 * agreement)

    # Gate checks - every one must pass
    if scalp and off_session:
        result["reasons"].append("Scalping only in London/NY hours (07:00-20:00 UTC) - spreads too wide now.")
    if direction == 0:
        result["reasons"].append("Buy and sell votes are balanced.")
    if agree_n < rules["min_votes"]:
        result["reasons"].append(f"Only {agree_n} strategies agree (need {rules['min_votes']} in {regime}).")
    if agreement < rules["min_agreement"]:
        result["reasons"].append(f"Agreement {agreement:.0%} is below {rules['min_agreement']:.0%}.")
    if not triggers:
        result["reasons"].append("No entry trigger yet (direction only, no timing). Wait.")
    if direction != 0 and htf_dir == -direction:
        if regime != "RANGING" or agreement < 0.85:
            result["reasons"].append(f"Against the {higher_tf(interval, scalp)} trend ({htf_why}).")
        else:
            warnings.append("Counter-trend to higher timeframe - take TP1 only.")

    if direction != 0:
        trade = build_trade(c, symbol, direction, price, min_rr, tp2_rr, scalp)
        if scalp and trade["spread_share"] > SCALP["max_spread_share"]:
            result["reasons"].append(f"Spread is {trade['spread_share']:.0%} of the stop - too costly to scalp.")
        if trade["blocked"]:
            ob = trade["nearest_obstacle"]
            result["reasons"].append(f"No room for 1:{min_rr:g} - strong level at {ob:.{digits}f} is before TP1.")
        if not result["reasons"]:
            lots, lot_note, risk_amt = lot_size(symbol, price, trade["risk_distance"], balance, risk_pct, cent)
            if lot_note:
                warnings.append(lot_note)
            result.update(
                signal="BUY" if direction == 1 else "SELL",
                entry=round(trade["entry"], digits), stop_loss=round(trade["sl"], digits),
                take_profit_1=round(trade["tp1"], digits), take_profit_2=round(trade["tp2"], digits),
                rr_tp1=f"1:{trade['rr_tp1']:g}", rr_tp2=f"1:{trade['rr_tp2']:g}",
                sl_distance=round(trade["risk_distance"], digits),
                lot_size=lots, risk_amount=round(risk_amt, 2), triggers=triggers,
                order="MARKET - enter now",
                est_minutes_to_tp1=trade["est_bars"] * c.minutes,
            )
            if scalp:  # time stop: a scalp that stalls is a failed scalp
                result["time_stop_minutes"] = max(3, trade["est_bars"] * 3) * c.minutes
    return result


# --------------------------------------------------------------------------
# SCALP ENGINE  -  15min decides DIRECTION, 5min/1min decides ENTRY
# --------------------------------------------------------------------------
ALL_ON = {"trend": 1.0, "breakout": 1.0, "price_action": 1.0, "reversion": 1.0}


def run_strategies(c: Ctx, weights: dict):
    votes = []
    for name, fam, w, trig, fn in STRATEGIES:
        try:
            v, why = fn(c)
        except Exception as e:
            v, why = 0, f"error: {e}"
        eff = w * weights.get(fam, 0.0)
        votes.append(dict(strategy=name, family=fam, vote=int(v), reason=why,
                          weight=round(eff, 2), active=eff > 0, trigger=trig))
    act = [v for v in votes if v["active"]]
    tally = dict(buy_w=sum(v["weight"] for v in act if v["vote"] == 1),
                 sell_w=sum(v["weight"] for v in act if v["vote"] == -1),
                 buy_n=sum(1 for v in act if v["vote"] == 1),
                 sell_n=sum(1 for v in act if v["vote"] == -1))
    return votes, tally


def generate_scalp_signal(symbol="XAU/USD", entry_tf="5min", balance=100.0, risk_pct=2.0,
                          api_key=None, min_rr=2.0, cent=False,
                          dir_df=None, entry_df=None, direction_tf="15min"):
    """Top-down scalp: all strategies on 15min pick the direction,
    then 5min/1min must give an entry trigger in that direction. Market entry."""
    api_key = api_key or os.getenv("TWELVE_DATA_API_KEY")
    if dir_df is None or entry_df is None:
        if not api_key:
            raise RuntimeError("Set TWELVE_DATA_API_KEY or pass --apikey")
        dir_df = fetch_candles(symbol, direction_tf, api_key)
        entry_df = fetch_candles(symbol, entry_tf, api_key)
    for name, d in ((direction_tf, dir_df), (entry_tf, entry_df)):
        if len(d) < 250:
            raise RuntimeError(f"Need at least 250 {name} candles, got {len(d)}")

    price = float(entry_df["close"].iloc[-1])
    dc = Ctx(dir_df.iloc[:-1], direction_tf)      # direction frame (closed candles)
    ec = Ctx(entry_df.iloc[:-1], entry_tf)        # entry frame (closed candles)
    digits = SYMBOLS.get(symbol, {}).get("digits", 5)
    now = datetime.now(timezone.utc)
    warnings, reasons = [], []
    name = {1: "BUY", -1: "SELL", 0: "NONE"}

    age_min = (now - entry_df.index[-1].to_pydatetime()).total_seconds() / 60
    if age_min > 3 * ec.minutes + 5:
        warnings.append(f"Last candle is {age_min / 60:.1f}h old - market may be closed.")
    if SYMBOLS.get(symbol, {}).get("fx_session") and not (7 <= now.hour < 20):
        reasons.append("Scalping only in London/NY hours (07:00-20:00 UTC) - spreads too wide now.")

    # ---- STEP 1: direction from 15min ----
    regime, regime_why = detect_regime(dc)
    dir_votes, dt = run_strategies(dc, REGIME_WEIGHTS.get(regime, {}))
    direction = 1 if dt["buy_w"] > dt["sell_w"] else -1 if dt["sell_w"] > dt["buy_w"] else 0
    agree_w, against_w = (dt["buy_w"], dt["sell_w"]) if direction == 1 else (dt["sell_w"], dt["buy_w"])
    agree_n = dt["buy_n"] if direction == 1 else dt["sell_n"]
    d_agreement = agree_w / (agree_w + against_w) if (agree_w + against_w) else 0

    if regime == "VOLATILE":
        reasons.append(f"{direction_tf} is too volatile ({regime_why}). Stand aside.")
    else:
        rules = REGIME_RULES[regime]
        if direction == 0 or agree_n < rules["min_votes"] or d_agreement < rules["min_agreement"]:
            reasons.append(f"{direction_tf} has no clear direction ({agree_n} votes, "
                           f"{d_agreement:.0%} agreement in {regime}).")

    # ---- STEP 2: entry trigger from 5min / 1min ----
    e_regime, e_why = detect_regime(ec)
    ent_votes, et = run_strategies(ec, ALL_ON)
    ent_trig = [v["strategy"] for v in ent_votes if v["trigger"] and direction and v["vote"] == direction]
    opp_trig = [v["strategy"] for v in ent_votes if v["trigger"] and direction and v["vote"] == -direction]
    e_for = et["buy_w"] if direction == 1 else et["sell_w"]
    e_against = et["sell_w"] if direction == 1 else et["buy_w"]
    e_agreement = e_for / (e_for + e_against) if (e_for + e_against) else 0

    if not reasons:
        if e_regime == "VOLATILE":
            reasons.append(f"{entry_tf} volatility spike ({e_why}) - wait for it to settle.")
        if not ent_trig:
            reasons.append(f"{direction_tf} says {name[direction]} - waiting for a {entry_tf} entry trigger.")
        elif len(opp_trig) > len(ent_trig):
            reasons.append(f"{entry_tf} triggers point the other way ({', '.join(opp_trig)}).")
        if e_agreement < 0.55:
            reasons.append(f"{entry_tf} momentum still against {direction_tf} ({e_agreement:.0%} agreement).")

    result = dict(symbol=symbol, mode="SCALP", interval=entry_tf, direction_tf=direction_tf,
                  time_utc=now.strftime("%Y-%m-%d %H:%M"), price=round(price, digits),
                  regime=regime, regime_reason=regime_why,
                  direction_bias=name[direction], direction_confidence=round(100 * d_agreement),
                  buy_votes=dt["buy_n"], sell_votes=dt["sell_n"],
                  entry_regime=e_regime, entry_agreement=round(100 * e_agreement),
                  signal="NO TRADE", reasons=reasons, warnings=warnings,
                  direction_votes=dir_votes, votes=ent_votes)
    if reasons or direction == 0:
        return result

    # ---- STEP 3: tight stop on entry frame, 1:2 target, room check on BOTH frames ----
    trade = build_trade(ec, symbol, direction, price, min_rr, SCALP["tp2_rr"], scalp=True)
    if trade["spread_share"] > SCALP["max_spread_share"]:
        reasons.append(f"Spread is {trade['spread_share']:.0%} of the stop - too costly to scalp.")
    blockers = [trade["nearest_obstacle"]] if trade["blocked"] else []
    big = list(dc.resistances) + [dc.prev_day_high] if direction == 1 else list(dc.supports) + [dc.prev_day_low]
    big = [x for x in big if x and (x - price) * direction > 0.3 * ec.atr_now]
    if big:
        near15 = min(big, key=lambda x: abs(x - price))
        if abs(near15 - price) < abs(trade["tp1"] - price):
            blockers.append(near15)
        elif abs(near15 - price) < abs(trade["tp2"] - price):
            trade["tp2"] = near15 - direction * 0.1 * ec.atr_now
            trade["rr_tp2"] = round(abs(trade["tp2"] - price) / trade["risk_distance"], 2)
    if blockers:
        reasons.append(f"No room for 1:{min_rr:g} - level at {blockers[0]:.{digits}f} is before TP1.")
    if reasons:
        return result

    lots, lot_note, risk_amt = lot_size(symbol, price, trade["risk_distance"], balance, risk_pct, cent)
    if lot_note:
        warnings.append(lot_note)
    result.update(
        signal=name[direction], order="MARKET - enter now",
        entry=round(price, digits), stop_loss=round(trade["sl"], digits),
        take_profit_1=round(trade["tp1"], digits), take_profit_2=round(trade["tp2"], digits),
        rr_tp1=f"1:{min_rr:g}", rr_tp2=f"1:{trade['rr_tp2']:g}",
        sl_distance=round(trade["risk_distance"], digits),
        lot_size=lots, risk_amount=round(risk_amt, 2), triggers=ent_trig,
        est_minutes_to_tp1=trade["est_bars"] * ec.minutes,
        time_stop_minutes=max(3, trade["est_bars"] * 3) * ec.minutes,
    )
    return result


# --------------------------------------------------------------------------
# PRETTY PRINT
# --------------------------------------------------------------------------
def print_signal(r, show_all=False):
    line = "=" * 64
    print(f"\n{line}\n {r.get('mode', '')} {r['symbol']}  |  {r['interval']}  |  {r['time_utc']} UTC  |  price {r['price']}\n{line}")
    if "direction_tf" in r:
        print(f" Direction ({r['direction_tf']}): {r['direction_bias']}  |  {r['regime']} ({r['regime_reason']})"
              f"  |  {r['buy_votes']} buy / {r['sell_votes']} sell, {r['direction_confidence']}% agree")
        print(f" Entry ({r['interval']}): {r['entry_regime']}  |  {r['entry_agreement']}% agree with direction")
    else:
        print(f" Regime : {r['regime']} ({r['regime_reason']})")
        print(f" HTF {r['htf']:<5}: {r['htf_bias']} ({r['htf_reason']})")
        print(f" Votes  : {r['buy_votes']} buy / {r['sell_votes']} sell"
              + (f"  |  agreement {r['confidence']}%" if "confidence" in r else ""))
    print(line)
    if r["signal"] in ("BUY", "SELL"):
        print(f" >>> {r['signal']} NOW <<<   ({r['order']})")
        print(f" Entry : {r['entry']}")
        print(f" SL    : {r['stop_loss']}   (distance {r['sl_distance']})")
        print(f" TP1   : {r['take_profit_1']}   ({r['rr_tp1']})")
        print(f" TP2   : {r['take_profit_2']}   ({r['rr_tp2']})")
        print(f" Lot   : {r['lot_size']}   (risking ${r['risk_amount']})")
        print(f" Trigger(s): {', '.join(r['triggers'])}")
        print(f" TP1 expected in ~{r['est_minutes_to_tp1']} min")
        if "time_stop_minutes" in r:
            print(f" Time stop: close if TP1 not hit within {r['time_stop_minutes']} min")
    else:
        print(" >>> NO TRADE <<<")
        for why in r["reasons"]:
            print(f"   - {why}")
    for w in r["warnings"]:
        print(f" ! {w}")
    print(line)
    icon = {1: "BUY ", -1: "SELL", 0: " -  "}

    def show(votes, title, triggers_only=False):
        print(f" {title}")
        for v in votes:
            if triggers_only and not v["trigger"] and not show_all:
                continue
            if (not v["active"] or v["vote"] == 0) and not show_all:
                continue
            tag = "" if v["active"] else "  (off in this regime)"
            print(f"   {icon[v['vote']]}  {v['strategy']:<27} {v['reason']}{tag}")

    if "direction_votes" in r:
        show(r["direction_votes"], f"[{r['direction_tf']} DIRECTION]")
        show(r["votes"], f"[{r['interval']} ENTRY TRIGGERS]", triggers_only=True)
    else:
        show(r["votes"], "[VOTES]")
    if not show_all:
        print(" (use --verbose to see every strategy)")
    print(line)


def main():
    ap = argparse.ArgumentParser(description="Multi-strategy signal engine")
    ap.add_argument("--symbol", default="XAU/USD", help="e.g. XAU/USD, EUR/USD, GBP/USD, USD/JPY, BTC/USD")
    ap.add_argument("--tf", default="15min", choices=list(TF_MINUTES), help="timeframe")
    ap.add_argument("--balance", type=float, default=100.0, help="account balance in USD")
    ap.add_argument("--risk", type=float, default=2.0, help="risk per trade in %%")
    ap.add_argument("--rr", type=float, default=2.0, help="minimum reward:risk for TP1")
    ap.add_argument("--cent", action="store_true", help="show lot size for an Exness cent account")
    ap.add_argument("--scalp", action="store_true",
                    help="scalp: 15min picks direction, --tf (5min default, or 1min) picks entry. Market entry, 1:2")
    ap.add_argument("--all", action="store_true", help="scan every symbol in SYMBOLS")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--verbose", action="store_true", help="show every strategy's vote")
    ap.add_argument("--apikey", default=None, help="Twelve Data key (or set TWELVE_DATA_API_KEY)")
    ap.add_argument("--demo", action="store_true", help="use fake data (offline test)")
    a = ap.parse_args()
    if a.scalp and "--tf" not in sys.argv:
        a.tf = "5min"
    if a.scalp and a.tf not in ("1min", "5min"):
        ap.error("scalp entry timeframe must be 1min or 5min")

    symbols = list(SYMBOLS) if a.all else [a.symbol]
    results = []
    for n, sym in enumerate(symbols):
        try:
            if a.demo and a.scalp:
                start = {"XAU/USD": 2650, "EUR/USD": 1.08, "GBP/USD": 1.27, "USD/JPY": 150, "BTC/USD": 65000}.get(sym, 100)
                r = generate_scalp_signal(sym, a.tf, a.balance, a.risk, min_rr=a.rr, cent=a.cent,
                                          dir_df=demo_candles("15min", 500, seed=n + 7, start_price=start),
                                          entry_df=demo_candles(a.tf, 500, seed=n + 7, start_price=start))
            elif a.scalp:
                if n > 0:
                    time.sleep(16)  # Twelve Data free tier: 8 requests/minute
                r = generate_scalp_signal(sym, a.tf, a.balance, a.risk, api_key=a.apikey,
                                          min_rr=a.rr, cent=a.cent)
            elif a.demo:
                start = {"XAU/USD": 2650, "EUR/USD": 1.08, "GBP/USD": 1.27, "USD/JPY": 150, "BTC/USD": 65000}.get(sym, 100)
                df = demo_candles(a.tf, 500, seed=n + 7, start_price=start)
                htf = demo_candles(higher_tf(a.tf, a.scalp), 300, seed=n + 99, start_price=start)
                r = generate_signal(sym, a.tf, a.balance, a.risk, min_rr=a.rr, cent=a.cent,
                                    df=df, htf_df=htf, scalp=a.scalp)
            else:
                if n > 0:
                    time.sleep(16)  # Twelve Data free tier: 8 requests/minute
                r = generate_signal(sym, a.tf, a.balance, a.risk, api_key=a.apikey, min_rr=a.rr,
                                    cent=a.cent, scalp=a.scalp)
        except Exception as e:
            r = dict(symbol=sym, error=str(e))
        results.append(r)
        if not a.json:
            print_signal(r, a.verbose) if "error" not in r else print(f"\n{sym}: ERROR - {r['error']}")

    if a.json:
        print(json.dumps(results if a.all else results[0], indent=2, default=float))
    else:
        print("Not financial advice. Check the economic calendar before entering.\n")


if __name__ == "__main__":
    main()
