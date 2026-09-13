"""
ingest/universe.py
====================
Manages the `universe` table: the list of every ticker the screener knows
about.

Two ways tickers get into the universe:
  1. seed_universe() loads db/universe_seed.csv - a starter list built
     from the real, current S&P 500 constituents plus a curated set of
     well-known large caps for the EU/China/Hong Kong/Taiwan/Singapore
     markets, major ETFs, and the index tickers named in the project
     brief. See the comment at the top of that CSV for exactly what it
     does and doesn't cover, and how to expand it later.
  2. add_user_ticker() lets a user add any other valid yfinance ticker
     through the Streamlit UI - it stays in the universe permanently.

Both paths are idempotent: running seed_universe() again just refreshes
the name/sector/etc for tickers already there, it never creates
duplicates (the `universe` table's ticker column is a PRIMARY KEY).
"""

import csv
import logging
import os

import psycopg2.extras
import yfinance as yf

logger = logging.getLogger(__name__)

DEFAULT_SEED_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "universe_seed.csv")

# Maps a yfinance ticker suffix to one of our five target markets. Any
# suffix not in this dict (e.g. a Japanese ".T" ticker a user adds) falls
# back to "OTHER" - the screener will still work for it, it just won't
# match a specific market filter in the UI.
SUFFIX_TO_MARKET = {
    ".DE": "EU", ".PA": "EU", ".AS": "EU", ".L": "EU", ".MI": "EU",
    ".SS": "CN", ".SZ": "CN",
    ".HK": "HK",
    ".TW": "TW", ".TWO": "TW",
    ".SI": "SG",
}


def infer_market_from_ticker(ticker: str) -> str:
    """Guess which market a ticker belongs to from its yfinance suffix."""
    for suffix, market in SUFFIX_TO_MARKET.items():
        if ticker.endswith(suffix):
            return market
    if "." not in ticker and not ticker.startswith("^"):
        return "US"
    return "OTHER"


def seed_universe(conn, csv_path: str = DEFAULT_SEED_CSV_PATH) -> int:
    """
    Load db/universe_seed.csv into the `universe` table.

    Uses ON CONFLICT (ticker) DO UPDATE so re-running this is always safe
    - it refreshes the name/sector/etc for tickers already in the table
    without creating duplicates, and it never touches `active` or
    `added_by_user`, so a ticker the user has deactivated or added
    themselves is left alone.
    """
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [
            (
                row["ticker"],
                row["name"],
                row["market"],
                row["sector"] or None,
                row["is_index"].strip().lower() == "true",
                row["is_etf"].strip().lower() == "true",
                row["source_index"],
            )
            for row in reader
        ]

    if not rows:
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO universe (ticker, name, market, sector, is_index, is_etf, source_index)
            VALUES %s
            ON CONFLICT (ticker) DO UPDATE SET
                name = EXCLUDED.name,
                market = EXCLUDED.market,
                sector = EXCLUDED.sector,
                is_index = EXCLUDED.is_index,
                is_etf = EXCLUDED.is_etf,
                source_index = EXCLUDED.source_index
            """,
            rows,
        )
    return len(rows)


def add_user_ticker(conn, ticker: str) -> bool:
    """
    Add a ticker a user typed into the UI, after checking it actually
    returns data from yfinance. Returns True if it was added (or was
    already in the universe), False if yfinance doesn't recognize it.
    """
    ticker = ticker.strip().upper()
    try:
        check = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=False)
        if check.empty:
            logger.warning("Rejected user-added ticker %s: no data from yfinance", ticker)
            return False
    except Exception as e:
        logger.warning("Rejected user-added ticker %s: %s", ticker, e)
        return False

    market = infer_market_from_ticker(ticker)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO universe (ticker, name, market, added_by_user, source_index)
            VALUES (%s, %s, %s, TRUE, 'USER_ADDED')
            ON CONFLICT (ticker) DO NOTHING
            """,
            (ticker, ticker, market),
        )
    return True


def refresh_market_cap(conn, ticker: str) -> None:
    """
    Update one ticker's market_cap in the universe table from yfinance's
    lightweight `fast_info` (much cheaper than the full `.info` dict).
    Non-fatal: if yfinance doesn't have a market cap for this ticker (e.g.
    some indices), we just log it and move on - the Screener tab's market
    cap filter simply treats a NULL market cap as "unknown".
    """
    try:
        # yfinance's FastInfo object looks dict-like, but its .get() method
        # does NOT behave like a normal dict's - it always returns None,
        # even for a key that is really there. Indexing with [...] is what
        # actually works, so we use that and catch the KeyError ourselves.
        market_cap = yf.Ticker(ticker).fast_info["market_cap"]
    except Exception as e:
        logger.warning("Could not refresh market cap for %s: %s", ticker, e)
        return

    if market_cap is None:
        return

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE universe SET market_cap = %s WHERE ticker = %s",
            (float(market_cap), ticker),
        )


def refresh_description(conn, ticker: str) -> None:
    """
    Fetch a brief "what does this company do" summary from yfinance and
    store it - but only the first time. A business summary changes rarely,
    and yfinance's full `.info` dict (the only place this field lives) is
    a much heavier call than the `fast_info` used for market cap, so once
    we have a description for a ticker we never fetch it again.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT description FROM universe WHERE ticker = %s", (ticker,))
        row = cur.fetchone()
    if row and row[0]:
        return

    try:
        summary = yf.Ticker(ticker).info.get("longBusinessSummary")
    except Exception as e:
        logger.warning("Could not fetch description for %s: %s", ticker, e)
        return
    if not summary:
        return

    with conn.cursor() as cur:
        cur.execute("UPDATE universe SET description = %s WHERE ticker = %s", (summary, ticker))


def get_active_tickers(conn, tickers_only: list[str] = None) -> list[str]:
    """Return every active ticker in the universe, optionally filtered to a specific list."""
    with conn.cursor() as cur:
        if tickers_only:
            cur.execute(
                "SELECT ticker FROM universe WHERE active = TRUE AND ticker = ANY(%s)",
                (tickers_only,),
            )
        else:
            cur.execute("SELECT ticker FROM universe WHERE active = TRUE")
        return [row[0] for row in cur.fetchall()]
