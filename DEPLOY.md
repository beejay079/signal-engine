# Put your Signal Engine online (no code, ~15 minutes)

You will do 3 things once: get a data key, upload the files to GitHub, and deploy on Railway.
After that you just open a link on your phone and tap **Get signal**.

---

## Step 1 — Get your free live-data key (Twelve Data)

1. Go to **twelvedata.com** and click **Sign up** (free plan).
2. After logging in, open **API Keys** in your dashboard.
3. Copy the key and keep it somewhere (Notes app). You need it in Step 3.

---

## Step 2 — Upload the files to GitHub

1. Unzip `signal-web.zip` on your Mac. You will see these files:
   `app.py`, `signal_engine.py`, `index.html`, `requirements.txt`, `Procfile`, `runtime.txt`, `DEPLOY.md`
2. Go to **github.com** → click **+** (top right) → **New repository**.
3. Name it `signal-engine`, choose **Private**, click **Create repository**.
4. On the next page click the link **"uploading an existing file"**.
5. Select **all** the files from the unzipped folder and drag them into the page.
6. Click **Commit changes**.

---

## Step 3 — Deploy on Railway

1. Go to **railway.app** → **New Project** → **Deploy from GitHub repo**.
2. Pick **signal-engine**. (If it's not listed, click **Configure GitHub App** and allow access to that repo.)
3. When the project opens, click the service → **Variables** tab → **New Variable**, and add:

   | Name | Value |
   |---|---|
   | `TWELVE_DATA_API_KEY` | the key from Step 1 |
   | `APP_PASSWORD` | any password you choose (stops strangers using your data limit) |

4. Go to the **Settings** tab → **Networking** → click **Generate Domain**.
5. Wait for the deploy to show **Active** (about 2 minutes), then open the domain link.

The badge in the top right of the page should say **LIVE**.
If it says **DEMO data**, the key from Step 1 isn't set correctly in Variables.

---

## Step 4 — Put it on your phone home screen

- **iPhone (Safari):** open the link → **Share** → **Add to Home Screen**.
- **Android (Chrome):** open the link → **⋮** menu → **Add to Home screen**.

The first time you tap **Get signal**, it asks for your `APP_PASSWORD`. It remembers it after that.

---

## How to use it

- Pick the pair, pick **1 min** or **5 min** for entry, set your balance and risk once (it remembers).
- Tap **Get signal**.
  - **BUY now / SELL now** → enter at market immediately with the SL, TP and lot shown. Tap any number to copy it.
  - **No trade** → it tells you why. "Waiting for a 1min entry trigger" means direction is set — try again in a minute.
- Turn on **Watch** and leave the page open: it checks every 30 seconds and beeps/vibrates when a trade appears.
- **Time stop:** if TP1 isn't hit by the time shown, close the trade.

## If something goes wrong

- **"Twelve Data limit reached"** → the free plan allows 8 checks per minute. Wait a minute.
- **Page won't open** → in Railway, open **Deployments** and send the red error text to Claude.
- **Gold price differs from MT5** → Twelve Data and Exness prices can differ slightly. Always place the trade using your MT5 price and the same SL/TP distances.
