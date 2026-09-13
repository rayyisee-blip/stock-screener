"""
ingest/prices.py
=================
Fetches daily OHLCV (Open/High/Low/Close/Volume) price bars for a list of
tickers and upserts them into the `ohlcv` table in Postgres.

Primary source: yfinance (free, no API key, covers all five target markets
with one ticker convention: 'AAPL', 'SAP.DE', '600519.SS', '2330.TW',
'D05.SI', etc).

Fallback source: Stooq's free CSV export, fetched directly with `requests`
(NOT via pandas-datareader - see the long comment above STOOQ_CSV_URL for
why). This mostly helps for US-listed tickers; Stooq's coverage of
non-US markets is patchy, so a ticker that fails on both sources is simply
skipped and logged, which is expected and fine (one bad ticker must never
abort a whole ingest run).
"""

import io
import logging
import time
from datetime import date
from typing import Optional

import pandas as pd
import psycopg2.extras
import requests
import yfinance as yf
import yfinance.exceptions as yf_exceptions

logger = logging.getLogger(__name__)

# --- Tuning knobs -------------------------------------------------------
# Keep all the "how hard do we hit the free APIs" numbers in one place so
# they're easy to find and adjust later.
BATCH_SIZE = 50  # max tickers per yf.download() call, per the project brief
SLEEP_BETWEEN_BATCHES_SECONDS = 1.5
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2.0  # doubles on each retry: 2s, 4s, 8s

