"""
data.py - candles with a shared cache and a Twelve Data credit meter.

Free (Basic) plan: 8 credits per minute, 800 per day. 1 candle request = 1 credit.
The meter makes sure the scanner and your manual taps never go over the limit.
"""
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import markets as mk
import signal_engine as se

PLAN_LIMITS = {"free": (8, 800), "grow": (55, None), "pro": (610, None)}
MANUAL_RESERVE_PER_MIN = 2      # credits per minute kept free for your own taps
MANUAL_RESERVE_PER_DAY = 100    # credits per day kept free for your own taps


class Busy(Exception):
    """Raised when the per-minute or daily credit limit would be exceeded."""


class Credits:
    def __init__(self):
        self.lock = threading.Lock()
        self.recent = deque()
        self.day, self.used_today = None, 0
        self.exhausted_until = 0.0

    def limits(self):
        per_min, per_day = PLAN_LIMITS[mk.plan()]
        per_min = int(os.getenv("CREDITS_PER_MIN", per_min))
        return per_min, per_day

    def _roll(self, now):
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.used_today = today, 0

    def minute_left(self):
        with self.lock:
            self._roll(time.time())
            return self.limits()[0] - len(self.recent)

    def day_left(self):
        with self.lock:
            self._roll(time.time())
            per_day = self.limits()[1]
            return None if per_day is None else per_day - self.used_today

    def take(self, n=1, reserve=0):
        with self.lock:
            now = time.time()
            self._roll(now)
            per_min, per_day = self.limits()
            if now < self.exhausted_until:
                raise Busy("Twelve Data credits used up - try again later.")
            if len(self.recent) + n > per_min - reserve:
                wait = int(60 - (now - self.recent[0])) + 1 if self.recent else 60
                raise Busy(f"Data limit for this minute reached - try again in {wait}s.")
            if per_day is not None and self.used_today + n > per_day:
                raise Busy("Daily data limit (800) reached - resets at 01:00 Lagos time.")
            for _ in range(n):
                self.recent.append(now)
            self.used_today += n

    def snapshot(self):
        per_min, per_day = self.limits()
        with self.lock:
            self._roll(time.time())
            return dict(plan=mk.plan(), per_min=per_min, per_day=per_day,
                        used_minute=len(self.recent), used_today=self.used_today)


credits = Credits()
_cache: dict = {}
_cache_lock = threading.Lock()


def live() -> bool:
    return bool(os.getenv("TWELVE_DATA_API_KEY"))


def candles(symbol: str, tf: str, size: int = 500, max_age: float = 20, reserve: int = 0):
    """Cached candles. Costs 1 credit only when the cache is older than max_age seconds."""
    k = (symbol, tf)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(k)
    if hit and now - hit[0] < max_age:
        return hit[1]
    if live():
        credits.take(1, reserve=reserve)
        try:
            df = se.fetch_candles(symbol, tf, os.getenv("TWELVE_DATA_API_KEY"), size)
        except Exception as e:
            msg = str(e).lower()
            if "run out of api credits" in msg or "api credits" in msg:
                credits.exhausted_until = time.time() + 60
                raise Busy("Twelve Data says the credits are used up - wait a minute.") from e
            raise
    else:   # demo data: changes every minute so the page and scanner can be tried without a key
        m = mk.BY_SYMBOL.get(symbol, {})
        seed = (int(now // 60) + hash(symbol) % 1000 + hash(tf) % 100) % 100_000
        df = se.demo_candles(tf, size, seed=seed, start_price=m.get("start", 100))
    with _cache_lock:
        _cache[k] = (now, df)
    mk.last_prices[symbol] = float(df["close"].iloc[-1])
    return df


def cache_age(symbol, tf):
    with _cache_lock:
        hit = _cache.get((symbol, tf))
    return None if not hit else time.time() - hit[0]
