"""
ingest/news.py
================
Fetches recent headlines for a ticker from three free, no-API-key sources,
scores each headline's sentiment with VADER, and upserts the results into
the `news` table.

Sources (see the project brief for why these three):
  1. Yahoo Finance's per-ticker RSS feed - the most directly relevant to
     one specific ticker.
  2. Google News RSS, searched by company name - catches coverage Yahoo's
     feed misses.
  3. GDELT 2.0 Doc API - broader macro/news coverage. GDELT's free API
     enforces a strict "1 request every 5 seconds" rate limit, so unlike
     the RSS sources, GDELT should only be called for a small number of
     tickers per run (e.g. the watchlist), never for the whole ~1,200
     ticker universe - see ingest/run.py for how the scopes use this.

Sentiment: VADER (vaderSentiment), NOT a transformer model like FinBERT -
transformers are far too heavy for a free GitHub Actions runner's memory
and time budget, and VADER is plenty for short headlines.
"""

import calendar
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import feedparser
import psycopg2.extras
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)

# One shared VADER analyzer - it loads a lexicon file, so we build it once
# instead of once per headline.
_sentiment_analyzer = SentimentIntensityAnalyzer()

YAHOO_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT asks free-tier callers to wait at least 5 seconds between requests.
GDELT_MIN_SECONDS_BETWEEN_CALLS = 5.5
_gdelt_last_call_time = 0.0

# Only headlines from the last 72 hours count toward the rating engine's
# News Sentiment bucket (see ingest/rating.py), so there's no point storing
# anything older than that for a ticker we've already ingested before.
NEWS_LOOKBACK_HOURS = 72


def score_sentiment(headline: str) -> float:
    """
    Run VADER on a headline and return its "compound" score: a single
    number from -1.0 (very negative) to +1.0 (very positive) that
    summarizes the whole sentence.
    """
    return _sentiment_analyzer.polarity_scores(headline)["compound"]


def _feedparser_entry_to_row(entry, ticker: str, source: str) -> Optional[dict]:
    """Turn one feedparser entry into a plain dict ready for the news table."""
    headline = entry.get("title")
    url = entry.get("link")
    if not headline or not url:
        return None

    published_at = None
    if entry.get("published_parsed"):
        # feedparser already normalizes this to a UTC time.struct_time.
        published_at = datetime.fromtimestamp(
            calendar.timegm(entry.published_parsed), tz=timezone.utc
        )

    # Google News RSS entries carry the real outlet name in entry.source;
    # Yahoo's feed doesn't, so we just label those "Yahoo Finance".
    outlet = None
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        outlet = entry.source.title

    return {
        "ticker": ticker,
        "headline": headline,
        "url": url,
        "source": source if outlet is None else outlet,
        "published_at": published_at,
        "sentiment_compound": score_sentiment(headline),
    }


def fetch_yahoo_rss(ticker: str) -> list[dict]:
    """Fetch headlines from Yahoo Finance's free per-ticker RSS feed."""
    url = YAHOO_RSS_URL.format(ticker=urllib.parse.quote(ticker))
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            logger.warning("Yahoo RSS feed for %s failed to parse: %s", ticker, feed.get("bozo_exception"))
            return []
        rows = [_feedparser_entry_to_row(e, ticker, "yahoo_rss") for e in feed.entries]
        return [r for r in rows if r is not None]
    except Exception as e:
        logger.warning("Yahoo RSS fetch failed for %s: %s", ticker, e)
        return []


def fetch_google_news_rss(ticker: str, company_name: Optional[str] = None) -> list[dict]:
    """Fetch headlines from Google News RSS, searched by company name (falls back to ticker)."""
    query_text = company_name or ticker
    url = GOOGLE_NEWS_RSS_URL.format(query=urllib.parse.quote(query_text))
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            logger.warning("Google News RSS for %s ('%s') failed to parse: %s", ticker, query_text, feed.get("bozo_exception"))
            return []
        rows = [_feedparser_entry_to_row(e, ticker, "google_news_rss") for e in feed.entries]
        return [r for r in rows if r is not None]
    except Exception as e:
        logger.warning("Google News RSS fetch failed for %s ('%s'): %s", ticker, query_text, e)
        return []


