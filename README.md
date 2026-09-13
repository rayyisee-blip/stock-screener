# Stock Screener

A multi-market stock screener that automatically fetches prices and news on a
schedule, computes technical indicators, scores a Buy/Sell rating for each
ticker, and shows it all in a searchable web app with a watchlist and a
performance table.

Every piece of this runs on a permanent **free tier**:

| Piece | What it does | Where it runs |
|---|---|---|
| `ingest/` | Fetches prices/news, computes indicators & ratings, writes to the database | GitHub Actions (free scheduled jobs) |
| Postgres database | Stores everything: universe, prices, indicators, ratings, news, watchlist | Supabase (free Postgres hosting) |
| `app/` | The web page you actually look at | Streamlit Community Cloud (free app hosting) |

The app **never** fetches data live - it only ever reads what the ingest jobs
already computed and saved to the database. This is what keeps it fast and
free: Streamlit's free tier has limited memory and no way to run scheduled
jobs, so all the expensive work happens separately, on a schedule, in GitHub
Actions.

This README assumes no prior deployment experience. Follow the numbered
steps in order.

---

## 1. Create a free Supabase project

[Supabase](https://supabase.com) gives you a free, hosted Postgres database.

1. Go to [supabase.com](https://supabase.com) and sign up (free, no credit
   card required).
2. Click **New Project**. Pick any name and a strong database password -
   **write this password down**, you'll need it in a moment.
3. Wait a minute or two for the project to finish setting up.
4. Once it's ready, click the **Connect** button (near the project name at
   the top of the dashboard).
5. In the panel that opens, under **Connection Method** choose
   **Session pooler** (not "Direct connection" - see the warning box
   below for why), keep **Type** set to **URI**, and copy the string. It
   looks like:

   ```
   postgresql://postgres.xxxxxxxxxxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-REGION.pooler.supabase.com:5432/postgres
   ```

   Replace `[YOUR-PASSWORD]` with the password from step 2. Save this whole
   string somewhere - it's your `DATABASE_URL` and you'll paste it in two
   more places below.

   > **Why Session pooler and not "Direct connection"?** Supabase's Direct
   > connection uses IPv6 by default. GitHub Actions and Streamlit
   > Community Cloud (the two places this `DATABASE_URL` gets used) are
   > both IPv4-only, so a Direct connection string fails there with an
   > error like `could not translate host name`. The Session pooler
   > connection is IPv4-compatible and works from both. (If you ever see
   > that exact error message after following this README, this is almost
   > always the cause - double check you copied the Session pooler string,
   > not the Direct one, and that you didn't accidentally leave a
   > placeholder like `xxxxxxxxxxxx` in the hostname.)

---

## 2. Run the database schema

This creates the six tables the app needs (`universe`, `ohlcv`, `indicators`,
`ratings`, `news`, `watchlist`).

1. In your Supabase project, open the **SQL Editor** (left sidebar).
2. Click **New query**.
3. Open [`db/schema.sql`](db/schema.sql) from this repo, copy its entire
   contents, and paste it into the SQL Editor.
4. Click **Run**. You should see a series of "Success" messages (one per
   table/index created).
5. To double check, open **Table Editor** (left sidebar) - you should see
   6 tables: `universe`, `ohlcv`, `indicators`, `ratings`, `news`,
   `watchlist`.

You only need to do this once. `schema.sql` is safe to re-run later (it uses
`CREATE TABLE IF NOT EXISTS`), but you won't normally need to.

---

## 3. Fork/clone this repo and set GitHub repo secrets

The GitHub Actions workflows in `.github/workflows/` need your database
connection string, but **never as plain text in a file** - that would leak
your password to anyone who reads the repo. Instead, it's stored as an
encrypted GitHub "secret":

1. Push this repo to your own GitHub account if you haven't already
   (or fork it, if you're working from an existing copy).
2. On GitHub, go to your repo → **Settings** → **Secrets and variables** →
   **Actions**.
3. Click **New repository secret**.
   - Name: `DATABASE_URL`
   - Value: the connection string you saved in step 1.
4. Click **Add secret**.

That's it - the three workflows in `.github/workflows/` (`ingest-watchlist.yml`,
`ingest-index.yml`, `ingest-full.yml`) already reference
`${{ secrets.DATABASE_URL }}`, so they'll pick this up automatically the next
time they run.

**Tip:** if your repo is public, GitHub Actions minutes are unlimited on the
free plan. If it's private, the free plan has a monthly minutes cap - the
watchlist workflow runs every 30 minutes on weekdays, so keep an eye on your
usage if you go private.

You can trigger any workflow manually to test it: go to your repo's
**Actions** tab, pick a workflow (e.g. "Ingest - Watchlist"), and click
**Run workflow**.

---

## 4. Run the first ingest manually (from your own computer)

Before waiting for a scheduled GitHub Actions run, it's worth running the
pipeline once yourself so you can see it work and catch any setup mistakes
early.

