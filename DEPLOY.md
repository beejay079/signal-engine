# Signal Engine — top-down + trade tracking

## Already live on Render? Update it in 2 minutes

1. Open your `signal-engine` repo on GitHub → **Add file** → **Upload files**.
2. Drag in **all** files from this zip (they replace the old ones) → **Commit changes**.
3. If your repo still has `limit_orders.py`, delete it (open it → **⋯** → **Delete file**). It is no longer used.
4. Render redeploys by itself. Wait for **Live**, then refresh the page on your phone.

Your `TWELVE_DATA_API_KEY` and `APP_PASSWORD` stay as they are.

**New in this version:** `structure.py` (market structure on every timeframe) and `calibrate_poi.py`
(optional - measures real POI reaction rates). Make sure both are uploaded.

### Market structure — every timeframe
For each of the 4 timeframes the page shows: bias and why, the last BOS/CHoCH with its exact price and time,
the swing points (HH / HL / LH / LL), impulsive or corrective movement, valid POIs (order blocks, liquidity
sweep, supply/demand base; FVG only as confirmation) with exact ranges and a reaction estimate, and a plan:
**limit at the best order block, stop below the HL (buy) / above the LH (sell), take profit at the previous HH / LL**.

**Levels are fixed.** They are built from closed candles around the last BOS/CHoCH and judged once, 4 candles
after the break. They change only when a new BOS/CHoCH closes on that timeframe - then that timeframe is
re-read, marked **NEW**, and (with *Watch* on) you get an alert. The take profit appears once the HH/LL is
confirmed (a pullback has started); until then the plan shows *forming*.

The pending order on the main card uses the setup timeframe's plan (then the entry, then the middle timeframe)
only when it agrees with the top-down structure bias. This order rule is **not backtested yet** - see BACKTEST.md.

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
   `app.py`, `signal_engine.py`, `topdown.py`, `backtest.py`, `index.html`, `requirements.txt`, `Procfile`, `runtime.txt`, `DEPLOY.md`
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

## What you get on every check

1. **Market story** — where the move came from (the protected low/high that started it), what price is doing now
   (impulse, pullback, or structure breaking), where it is likely going (the liquidity it is drawn to), and the level
   that proves the idea wrong.
2. **Top-down structure** — bias timeframes (swings HH/HL/LH/LL, protected level), setup timeframe (impulse or pullback
   and how deep), entry timeframe.
3. **Option A — market order** (only A/A+ setups): entry, stop beyond structure, TP1/TP2, lot size.
4. **Limit order** at the best zone in the pullback: stop beyond the zone, one take-profit at 1:1,
   cancel rules and a cancel time.
5. **Checklist** of every rule, ✓ or ✗.

## Tracking the trade you took

Tap **I took it** (market) or **I placed it** (limit). Your phone remembers it and every check updates it:

- limit **filled**, or **cancelled** (not filled in time, or price reached the target without you);
- **TP1 hit → half closed, stop moved to entry**, then the stop **trails** under each new higher low / lower high;
- **closed early** if the entry chart breaks structure against you before TP1, or the setup timeframe flips;
- a **warning with the reason** whenever the structure changes.

Every change has a time and a reason. With **Watch & alert me** on (page open), your phone beeps/vibrates on every
change and on every new A/A+ setup. New signals on that market pause while you have a trade open there.
Copy any stop change into MT5 yourself. The app can't move your MT5 orders.

**Journal** at the bottom: every closed trade with its R result, win rate and total.

## Be honest with yourself: the test results

`BACKTEST.md` has the full test on real gold candles. In short: after spread, none of the setups made money in
testing — they were close to a coin flip. Each order card shows its tested result. Use a demo account and your
journal first.

Data use: Watch mode uses about 2–3 Twelve Data credits a minute (800 free per day ≈ 4–5 hours of watching).

## If something goes wrong

- **"Twelve Data limit reached"** → the free plan allows 8 credits per minute. The first check of a market uses 4 (one per timeframe), so switching markets quickly can hit it. Wait a minute.
- **Page won't open** → in Render, open **Logs** and send the red error text to Claude.
- **Gold price differs from MT5** → Twelve Data and Exness prices can differ slightly. Always place the trade using your MT5 price and the same SL/TP distances.