def fetch_gdelt(ticker: str, query_text: Optional[str] = None, max_records: int = 25) -> list[dict]:
    """
    Fetch broader macro/news coverage from GDELT's free Doc API.

    IMPORTANT: GDELT rate-limits free callers to about 1 request per 5
    seconds. This function sleeps as needed to respect that, so only call
    it for a handful of tickers per run (e.g. the watchlist) - never loop
    it over the full ~1,200 ticker universe.
    """
    global _gdelt_last_call_time

    elapsed = time.monotonic() - _gdelt_last_call_time
    if elapsed < GDELT_MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(GDELT_MIN_SECONDS_BETWEEN_CALLS - elapsed)

    params = {
        "query": query_text or ticker,
        "mode": "artlist",
        "maxrecords": str(max_records),
        "format": "json",
        "sort": "datedesc",
    }
    try:
        response = requests.get(GDELT_DOC_API_URL, params=params, timeout=20)
        _gdelt_last_call_time = time.monotonic()
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning("GDELT fetch failed for %s: %s", ticker, e)
        _gdelt_last_call_time = time.monotonic()
        return []

    rows = []
    for article in data.get("articles", []):
        headline = article.get("title")
        url = article.get("url")
        if not headline or not url:
            continue
        published_at = None
        seen_date = article.get("seendate")  # format: '20260909T130003Z'
        if seen_date:
            try:
                published_at = datetime.strptime(seen_date, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                published_at = None
        rows.append(
            {
                "ticker": ticker,
                "headline": headline,
                "url": url,
                "source": article.get("domain") or "gdelt",
                "published_at": published_at,
                "sentiment_compound": score_sentiment(headline),
            }
        )
    return rows


def collect_news_for_ticker(
    ticker: str, company_name: Optional[str] = None, include_gdelt: bool = False
) -> list[dict]:
    """
    Fetch and score headlines for one ticker from all enabled sources,
    removing duplicate URLs (the same story often shows up in more than
    one feed).

    Set `include_gdelt=True` only for a small set of tickers (e.g. the
    watchlist) - see the rate-limit note on fetch_gdelt().
    """
    rows = fetch_yahoo_rss(ticker) + fetch_google_news_rss(ticker, company_name)
    if include_gdelt:
        rows += fetch_gdelt(ticker, company_name)

    seen_urls = set()
    deduplicated = []
    for row in rows:
        if row["url"] in seen_urls:
            continue
        seen_urls.add(row["url"])
        deduplicated.append(row)
    return deduplicated


def upsert_news(conn, rows: list[dict]) -> int:
    """
    Insert (or update) news rows into the `news` table. `conn` is a
    psycopg2 connection; the caller controls commit/rollback.
    """
    if not rows:
        return 0

    values = [
        (
            r["ticker"],
            r["headline"],
            r["url"],
            r["source"],
            r["published_at"],
            r["sentiment_compound"],
        )
        for r in rows
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO news (ticker, headline, url, source, published_at, sentiment_compound)
            VALUES %s
            ON CONFLICT (ticker, url) DO UPDATE SET
                sentiment_compound = EXCLUDED.sentiment_compound,
                source = EXCLUDED.source,
                fetched_at = NOW()
            """,
            values,
        )
    return len(values)


if __name__ == "__main__":
    # Quick manual smoke test - run "python -m ingest.news" to fetch and
    # score headlines for one ticker. This does NOT touch the database.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    demo_rows = collect_news_for_ticker("AAPL", "Apple Inc")
    print(f"Fetched {len(demo_rows)} unique headlines for AAPL")
    for demo_row in demo_rows[:5]:
        print(f"  [{demo_row['sentiment_compound']:+.2f}] {demo_row['headline']} ({demo_row['source']})")
