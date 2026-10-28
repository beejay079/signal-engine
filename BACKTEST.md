# Backtest — what was tested and what it showed

**Data:** real XAU/USD 1-minute candles, Jan–Jun 2018 (~157,000 candles, from the public FX-Data repository on GitHub).
**Method:** `backtest.py` replays the *same code the app runs* candle by candle. It never looks ahead: higher-timeframe
candles are used only after they close. Spread ($0.30) is charged on every trade. Results are in R (1R = your stop distance).

## Results (real app logic, after spread)

| Order type | Trades | Win rate | Average per trade | Total |
|---|---|---|---|---|
| 5M limit (pullback zone, 1:1) | 126 | 44% | −0.24R | −29.7R |
| 15M limit (pullback zone, 1:1) | 38 | 47% | −0.17R | −6.5R |
| 15M market (A/A+ only) | 13 | 38% | −0.15R | −1.9R |
| 5M market (A/A+ only) | 30 | ~27% | −0.46R | −13.8R |

## What we learned

1. **Direction:** across ~5,800 signals, price reached +1 setup-ATR before −1 only 47–50% of the time —
   for every setup type (trend pullback, liquidity sweep, breakout retest, range fade) and for the top-down bias itself.
   On this data, the structure reading did not predict the next move better than a coin flip.
2. **Tight stops hurt:** with ~$1 stops on 5M, the $0.30 spread alone cost ~0.3R per trade and noise stopped trades out.
   Most losing limit trades had been +0.9R in profit before reversing.
3. **Tuning trap:** the best rules on Feb–Mar collapsed on Apr–Jun (48% → 9% win). No rule set was profitable in
   both periods for market entries. The tested limit rule now in the app (wider stop beyond the zone, 1:1 target)
   gives the highest win rate found (~45–47%) but is still slightly negative after spread.

## What this means for you

- The app reads structure, gives market + limit orders and tracks them properly — but it has **no proven edge**.
- Practise on a **demo account** and keep using the in-app journal. Trust it only after 50+ demo trades with a positive total R.
- Gold in 2026 behaves differently from 2018 (much more volatile). Results can differ either way — that is why the journal matters.

Rerun: `python3 backtest.py candles.csv XAU/USD 15min` (csv columns: time, open, high, low, close; 1-minute data).
