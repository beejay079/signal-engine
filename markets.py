"""
markets.py - every instrument the site can scan, grouped like the broker app:
Metals, Forex, Indices, Energy, Crypto, Commodities, Bonds.

  plan   = cheapest Twelve Data plan that serves the symbol
           (free = Basic $0, grow = Grow $29+, pro = Pro $99+)
  verify = True where the Twelve Data symbol code could not be confirmed;
           the site shows an error for that market if the code is wrong.

Registering the specs into signal_engine.SYMBOLS lets the engine size stops and
lots for every market without editing signal_engine.py. lot_size() is replaced
with a version that also converts cross pairs (EUR/GBP, GBP/JPY ...) to USD.
"""
import os

import numpy as np

import signal_engine as se

PLAN_RANK = {"free": 0, "grow": 1, "pro": 2}
CATEGORIES = ["Metals", "Forex", "Indices", "Energy", "Crypto", "Commodities", "Bonds"]
GOLD = "XAU/USD"


def _m(symbol, name, cat, contract, digits, spread, plan="free", start=1.0,
       session=True, verify=False):
    return dict(symbol=symbol, name=name, category=cat, contract=contract, digits=digits,
                spread=spread, plan=plan, start=start, fx_session=session, verify=verify)


_FX = [  # symbol, approx price (demo data only)
    ("EUR/USD", 1.08), ("GBP/USD", 1.27), ("USD/JPY", 150), ("USD/CHF", 0.88), ("AUD/USD", 0.66),
    ("USD/CAD", 1.37), ("NZD/USD", 0.60), ("EUR/GBP", 0.85), ("EUR/JPY", 162), ("GBP/JPY", 190),
    ("AUD/JPY", 99), ("EUR/AUD", 1.64), ("GBP/AUD", 1.93), ("EUR/CHF", 0.95), ("CAD/JPY", 109),
    ("CHF/JPY", 170), ("AUD/NZD", 1.10), ("GBP/CHF", 1.12),
]
MAJORS = {"EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"}

MARKETS = [
    _m("XAU/USD", "Gold", "Metals", 100, 2, 0.30, start=2650),
    _m("XAG/USD", "Silver", "Metals", 5000, 3, 0.025, start=31),
    _m("XPT/USD", "Platinum", "Metals", 100, 2, 2.0, start=950, verify=True),
    _m("XPD/USD", "Palladium", "Metals", 100, 2, 4.0, start=1000, verify=True),
] + [
    _m(s, s.replace("/", ""), "Forex", 100_000, 3 if s.endswith("JPY") else 5,
       (0.012 if s.endswith("JPY") else 0.00010) * (1 if s in MAJORS else 1.8), start=p)
    for s, p in _FX
] + [
    _m("SPX", "US500", "Indices", 1, 2, 0.5, "grow", 5800, verify=True),
    _m("NDX", "USTEC", "Indices", 1, 2, 1.5, "grow", 20500, verify=True),
    _m("DJI", "US30", "Indices", 1, 2, 2.5, "grow", 42000, verify=True),
    _m("WTI/USD", "USOIL", "Energy", 1000, 3, 0.03, "grow", 72, verify=True),
    _m("XBR/USD", "UKOIL", "Energy", 1000, 3, 0.03, "grow", 76, verify=True),
    _m("NG/USD", "XNGUSD", "Energy", 10_000, 4, 0.006, "grow", 2.6, verify=True),
    _m("BTC/USD", "Bitcoin", "Crypto", 1, 2, 15.0, start=65000, session=False),
    _m("ETH/USD", "Ethereum", "Crypto", 1, 2, 1.5, start=2600, session=False),
    _m("SOL/USD", "Solana", "Crypto", 1, 3, 0.15, start=150, session=False),
    _m("XRP/USD", "Ripple", "Crypto", 1, 5, 0.003, start=0.55, session=False),
    _m("LTC/USD", "Litecoin", "Crypto", 1, 2, 0.3, start=70, session=False),
    _m("DOGE/USD", "Dogecoin", "Crypto", 1, 5, 0.0008, start=0.12, session=False),
    _m("XCU/USD", "Copper", "Commodities", 25_000, 4, 0.004, "grow", 4.3, verify=True),
    _m("US10Y", "US 10Y yield", "Bonds", 1, 3, 0.01, "pro", 4.2, verify=True),
]
BY_SYMBOL = {m["symbol"]: m for m in MARKETS}
FX_FALLBACK_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 1 / 150, "CHF": 1.13,
                   "CAD": 0.73, "AUD": 0.66, "NZD": 0.60}
last_prices: dict = {}   # filled by the app whenever candles are fetched


def plan() -> str:
    p = os.getenv("DATA_PLAN", "free").lower()
    return p if p in PLAN_RANK else "free"


def allowed(symbol: str) -> bool:
    m = BY_SYMBOL.get(symbol)
    return bool(m) and PLAN_RANK[m["plan"]] <= PLAN_RANK[plan()]


def catalog():
    return [dict(symbol=m["symbol"], name=m["name"], category=m["category"], plan=m["plan"],
                 locked=not allowed(m["symbol"]), verify=m["verify"]) for m in MARKETS]


def _usd_per_unit(ccy: str) -> float:
    """How many USD one unit of `ccy` is worth, from the latest fetched prices if we have them."""
    if ccy == "USD":
        return 1.0
    if f"{ccy}/USD" in last_prices:
        return last_prices[f"{ccy}/USD"]
    if f"USD/{ccy}" in last_prices and last_prices[f"USD/{ccy}"]:
        return 1 / last_prices[f"USD/{ccy}"]
    return FX_FALLBACK_USD.get(ccy, 1.0)


def lot_size(symbol, entry, risk_distance, balance, risk_pct, cent=False):
    """Same output as the engine's lot_size, but correct for cross pairs and every market."""
    m = BY_SYMBOL.get(symbol, dict(contract=100_000, category="Forex"))
    risk_amount = balance * risk_pct / 100
    loss_per_lot = risk_distance * m["contract"]                 # in quote currency
    if "/" in symbol and m["category"] in ("Forex", "Metals"):
        base, quote = symbol.split("/")
        loss_per_lot *= (1 / entry) if base == "USD" else _usd_per_unit(quote)
    lots = np.floor(risk_amount / loss_per_lot * 100) / 100 if loss_per_lot > 0 else 0.01
    notes = []
    if lots < 0.01:
        lots = 0.01
        real = loss_per_lot * 0.01
        notes.append(f"Min lot 0.01 risks ${real:.2f} ({100 * real / balance:.1f}% of balance) - above your {risk_pct}%.")
        risk_amount = real
    if m["category"] not in ("Forex", "Metals"):
        notes.append(f"Lot assumes {m['contract']:,} units per lot - check the contract size in MT5.")
    lots = float(lots) * (100 if cent else 1)
    if cent:
        notes.append("(cent-account lots)")
    return round(lots, 2), " ".join(notes), risk_amount


# Register every market's specs with the engine and use the better lot sizing
for _mk in MARKETS:
    se.SYMBOLS[_mk["symbol"]] = dict(contract=_mk["contract"], digits=_mk["digits"],
                                     spread=_mk["spread"], fx_session=_mk["fx_session"])
se.lot_size = lot_size
