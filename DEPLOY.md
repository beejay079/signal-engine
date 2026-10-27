# Put your Signal Engine online (no code)

You do this once. After that the site scans the markets by itself and your phone buzzes when it
finds a top-grade trade. Gold is always checked first and most often.

Already have the site on Render? Do **Step 2** (replace the files), then **Steps 4–6**.

---

## Step 1 — Get your free live-data key (Twelve Data)

1. Go to **twelvedata.com** → **Sign up** (free plan).
2. Open **API Keys** in your dashboard and copy the key.

---

## Step 2 — Put the files on GitHub

1. Unzip `signal-site.zip`. You'll see 16 files:
   `app.py`, `signal_engine.py`, `topdown.py`, `manage.py`, `limit_orders.py`, `entry_rules.py`, `setups.py`, `markets.py`, `scanner.py`, `notify.py`, `data.py`,
   `index.html`, `requirements.txt`, `Procfile`, `runtime.txt`, `DEPLOY.md`
2. **New site:** github.com → **+** → **New repository** → name `signal-engine` → **Private** → **Create**
   → click **"uploading an existing file"**.
   **Updating:** open your `signal-engine` repo → **Add file** → **Upload files**.
3. Drag in **all 16 files** → **Commit changes**. (Same names replace the old ones.)

---

## Step 3 — Deploy free on Render (new site only)

1. **render.com** → sign up with GitHub → **New +** → **Web Service** → pick **signal-engine**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Language | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |
   | Instance Type | Free |

3. Click **Deploy Web Service** and wait for **Live**.

---

## Step 4 — Environment variables (Render → your service → Environment)

| Name | Value | Needed? |
|---|---|---|
| `TWELVE_DATA_API_KEY` | key from Step 1 | yes |
| `APP_PASSWORD` | any password, letters and numbers only | yes (also protects the scan link) |
| `NTFY_TOPIC` | a long secret name, e.g. `hameed-gold-7f3k9q2x` | yes, for phone alerts |
| `DATA_PLAN` | `free` (default), `grow` or `pro` | only after you pay Twelve Data |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | from @BotFather | optional, instead of / as well as ntfy |

Click **Save Changes** — Render redeploys by itself.

> Keep the `NTFY_TOPIC` name secret and hard to guess — anyone who knows it can read your alerts.

---

## Step 5 — Phone notifications (ntfy, free, no account)

1. Install **ntfy** from the App Store / Play Store.
2. Open it → **+** (Subscribe to topic) → type exactly your `NTFY_TOPIC` name → **Subscribe**.
3. On iPhone allow notifications when asked.
4. Open your site → **Scanner** tab → **Send test notification**. Your phone should buzz.

---

## Step 6 — Make it scan every minute (cron-job.org, free)

The free Render plan sleeps when nobody visits. This keeps it awake and runs the scanner.

1. Go to **cron-job.org** → **Sign up** (free) → **Create cronjob**.
2. **URL:** `https://YOUR-SITE.onrender.com/api/scan?key=YOUR_APP_PASSWORD`
   (your Render link + `/api/scan?key=` + the password from Step 4)
3. **Execution schedule:** **Every minute** → **Create**.

Within 2 minutes the **Scanner** tab shows **Status: Running**.

---

## Using it

**Signal tab** — pick a category (Metals, Forex, Indices, Energy, Crypto, Commodities, Bonds), a market and a
timeframe, tap **Get signal**. You get BUY/SELL now with a grade, or a limit order, or the reason there's no trade.

**Scanner tab**
- Tick the markets to watch. Gold ★ is always scanned first.
- **Gold checks / Others checks:** how often each is checked. The **Daily data budget** bar shows if your
  choice fits the free plan (800 a day). Default: Gold every 5 min + 8 other markets every 15 min.
- **Alert from:** the lowest grade that buzzes your phone. Default: Gold **A and up**, others **A+ only**.
- Hours: 08:00–21:00 Lagos time, Monday–Friday. Crypto is scanned every day.
- The scanner uses the balance and risk from the Signal tab when you press **Save scanner settings**.

