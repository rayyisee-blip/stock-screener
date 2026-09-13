"""
app/data.py
============
Every function the Streamlit app uses to talk to Postgres lives here, so
the four tab files (screener_tab.py, watchlist_tab.py, performance_tab.py,
detail_tab.py) never write raw SQL themselves - they just call a function
with a clear name.

IMPORTANT: this app is READ-ONLY against price/indicator/rating/news data.
The only thing it ever writes is the `watchlist` table (add_ticker_to_
watchlist / remove_ticker_from_watchlist below). All the heavy lifting -
fetching prices, computing indicators, scoring ratings - happens in the
ingest/ jobs on a schedule, never here. That's why every read function
below is wrapped in @st.cache_data(ttl=300): the data only changes when
an ingest job runs, so re-querying Postgres on every click would be
wasted work (and Streamlit Community Cloud's free tier has limited
memory/CPU to waste).
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from db.connection import get_connection
from ingest.universe import add_user_ticker

CACHE_TTL_SECONDS = 300
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


def to_singapore_time(utc_datetime: datetime) -> datetime:
    """Convert a UTC datetime (as stored in Postgres) to Singapore time for display."""
    if utc_datetime is None:
        return None
    if utc_datetime.tzinfo is None:
        utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)
    return utc_datetime.astimezone(SINGAPORE_TZ)


def _query(sql: str, params: tuple = ()) -> list[tuple]:
    """Open a connection, run one query, return all rows, always close the connection."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _to_float_columns(df: pd.DataFrame, columns: list) -> pd.DataFrame:
    """
    psycopg2 reads Postgres NUMERIC columns back as Python Decimal
    objects, not float. Streamlit's tables/charts serialize data through
    Apache Arrow and Altair, and both mishandle a column of Decimal
    objects (Altair silently treats it as text, Arrow can render it as an
    empty table) - so every numeric column coming out of Postgres gets
    converted to a plain float here before it reaches any Streamlit
    widget. Missing values (None) safely become NaN.
    """
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _execute(sql: str, params: tuple = ()) -> None:
    """Open a connection, run one write statement, commit, always close the connection."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_last_updated_utc():
    """The most recent time any rating was computed - shown as 'last updated' on every tab."""
    rows = _query("SELECT MAX(computed_at) FROM ratings")
    return rows[0][0] if rows and rows[0][0] else None


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_filter_options() -> dict:
    """Distinct markets/sectors/rating labels currently in the universe, for filter dropdowns."""
    markets = [r[0] for r in _query("SELECT DISTINCT market FROM universe WHERE active = TRUE ORDER BY market")]
    sectors = [r[0] for r in _query("SELECT DISTINCT sector FROM universe WHERE active = TRUE AND sector IS NOT NULL ORDER BY sector")]
    labels = ["Strong Buy", "Buy", "Neutral", "Sell", "Strong Sell"]
    return {"markets": markets, "sectors": sectors, "labels": labels}


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_screener_page(
    market: str = None,
    rating_label: str = None,
    sector: str = None,
    rsi_min: float = None,
    rsi_max: float = None,
    market_cap_min: float = None,
    tickers_only: list = None,
    sort_by: str = "composite_score",
    sort_desc: bool = True,
    page: int = 1,
    page_size: int = 50,
) -> tuple[pd.DataFrame, int]:
    """
    Return one page of the screener table (as a DataFrame) plus the total
    number of matching rows (so the UI can show "page X of Y").

    Each ticker's MOST RECENT rating/indicator row is used - "DISTINCT ON"
    is Postgres's way of saying "just the latest row per ticker".
    """
    where_clauses = ["u.active = TRUE"]
    params = []

    if market:
        where_clauses.append("u.market = %s")
        params.append(market)
    if sector:
        where_clauses.append("u.sector = %s")
        params.append(sector)
    if rating_label:
        where_clauses.append("r.label = %s")
        params.append(rating_label)
    if rsi_min is not None:
        where_clauses.append("i.rsi_14 >= %s")
        params.append(rsi_min)
    if rsi_max is not None:
        where_clauses.append("i.rsi_14 <= %s")
        params.append(rsi_max)
    if market_cap_min is not None:
        where_clauses.append("u.market_cap >= %s")
        params.append(market_cap_min)
    if tickers_only:
        where_clauses.append("u.ticker = ANY(%s)")
        params.append(tickers_only)

    where_sql = " AND ".join(where_clauses)

    # Only these column names are allowed to be sorted on - never build SQL
    # directly out of a value that came from user input.
    allowed_sort_columns = {
        "ticker": "u.ticker", "market": "u.market", "composite_score": "r.composite_score",
        "label": "r.label", "rsi_14": "i.rsi_14", "market_cap": "u.market_cap",
    }
    sort_column = allowed_sort_columns.get(sort_by, "r.composite_score")
    sort_direction = "DESC" if sort_desc else "ASC"

    base_query = f"""
        FROM universe u
        LEFT JOIN LATERAL (
            SELECT * FROM ratings WHERE ratings.ticker = u.ticker ORDER BY date DESC LIMIT 1
        ) r ON TRUE
        LEFT JOIN LATERAL (
            SELECT * FROM indicators WHERE indicators.ticker = u.ticker ORDER BY date DESC LIMIT 1
        ) i ON TRUE
        WHERE {where_sql}
    """

    total_count = _query(f"SELECT COUNT(*) {base_query}", tuple(params))[0][0]

    offset = (page - 1) * page_size
    rows = _query(
        f"""
        SELECT u.ticker, u.name, u.market, u.sector, u.market_cap,
               r.composite_score, r.label, i.rsi_14, i.price_vs_sma50_pct
        {base_query}
        ORDER BY {sort_column} {sort_direction} NULLS LAST
        LIMIT %s OFFSET %s
        """,
        tuple(params) + (page_size, offset),
    )
    df = pd.DataFrame(
        rows,
        columns=["Ticker", "Name", "Market", "Sector", "Market Cap", "Score", "Rating", "RSI (14)", "% vs SMA50"],
    )
    df = _to_float_columns(df, ["Market Cap", "Score", "RSI (14)", "% vs SMA50"])
    return df, total_count


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_watchlist_tickers() -> list[str]:
    return [r[0] for r in _query("SELECT ticker FROM watchlist ORDER BY added_at")]


def add_ticker_to_watchlist(ticker: str) -> bool:
    """
    Add a ticker to the watchlist, validating it against yfinance first if
    it isn't already in the universe. Returns True on success. Clears the
    read caches so the Watchlist tab immediately reflects the change.
    """
    ticker = ticker.strip().upper()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM universe WHERE ticker = %s", (ticker,))
            already_known = cur.fetchone() is not None
        if not already_known:
            if not add_user_ticker(conn, ticker):
                return False
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO watchlist (ticker) VALUES (%s) ON CONFLICT (ticker) DO NOTHING",
                (ticker,),
            )
        conn.commit()
    finally:
        conn.close()

    get_watchlist_tickers.clear()
    get_screener_page.clear()
    return True


def remove_ticker_from_watchlist(ticker: str) -> None:
    _execute("DELETE FROM watchlist WHERE ticker = %s", (ticker,))
    get_watchlist_tickers.clear()


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_price_history_for_tickers(tickers: list, lookback_days: int = 400) -> pd.DataFrame:
    """Long-format (ticker, date, close) history for the Performance tab's % change math."""
    if not tickers:
        return pd.DataFrame(columns=["ticker", "date", "close"])
    rows = _query(
        """
        SELECT ticker, date, close FROM ohlcv
        WHERE ticker = ANY(%s) AND date >= CURRENT_DATE - %s * INTERVAL '1 day'
        ORDER BY ticker, date
        """,
        (tickers, lookback_days),
    )
    df = pd.DataFrame(rows, columns=["ticker", "date", "close"])
    return _to_float_columns(df, ["close"])


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_price_and_indicator_history(ticker: str, lookback_days: int = 400) -> pd.DataFrame:
    """Date-indexed close/SMA history for the Detail tab's price chart."""
    rows = _query(
        """
        SELECT o.date, o.close, i.sma_20, i.sma_50, i.sma_200
        FROM ohlcv o
        LEFT JOIN indicators i ON i.ticker = o.ticker AND i.date = o.date
        WHERE o.ticker = %s AND o.date >= CURRENT_DATE - %s * INTERVAL '1 day'
        ORDER BY o.date
        """,
        (ticker, lookback_days),
    )
    df = pd.DataFrame(rows, columns=["date", "close", "sma_20", "sma_50", "sma_200"])
    df = _to_float_columns(df, ["close", "sma_20", "sma_50", "sma_200"])
    # psycopg2 gives back plain datetime.date objects for a DATE column,
    # not pandas Timestamps. st.line_chart's charting library can't infer
    # a proper axis type from that (it renders a blank chart), so we
    # convert to a real DatetimeIndex before returning.
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_latest_rating(ticker: str) -> dict:
    rows = _query(
        """
        SELECT trend_score, momentum_score, volatility_score, volume_score,
               news_score, composite_score, label, low_news_coverage, date
        FROM ratings WHERE ticker = %s ORDER BY date DESC LIMIT 1
        """,
        (ticker,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        # float(...) here for the same reason _to_float_columns exists:
        # these come back from Postgres as Decimal, and st.bar_chart
        # (used for the bucket breakdown) can't render Decimal values.
        "trend_score": float(r[0]), "momentum_score": float(r[1]), "volatility_score": float(r[2]),
        "volume_score": float(r[3]), "news_score": float(r[4]), "composite_score": float(r[5]),
        "label": r[6], "low_news_coverage": r[7], "date": r[8],
    }


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_recent_news(ticker: str, limit: int = 10) -> pd.DataFrame:
    rows = _query(
        """
        SELECT headline, url, source, published_at, sentiment_compound
        FROM news WHERE ticker = %s
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s
        """,
        (ticker, limit),
    )
    df = pd.DataFrame(rows, columns=["Headline", "URL", "Source", "Published (SGT)", "Sentiment"])
    # Postgres returns TIMESTAMPTZ values converted to whatever timezone
    # the DATABASE CONNECTION happens to be configured with (which varies
    # by server - it is not guaranteed to be UTC), even though the
    # correct INSTANT in time is always preserved. To display consistently
    # in Singapore time regardless of the connection's own timezone
    # setting, we explicitly convert every value here rather than trusting
    # however it already looks.
    df["Published (SGT)"] = df["Published (SGT)"].apply(to_singapore_time)
    return _to_float_columns(df, ["Sentiment"])


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def get_all_active_tickers_with_names() -> pd.DataFrame:
    """Every active ticker + name, used to populate the Detail tab's ticker picker."""
    rows = _query("SELECT ticker, name FROM universe WHERE active = TRUE ORDER BY ticker")
    return pd.DataFrame(rows, columns=["ticker", "name"])
