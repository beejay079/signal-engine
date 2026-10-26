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

1. Unzip `signal-site.zip`. You'll see 12 files:
   `app.py`, `signal_engine.py`, `limit_orders.py`, `markets.py`, `scanner.py`, `notify.py`, `data.py`,
   `index.html`, `requirements.txt`, `Procfile`, `runtime.txt`, `DEPLOY.md`
2. **New site:** github.com → **+** → **New repository** → name `signal-engine` → **Private** → **Create**
   → click **"uploading an existing file"**.
   **Updating:** open your `signal-engine` repo → **Add file** → **Upload files**.
3. Drag in **all 12 files** → **Commit changes**. (Same names replace the old ones.)

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

**Grades** (from 29 strategies): A+ ≥ 85 · A ≥ 75 · B ≥ 65. A trade never gets above B if the smallest
lot (0.01) would risk much more than your risk %.

## Free plan limits — read this

- Free Twelve Data = **forex, metals and crypto only**, 8 requests a minute, 800 a day.
  Indices, oil, gas and copper need **Grow** ($29+/month); bonds need **Pro**. They show 🔒 until you set `DATA_PLAN`.
- Gold every **1 minute** all day does not fit the free plan (needs ~830 a day for Gold alone).
- Codes for Platinum, Palladium, indices, energy, copper and bonds weren't confirmed. If one shows an
  error after upgrading, send the message to Claude.
- Scanner settings reset to the defaults if Render restarts the app (e.g. after you upload new files). Just re-save them.

## If something goes wrong

- **Status: Waiting for the every-minute scan link** → check the cron-job.org URL and password (Step 6).
- **Phone alerts: Not set up** → `NTFY_TOPIC` missing in Render Environment.
- **"Data limit … reached"** → too many taps in one minute, or the 800 daily limit — wait.
- **Page won't open** → Render → **Logs**, send the red error text to Claude.
- **Gold price differs from MT5** → place the trade on your MT5 price with the same SL/TP distances.