**Top-down analysis before every order**
Daily + 4H bias → 1H location (buy only in discount, sell only in premium) → 15m pullback done →
5m/1m zone entry → stop beyond the 15m pullback swing and no 1H/4H level before TP1.
All steps ✓ = *BUY/SELL now*. Bias + location ✓ but no entry yet = *limit order* at a 15m zone.
Bias unclear or price in the wrong half of the 1H range = *no trade*. The page shows the ✓/✗ checklist.

**Gold notifications**
- New Gold setup (market or limit) that passed the top-down.
- Filled ✅ · +1R → move SL to breakeven 🔒 · TP1 hit 🎯 · stopped ❌ · cancel pending order ⌛
- **HOLD ✅ / CAUTION ⚠️ / WITHDRAW ❌** every 15 min while you're in a trade, and an instant
  WITHDRAW alert if the 15m structure, 1H trend or bias turns against you.
- Gold bias change 🧭, a morning "scanner online" ☀️, and a Gold status every hour 🟡.
- Trade you opened yourself? Enter it under **I'm in a trade** on the Signal tab — you get the same
  HOLD / WITHDRAW alerts for it.

**Valid entries only**
- *BUY/SELL now* appears only when price is reacting at a zone (order block, FVG, Fibonacci 50–61.8%,
  support/resistance, retest, liquidity sweep, pin bar/engulfing at a level). Never on a breakout candle
  alone, never when price is more than 1.5 ATR past the 20 EMA, never when price already left the zone.
- Stop loss goes beyond the pullback swing + buffer (at least 0.8 ATR), so normal pullbacks don't hit it.
- Otherwise you get a *BUY/SELL LIMIT* at the zone instead.
- **Locked setups:** once given, entry/SL/TP stay fixed until the setup is filled, hits TP1 or SL,
  expires, or you tap *Clear this setup*. The status shows Waiting for fill → In trade → TP1 hit / Stopped.

**Grades** (from 29 strategies): A+ ≥ 85 · A ≥ 75 · B ≥ 65. A trade never gets above B if the smallest
lot (0.01) would risk much more than your risk %.

## Free plan limits — read this

- Free Twelve Data = **forex, metals and crypto only**, 8 requests a minute, 800 a day.
  Indices, oil, gas and copper need **Grow** ($29+/month); bonds need **Pro**. They show 🔒 until you set `DATA_PLAN`.
- Gold every **1 minute** all day does not fit the free plan (needs ~830 a day for Gold alone).
- Codes for Platinum, Palladium, indices, energy, copper and bonds weren't confirmed. If one shows an
  error after upgrading, send the message to Claude.
- Scanner settings reset to the defaults if Render restarts the app (e.g. after you upload new files). Just re-save them.

## Not getting notifications?

Open the **Scanner** tab → **Why am I not getting alerts?** lists exactly what's missing. Usually one of:
1. `TWELVE_DATA_API_KEY` not set → the badge says DEMO and nothing is sent.
2. `NTFY_TOPIC` not set, or the ntfy app is subscribed to a different name (must match exactly).
3. The cron-job.org link isn't running → Status says "Waiting…". Check the URL ends with `/api/scan?key=YOUR_PASSWORD`.
4. Outside 08:00–21:00 Lagos time or at the weekend → no scans, that's normal.
Tap **Send test notification** — if your phone buzzes, alerts work; the hourly Gold status confirms it all day.

## If something goes wrong

- **Status: Waiting for the every-minute scan link** → check the cron-job.org URL and password (Step 6).
- **Phone alerts: Not set up** → `NTFY_TOPIC` missing in Render Environment.
- **"Data limit … reached"** → too many taps in one minute, or the 800 daily limit — wait.
- **Page won't open** → Render → **Logs**, send the red error text to Claude.
- **Gold price differs from MT5** → place the trade on your MT5 price with the same SL/TP distances.