# Stooq's public "download as CSV" endpoint. pandas-datareader used to have
# a StooqDailyReader, but as of the current pandas-datareader release
# (0.11.1) EVERY data source except a small allow-list raises
# NotImplementedError - Stooq support was removed upstream - and the last
# version that still had it (0.10.0) fails to even import against modern
# pandas. So instead we talk to Stooq's CSV endpoint ourselves with
# `requests`. Note: from some networks Stooq serves a JavaScript
# "verifying your browser" anti-bot page instead of CSV data, which makes
# this fallback best-effort rather than guaranteed - if that happens the
# CSV parse below fails, we log it, and the ticker is skipped like any
# other failed fetch.
STOOQ_CSV_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
STOOQ_REQUEST_HEADERS = {
    # A plain browser-like User-Agent; Stooq's endpoint sometimes refuses
    # requests that look like they come from a script with no User-Agent.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _chunk_list(items: list, size: int):
    """Yield successive `size`-sized chunks from `items`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _ticker_to_stooq_symbol(ticker: str) -> str:
    """
    Best-effort translation from a yfinance ticker to a Stooq symbol.

    Stooq's own convention differs from yfinance's:
      - Plain US tickers need a '.us' suffix, e.g. 'AAPL' -> 'aapl.us'.
      - Tickers that already carry a market suffix (e.g. 'SAP.DE') are
        passed through lowercased and used as-is; Stooq may or may not
        recognise it depending on the market, which is fine because a
        failed fallback is simply logged and skipped.
    """
    if "." in ticker:
        return ticker.lower()
    return f"{ticker.lower()}.us"


def _fetch_batch_from_yfinance(
    tickers: list[str], period: str
) -> dict[str, Optional[pd.DataFrame]]:
    """
    Download daily bars for up to BATCH_SIZE tickers in one yfinance call,
    retrying with exponential backoff if Yahoo Finance rate-limits us.

    Returns a dict mapping every requested ticker to either a DataFrame
    (columns: Open, High, Low, Close, Adj Close, Volume) or None if no
    usable data came back for that ticker.
    """
    backoff_seconds = INITIAL_BACKOFF_SECONDS
    raw = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # group_by='ticker' guarantees a consistent (Ticker, Price)
            # column structure whether we ask for 1 ticker or 50.
            raw = yf.download(
                tickers,
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
            break
        except yf_exceptions.YFRateLimitError as e:
            logger.warning(
                "yfinance rate-limited us (attempt %d/%d): %s. "
                "Backing off %.1fs before retrying.",
                attempt,
                MAX_RETRIES,
                e,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
            backoff_seconds *= 2
        except Exception as e:
            # Any other yfinance/network failure for the whole batch - log
            # it and treat every ticker in this batch as "no data", so the
            # caller can try the Stooq fallback per ticker.
            logger.warning(
                "yfinance batch download failed (attempt %d/%d) for %s: %s",
                attempt,
                MAX_RETRIES,
                tickers,
                e,
            )
            time.sleep(backoff_seconds)
            backoff_seconds *= 2

    results: dict[str, Optional[pd.DataFrame]] = {t: None for t in tickers}
    if raw is None or raw.empty:
        return results

    available_tickers = set(raw.columns.get_level_values(0))
    for ticker in tickers:
        if ticker not in available_tickers:
            continue
        sub = raw[ticker].dropna(how="all")
        # A ticker with zero usable rows (e.g. delisted) counts as failed.
        if sub.empty or sub["Close"].dropna().empty:
            continue
        results[ticker] = sub
    return results


def _fetch_from_stooq(ticker: str) -> Optional[pd.DataFrame]:
    """
    Try to fetch daily bars for one ticker from Stooq's free CSV export.
    Returns a DataFrame shaped like the yfinance one, or None on any
    failure (network error, unrecognised symbol, or Stooq's anti-bot page
    instead of real data).
    """
    symbol = _ticker_to_stooq_symbol(ticker)
    url = STOOQ_CSV_URL.format(symbol=symbol)
    try:
        response = requests.get(url, headers=STOOQ_REQUEST_HEADERS, timeout=15)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text))
        expected_columns = {"Date", "Open", "High", "Low", "Close", "Volume"}
        if not expected_columns.issubset(df.columns):
            # This is what happens when Stooq returns an HTML error/anti-bot
            # page instead of a CSV - the columns won't match, so we treat
            # it as "no data" rather than crashing on a KeyError later.
            logger.warning(
                "Stooq fallback returned unexpected content for %s (symbol '%s')",
                ticker,
                symbol,
            )
            return None
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        df["Adj Close"] = df["Close"]  # Stooq's free CSV has no separate adjusted close
        return df
    except Exception as e:
        logger.warning("Stooq fallback failed for %s (symbol '%s'): %s", ticker, symbol, e)
        return None


def fetch_ohlcv(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars for a list of tickers, batching yfinance calls
    and falling back to Stooq for any ticker yfinance couldn't provide.

    `period` follows yfinance's convention, e.g. '5d', '1mo', '1y', '2y'.
    Use a short period for frequent refreshes and a long one (e.g. '2y')
    the first time a ticker is added, so there is enough history to
    compute a 200-day moving average later.

    Returns a dict of {ticker: DataFrame}. Tickers that failed on both
    sources are simply absent from the result (and logged) - the caller
    does not need to handle a None value.
    """
    all_results: dict[str, pd.DataFrame] = {}
    batches = list(_chunk_list(tickers, BATCH_SIZE))

    for batch_index, batch in enumerate(batches):
        batch_results = _fetch_batch_from_yfinance(batch, period)

        for ticker, df in batch_results.items():
            if df is not None:
                all_results[ticker] = df
                continue

            logger.info("No yfinance data for %s, trying Stooq fallback", ticker)
            fallback_df = _fetch_from_stooq(ticker)
            if fallback_df is not None:
                all_results[ticker] = fallback_df
            else:
                logger.warning(
                    "Skipping %s - no data from yfinance or Stooq", ticker
                )

        # Be polite to the free API and avoid tripping rate limits, but
        # don't sleep after the very last batch.
        if batch_index < len(batches) - 1:
            time.sleep(SLEEP_BETWEEN_BATCHES_SECONDS)

    return all_results


def upsert_ohlcv(conn, ticker: str, df: pd.DataFrame, source: str) -> int:
    """
    Insert (or update, if the same ticker+date already exists) rows from
    `df` into the `ohlcv` table.

    `conn` is a psycopg2 connection (see db/connection.py). This function
    does not commit or close the connection - the caller controls the
    transaction so multiple tickers can be committed together.

    Returns the number of rows written.
    """
    rows = []
    for row_date, row in df.iterrows():
        # NaN (from a day with missing data) must become Python None, or
        # psycopg2 will try to insert the literal float NaN, which
        # Postgres's NUMERIC column type rejects.
        def clean(value):
            return None if pd.isna(value) else float(value)

        rows.append(
            (
                ticker,
                row_date.date() if hasattr(row_date, "date") else row_date,
                clean(row.get("Open")),
                clean(row.get("High")),
                clean(row.get("Low")),
                clean(row.get("Close")),
                clean(row.get("Adj Close")),
                None if pd.isna(row.get("Volume")) else int(row.get("Volume")),
                source,
            )
        )

    if not rows:
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO ohlcv (ticker, date, open, high, low, close, adj_close, volume, source)
            VALUES %s
            ON CONFLICT (ticker, date) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume,
                source = EXCLUDED.source,
                fetched_at = NOW()
            """,
            rows,
        )
    return len(rows)


if __name__ == "__main__":
    # Quick manual smoke test - run "python -m ingest.prices" to fetch a
    # handful of tickers (one per target market) and print how many rows
    # came back for each. This does NOT touch the database.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    demo_tickers = ["AAPL", "SAP.DE", "600519.SS", "2330.TW", "D05.SI"]
    data = fetch_ohlcv(demo_tickers, period="3mo")
    for demo_ticker in demo_tickers:
        if demo_ticker in data:
            print(f"{demo_ticker}: {len(data[demo_ticker])} rows")
        else:
            print(f"{demo_ticker}: FAILED (see warnings above)")
