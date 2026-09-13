"""
ingest/run.py
===============
The orchestrator: this is the one script GitHub Actions (and you, running
it by hand) actually calls. It wires together every other ingest/ module
into one pipeline, run with:

    python -m ingest.run --scope watchlist
    python -m ingest.run --scope index
    python -m ingest.run --scope full

What "--scope" controls (see the project brief for the full schedule):
  - watchlist: just the tickers on the watchlist table. Meant to run every
    30 minutes while a market is open, so it stays lightweight.
  - index:     every individual stock in the universe (not ETFs/indices).
               Meant to run once a day after the US market closes.
  - full:      the ENTIRE universe, including ETFs and indices. Meant to
               run once a week (Saturday), since it's the heaviest scope.

For each ticker in scope, this script:
  1. Fetches price history (a long backfill the first time, a short
     top-up after that) and upserts it into the `ohlcv` table.
  2. Reads the ticker's full price history back out of Postgres and
     computes indicators from it, upserting into `indicators`.
  3. Fetches recent news headlines and scores their sentiment, upserting
     into `news`.
  4. Computes the composite rating from the latest indicators + recent
     news, upserting into `ratings`.

One ticker's failure is logged and skipped - it never aborts the whole
run (see the project brief's "one bad ticker must never abort a run").
Progress is committed to Postgres after each ticker, not saved up for one
giant commit at the end, so a run that dies partway through still leaves
useful data behind.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

import pandas as pd

from db.connection import get_connection
from ingest import indicators as indicators_module
from ingest import news as news_module
from ingest import prices as prices_module
from ingest import rating as rating_module
from ingest import universe as universe_module

logger = logging.getLogger(__name__)

# A brand new database has an empty watchlist table, which would make
# "--scope watchlist" a no-op with nothing to show. Seed one ticker per
# target market so a first-time run actually produces data to explore.
DEFAULT_WATCHLIST_TICKERS = ["AAPL", "SAP.DE", "600519.SS", "2330.TW", "D05.SI"]

# Below this many stored bars, fetch a long history (enough for SMA-200);
# at or above it, a short top-up is enough to catch new days.
MIN_BARS_BEFORE_TOPUP_ONLY = indicators_module.MIN_BARS_RECOMMENDED
BACKFILL_PERIOD = "2y"
TOPUP_PERIOD = "1mo"


def _ensure_default_watchlist(conn):
    """If the watchlist is empty, seed it with one ticker per target market."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM watchlist")
        (count,) = cur.fetchone()
    if count > 0:
        return

    logger.info("Watchlist is empty - seeding it with default tickers: %s", DEFAULT_WATCHLIST_TICKERS)
    with conn.cursor() as cur:
        for ticker in DEFAULT_WATCHLIST_TICKERS:
            cur.execute(
                "INSERT INTO watchlist (ticker) VALUES (%s) ON CONFLICT (ticker) DO NOTHING",
                (ticker,),
            )
    conn.commit()


def _get_tickers_for_scope(conn, scope: str) -> list[dict]:
    """
    Return the tickers this run should process, as a list of
    {'ticker': ..., 'name': ...} dicts (name is used for Google News
    search queries).
    """
    with conn.cursor() as cur:
        if scope == "watchlist":
            cur.execute(
                """
                SELECT u.ticker, u.name
                FROM watchlist w
                JOIN universe u ON u.ticker = w.ticker
                WHERE u.active = TRUE
                """
            )
        elif scope == "index":
            # The bulk of the universe: individual stocks, not ETFs/indices.
            cur.execute(
                "SELECT ticker, name FROM universe WHERE active = TRUE AND is_index = FALSE AND is_etf = FALSE"
            )
        elif scope == "full":
            cur.execute("SELECT ticker, name FROM universe WHERE active = TRUE")
        else:
            raise ValueError(f"Unknown scope: {scope}")
        return [{"ticker": row[0], "name": row[1]} for row in cur.fetchall()]


