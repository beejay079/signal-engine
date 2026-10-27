# Signal Engine — top-down edition

## Already live on Render? Update it in 2 minutes

1. Open your `signal-engine` repo on GitHub → **Add file** → **Upload files**.
2. Drag in **all** files from this zip (they replace the old ones) → **Commit changes**.
3. Delete the old `limit_orders.py` from the repo (open it → **⋯** → **Delete file**). It is no longer used.
4. Render redeploys by itself. Wait for **Live**, then refresh the page on your phone.

Your `TWELVE_DATA_API_KEY` and `APP_PASSWORD` stay as they are.

---

# First-time setup (no code, ~15 minutes)

You will do 3 things once: get a data key, upload the files to GitHub, and deploy free on Render.
After that you just open a link on your phone and tap **Get signal**.

---

## Step 1 — Get your free live-data key (Twelve Data)

1. Go to **twelvedata.com** and click **Sign up** (free plan).
2. After logging in, open **API Keys** in your dashboard.
3. Copy the key and keep it somewhere (Notes app). You need it in Step 3.

---

## Step 2 — Upload the files to GitHub

1. Unzip `signal-site.zip` on your Mac. You will see these files:
   `app.py`, `signal_engine.py`, `topdown.py`, `index.html`, `requirements.txt`, `Procfile`, `runtime.txt`, `DEPLOY.md`
2. Go to **github.com** → click **+** (top right) → **New repository**.
3. Name it `signal-engine`, choose **Private**, click **Create repository**.
4. On the next page click the link **"uploading an existing file"**.
5. Select **all** the files from the unzipped folder and drag them into the page.
6. Click **Commit changes**.

---

## Step 3 — Deploy free on Render

1. Go to **render.com** → **Get Started** → sign up with your **GitHub** account.
2. Click **New +** → **Web Service** → pick the **signal-engine** repo → **Connect**.
3. Fill in:

   | Field | Value |
   |---|---|
   | Language | **Python 3** |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |
   | Instance Type | **Free** |

4. Under **Environment Variables** add:

   | Name | Value |
   |---|---|
   | `TWELVE_DATA_API_KEY` | the key from Step 1 |
   | `APP_PASSWORD` | any password you choose |

5. Click **Deploy Web Service**. Wait for **Live** (3–5 minutes), then open the `.onrender.com` link at the top.

> Free plan note: the app sleeps after 15 minutes with nobody using it. The first tap after that
> takes about a minute to wake up. Open the app a minute before you trade; with **Watch** on, it stays awake.

---

## Step 4 — Put it on your phone home screen

- **iPhone (Safari):** open the link → **Share** → **Add to Home Screen**.
- **Android (Chrome):** open the link → **⋮** menu → **Add to Home screen**.

The first time you tap **Get signal**, it asks for your `APP_PASSWORD`. It remembers it after that.

---

## How it reads the market

Pick the market and your **entry timeframe**. Every check reads four charts top-down:

| Entry | Bias | Setup | Entry trigger |
|---|---|---|---|
| 1m | 4H + 1H | 15M | 1M |
| 5m | 4H + 1H | 15M | 5M |
| 15m | D1 + 4H | 1H | 15M |
| 1h | W1 + D1 | 4H | 1H |

1. **Bias** — structure (higher highs/lows or lower highs/lows, last BOS/CHoCH) and EMA50/200 decide which side you may trade.
2. **Setup** — has price pulled back into a point of interest (order block, fair value gap, support/resistance, previous-day level, Fib 50–61.8%, EMA50)?
3. **Entry** — is there a *fresh* trigger there (CHoCH, liquidity sweep + reclaim, engulfing/pin bar, breakout-retest) that is still holding?

Five playbooks are tested every time — **Trend pullback, Liquidity sweep, Breakout retest, Range fade, Momentum break** — and only the best valid one is used. All 29 strategies vote on the setup and entry charts; at least half must agree.

**Don't-chase rules.** No entry if price is already stretched from the EMA20, ran far past the trigger, made 6+ candles in a row, has extreme RSI, or has left the zone. You'll see **Don't chase** with the next pullback zone instead.

## What you'll see

- **BUY now / SELL now** (grade **A** or **A+** only) → enter at market with the SL, TP1, TP2 and lot shown. Tap a number to copy it. Follow **Manage the trade**: close half at TP1 and move the stop to entry; exit early if the trigger level breaks.
- **Wait** → the direction is there but the entry isn't. The **Plan** shows the zone to wait for and what trigger to wait for. An optional limit order is shown when it makes sense.
- **Don't chase** → the move already went. Wait for the pullback zone shown.
- **No trade / Stand aside** → no clear top-down direction, market closed, or a news-type volatility spike.
- **Checklist** shows exactly which checks passed (✓) and failed (✗).

Turn on **Watch & alert me** and keep the page open: it checks every 30 seconds and beeps, vibrates and (if you allow it) shows a notification when an A/A+ setup appears. One alert per setup — no repeats.

Data use: the higher timeframes are cached, so Watch mode uses about 2–3 Twelve Data credits a minute. The free plan's 800 credits a day covers roughly 4–5 hours of watching.

## If something goes wrong

- **"Twelve Data limit reached"** → the free plan allows 8 credits per minute. The first check of a market uses 4 (one per timeframe), so switching markets quickly can hit it. Wait a minute.
- **Page won't open** → in Render, open **Logs** and send the red error text to Claude.
- **Gold price differs from MT5** → Twelve Data and Exness prices can differ slightly. Always place the trade using your MT5 price and the same SL/TP distances.