1. Install Python 3.11 if you don't already have it.
2. Clone the repo and open a terminal in its folder.
3. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Set your `DATABASE_URL` environment variable to the connection string
   from step 1 (do **not** put it in a file that gets committed to git):

   - macOS/Linux (bash/zsh):
     ```bash
     export DATABASE_URL="postgresql://postgres.xxxxxxxxxxxxxxxxxxxx:yourpassword@aws-0-region.pooler.supabase.com:5432/postgres"
     ```
   - Windows (PowerShell):
     ```powershell
     $env:DATABASE_URL = "postgresql://postgres.xxxxxxxxxxxxxxxxxxxx:yourpassword@aws-0-region.pooler.supabase.com:5432/postgres"
     ```

   (Running locally, on your own machine, you *can* use the Direct
   connection string instead if you prefer - the IPv4/IPv6 issue only
   applies to GitHub Actions and Streamlit Cloud. The Session pooler
   string works fine locally too, though, so there's no need to keep two
   different strings around.)

5. Run the smallest scope first:

   ```bash
   python -m ingest.run --scope watchlist
   ```

   On a brand new database, this automatically seeds the universe (about
   600 tickers) and a default 5-ticker watchlist (one per target market:
   `AAPL`, `SAP.DE`, `600519.SS`, `2330.TW`, `D05.SI`), then fetches prices
   and news and computes indicators/ratings for those 5. It should take
   roughly 1-2 minutes and print a summary line like:

   ```
   Ingest run finished: scope=watchlist succeeded=5 failed=0 duration=0:01:30
   ```

6. (Optional, but recommended) Run the tests to confirm your setup:

   ```bash
   pytest tests/
   ```

Once this works, the three GitHub Actions workflows will keep the database
updated automatically on their own schedule - you don't need to run
`ingest.run` by hand again unless you want to.

---

## 5. Deploy the Streamlit app

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with
   your GitHub account (free).
2. Click **New app**, and pick this repo, the branch you want to deploy
   (check what your repo's default branch is actually called - it may be
   `main` or `master`), and set the **Main file path** to `app/main.py`.
3. Before deploying, open **Advanced settings** → **Secrets**, and paste
   (as TOML - note the `=` and quotes, this is not YAML):

   ```toml
   DATABASE_URL = "postgresql://postgres.xxxxxxxxxxxxxxxxxxxx:yourpassword@aws-0-region.pooler.supabase.com:5432/postgres"
   ```

   (Same connection string as before - Streamlit Cloud reads this into
   `os.environ["DATABASE_URL"]` for you, the same way `db/connection.py`
   expects it.)
4. Click **Deploy**. The first build takes a minute or two while it
   installs `requirements.txt`.
5. Once it's live, you'll get a public URL like
   `https://your-app-name.streamlit.app`.

The app is **read-only** against everything except your watchlist - adding
or removing a ticker in the Watchlist tab is the only thing it writes back
to the database.

---

## Day-to-day: how the schedule works

Once steps 1-5 are done, everything runs itself:

- **Every 30 minutes** (weekdays, roughly matching global trading hours):
  `ingest-watchlist.yml` refreshes just your watchlist tickers.
- **Once a day**, after the US market closes: `ingest-index.yml` refreshes
  every individual stock in the universe.
- **Once a week** (Saturday): `ingest-full.yml` refreshes the entire
  universe, including ETFs and indices.

You can watch these run (and see their logs) under your repo's **Actions**
tab on GitHub. If a run fails, GitHub will show you the error in the log -
most failures are just a handful of tickers being temporarily unavailable,
which the pipeline logs and skips without stopping the whole run.

---

## Adding more tickers to the universe

- **Through the app**: use the "Add a ticker" box on the Watchlist tab. Any
  ticker yfinance recognizes works (check the exact symbol on
  [finance.yahoo.com](https://finance.yahoo.com) if you're not sure).
- **In bulk**: edit [`db/universe_seed.csv`](db/universe_seed.csv) and
  re-run `python -m ingest.run` (any scope) - it re-applies this file every
  time, so new rows get picked up automatically. See the comment at the top
  of `ingest/universe.py` for what this starter file does and doesn't cover
  (it includes the real, current S&P 500 list plus a curated set of
  well-known large caps for the other four markets - not the complete
  official STOXX 600 / CSI 300 / Taiwan 50 / STI 30 constituent lists, to
  avoid guessing at ticker symbols that can't be verified).

---

## Project structure

```
db/schema.sql          Postgres table definitions (plain SQL, no ORM)
db/connection.py        Shared DATABASE_URL connection helper
db/universe_seed.csv    Starter list of tickers to seed the universe table

ingest/prices.py        Fetches OHLCV bars (yfinance, with a Stooq fallback)
ingest/indicators.py    Computes technical indicators (pure pandas/numpy)
ingest/news.py          Fetches headlines + scores sentiment (VADER)
ingest/rating.py        Combines indicators + news into a Buy/Sell rating
ingest/universe.py      Seeds/manages the list of tickers
ingest/run.py           Orchestrator - what GitHub Actions actually calls

app/main.py             Streamlit entry point, wires up the four tabs
app/data.py             Every database read/write the app performs
app/screener_tab.py     Tab 1
app/watchlist_tab.py    Tab 2
app/performance_tab.py  Tab 3
app/detail_tab.py       Tab 4

tests/test_indicators.py   RSI/MACD correctness tests
.github/workflows/          The three scheduled ingest jobs
```

---

**Educational tool. Not financial advice. Data may be delayed or incorrect.**