def _existing_bar_count(conn, ticker: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ohlcv WHERE ticker = %s", (ticker,))
        (count,) = cur.fetchone()
    return count


def _load_price_history_from_db(conn, ticker: str) -> pd.DataFrame:
    """Read a ticker's full stored price history back out of Postgres, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM ohlcv
            WHERE ticker = %s
            ORDER BY date ASC
            """,
            (ticker,),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df = df.set_index("date")
    # psycopg2 reads Postgres NUMERIC columns back as Python Decimal
    # objects, not float. pandas/numpy math (rolling means, ewm, etc. in
    # ingest/indicators.py) works with float64 and cannot mix with
    # Decimal, so we convert right after reading.
    numeric_columns = ["open", "high", "low", "close", "volume"]
    df[numeric_columns] = df[numeric_columns].astype(float)
    return df


def _load_recent_news_from_db(conn, ticker: str) -> list[dict]:
    """Read this ticker's last 7 days of news back out of Postgres (comfortably covers the rating engine's 72h window)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT published_at, sentiment_compound
            FROM news
            WHERE ticker = %s AND published_at >= NOW() - INTERVAL '7 days'
            """,
            (ticker,),
        )
        rows = cur.fetchall()
    return [{"published_at": r[0], "sentiment_compound": r[1]} for r in rows]


def _process_one_ticker(conn, ticker: str, company_name: str, price_period: str, include_gdelt: bool) -> None:
    """Run the full pipeline (prices -> indicators -> news -> rating) for one ticker."""
    price_data = prices_module.fetch_ohlcv([ticker], period=price_period)
    if ticker not in price_data:
        logger.warning("Skipping %s entirely - no price data from any source", ticker)
        return
    prices_module.upsert_ohlcv(conn, ticker, price_data[ticker], source="yfinance")
    universe_module.refresh_market_cap(conn, ticker)
    universe_module.refresh_description(conn, ticker)

    history_df = _load_price_history_from_db(conn, ticker)
    history_df = history_df.rename(columns=str.lower)
    indicators_df = indicators_module.compute_indicators(history_df)
    indicators_module.upsert_indicators(conn, ticker, indicators_df)

    news_rows = news_module.collect_news_for_ticker(ticker, company_name, include_gdelt=include_gdelt)
    news_module.upsert_news(conn, news_rows)

    latest_indicators_row = indicators_df.iloc[-1].to_dict()
    latest_close = float(history_df["close"].iloc[-1])
    recent_news = _load_recent_news_from_db(conn, ticker)
    rating = rating_module.compute_rating(latest_indicators_row, latest_close, recent_news)
    latest_date = indicators_df.index[-1]
    rating_module.upsert_rating(conn, ticker, latest_date.date() if hasattr(latest_date, "date") else latest_date, rating)

    conn.commit()


def run(scope: str) -> None:
    started_at = datetime.now(timezone.utc)
    logger.info("Starting ingest run: scope=%s at %s", scope, started_at.isoformat())

    conn = get_connection()
    try:
        seeded_count = universe_module.seed_universe(conn)
        conn.commit()
        logger.info("Universe seeded/refreshed: %d rows", seeded_count)

        if scope == "watchlist":
            _ensure_default_watchlist(conn)

        tickers = _get_tickers_for_scope(conn, scope)
        logger.info("Scope '%s' selected %d tickers to process", scope, len(tickers))

        succeeded, failed = 0, 0
        for entry in tickers:
            ticker = entry["ticker"]
            try:
                bar_count = _existing_bar_count(conn, ticker)
                price_period = TOPUP_PERIOD if bar_count >= MIN_BARS_BEFORE_TOPUP_ONLY else BACKFILL_PERIOD
                # GDELT's strict rate limit makes it only practical for the
                # small watchlist scope - see ingest/news.py.
                include_gdelt = scope == "watchlist"

                _process_one_ticker(conn, ticker, entry["name"], price_period, include_gdelt)
                succeeded += 1
            except Exception as e:
                # One bad ticker must never abort the whole run.
                logger.warning("Failed to process %s: %s", ticker, e)
                conn.rollback()
                failed += 1

        logger.info(
            "Ingest run finished: scope=%s succeeded=%d failed=%d duration=%s",
            scope,
            succeeded,
            failed,
            datetime.now(timezone.utc) - started_at,
        )
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Fetch prices/news, compute indicators/ratings, and write them to Postgres.")
    parser.add_argument(
        "--scope",
        required=True,
        choices=["watchlist", "index", "full"],
        help="Which tier of the universe to process (see module docstring for the intended schedule).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(args.scope)


if __name__ == "__main__":
    sys.exit(main())
