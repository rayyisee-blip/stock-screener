"""
ingest/rating.py
==================
Turns the technical indicators (from ingest/indicators.py) and recent news
sentiment (from ingest/news.py) into one composite Buy/Sell rating per
ticker, TradingView-style - but every number that goes into the final
score is stored and visible, never a black box.

How scoring works, in plain language:
  1. Each of the five "buckets" (Trend, Momentum, Volatility, Volume,
     News Sentiment) gets its own score from -100 (very bearish) to +100
     (very bullish).
  2. Within Trend and Momentum, several individual signals (e.g. "is the
     price above its 50-day average?", "is RSI high or low?") are each
     converted to a -100..+100 number and averaged into that bucket's
     score. Volatility and Volume use one clear signal each, kept simple
     on purpose so a beginner can trace exactly why a bucket scored the
     way it did.
  3. The five bucket scores are combined into one COMPOSITE score using
     the WEIGHTS below - a simple weighted average.
  4. The composite score is mapped to a human label (Strong Buy .. Strong
     Sell).

Design choices worth knowing about (the project brief left these open):
  - RSI/Stochastic/ROC are scored as MOMENTUM (higher = more bullish
    momentum), not as overbought/oversold reversal signals. A high RSI
    here means "strong upward momentum", not "sell warning".
  - Volatility is scored using where price sits within its Bollinger
    Bands (%B): breaking above the upper band scores bullish, breaking
    below the lower band scores bearish. ATR and Bollinger bandwidth are
    still computed and stored in the `indicators` table for the Detail
    tab to show, but they measure the SIZE of volatility, not its
    direction, so they aren't folded into this directional score.
  - Volume is scored as "does today's volume confirm the current trend?":
    above-average volume while price is above its 20-day average scores
    bullish, above-average volume while price is below its 20-day average
    scores bearish. OBV is still computed and stored for the Detail tab.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2.extras

logger = logging.getLogger(__name__)

# Bucket weights for the composite score. Keep them all in one place, and
# make sure they add up to 1.0, so re-tuning the rating engine later only
# means editing these five numbers.
WEIGHTS = {
    "trend": 0.35,
    "momentum": 0.30,
    "volatility": 0.10,
    "volume": 0.10,
    "news": 0.15,
}

# A headline only counts toward the News Sentiment bucket if it was
# published within this many hours of "now".
NEWS_LOOKBACK_HOURS = 72
# Fewer than this many recent headlines and we don't trust the average
# enough to call it a real signal - the bucket becomes neutral (0) and
# gets flagged so the UI can tell the user why.
MIN_HEADLINES_FOR_NEWS_SCORE = 3


def _safe_float(value) -> Optional[float]:
    """Convert a possibly-NaN/None indicator value into a clean float or None."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def _linear_score(value: Optional[float], full_scale_at: float) -> float:
    """
    Map `value` onto -100..+100, where reaching +/- `full_scale_at`
    counts as maximally bullish/bearish. For example,
    _linear_score(price_vs_sma20_pct, full_scale_at=10) means "10% above
    the 20-day average is as bullish as this signal gets".

    A missing value (None) scores 0 (neutral) rather than being treated
    as bearish - "we don't know" is not the same as "it's bad news".
    """
    if value is None or full_scale_at == 0:
        return 0.0
    score = (value / full_scale_at) * 100
    return max(-100.0, min(100.0, score))


def _sign(value: Optional[float]) -> float:
    if value is None or value == 0:
        return 0.0
    return 1.0 if value > 0 else -1.0


def _score_trend(row: dict) -> float:
    price_vs_sma20 = _safe_float(row.get("price_vs_sma20_pct"))
    price_vs_sma50 = _safe_float(row.get("price_vs_sma50_pct"))
    price_vs_sma200 = _safe_float(row.get("price_vs_sma200_pct"))
    adx_14 = _safe_float(row.get("adx_14"))
    ma_cross_state = row.get("ma_cross_state")

    sma20_component = _linear_score(price_vs_sma20, full_scale_at=10)
    sma50_component = _linear_score(price_vs_sma50, full_scale_at=15)
    sma200_component = _linear_score(price_vs_sma200, full_scale_at=20)

    if ma_cross_state == "golden_cross":
        cross_component = 50.0
    elif ma_cross_state == "death_cross":
        cross_component = -50.0
    else:
        cross_component = 0.0

    # ADX only measures trend STRENGTH (always 0-100), so we point it in
    # the direction the 50-day average says the trend is currently facing.
    adx_component = _sign(price_vs_sma50) * _linear_score(adx_14, full_scale_at=50)

    components = [sma20_component, sma50_component, sma200_component, cross_component, adx_component]
    return sum(components) / len(components)


def _score_momentum(row: dict, close: Optional[float]) -> float:
    rsi_14 = _safe_float(row.get("rsi_14"))
    macd_hist = _safe_float(row.get("macd_hist"))
    stoch_k = _safe_float(row.get("stoch_k"))
    roc_10 = _safe_float(row.get("roc_10"))

    rsi_component = _linear_score(None if rsi_14 is None else rsi_14 - 50, full_scale_at=50)
    stoch_component = _linear_score(None if stoch_k is None else stoch_k - 50, full_scale_at=50)
    roc_component = _linear_score(roc_10, full_scale_at=10)

    # MACD's histogram is in price units (dollars, euros, etc.), so a
    # fixed threshold wouldn't be fair to both a $5 stock and a $500
    # stock. Express it as a percentage of the current price instead.
    if macd_hist is not None and close:
        macd_pct_of_price = (macd_hist / close) * 100
        macd_component = _linear_score(macd_pct_of_price, full_scale_at=0.5)
    else:
        macd_component = 0.0

    components = [rsi_component, macd_component, stoch_component, roc_component]
    return sum(components) / len(components)


def _score_volatility(row: dict) -> float:
    bb_pct_b = _safe_float(row.get("bb_pct_b"))
    # %B = 0.5 means price sits exactly on the middle band (neutral);
    # %B = 1 means price is at the upper band (maximally bullish here);
    # %B = 0 means price is at the lower band (maximally bearish here).
    return _linear_score(None if bb_pct_b is None else bb_pct_b - 0.5, full_scale_at=0.5)


def _score_volume(row: dict) -> float:
    volume_vs_avg20 = _safe_float(row.get("volume_vs_avg20"))
    price_vs_sma20 = _safe_float(row.get("price_vs_sma20_pct"))

    # "Is volume unusually high, and does the current trend agree with
    # that? High volume in an uptrend is bullish confirmation; high
    # volume in a downtrend is bearish confirmation (distribution)."
    volume_ratio_component = _linear_score(
        None if volume_vs_avg20 is None else volume_vs_avg20 - 1.0, full_scale_at=1.0
    )
    return _sign(price_vs_sma20) * volume_ratio_component


def _score_news(news_rows: list[dict], now: Optional[datetime] = None) -> tuple[float, bool]:
    """
    Average the VADER compound sentiment of headlines published in the
    last NEWS_LOOKBACK_HOURS, scaled from VADER's native -1..+1 range to
    the rating engine's -100..+100 range.

    Returns (news_score, low_news_coverage).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=NEWS_LOOKBACK_HOURS)

    recent_scores = []
    for news_row in news_rows:
        published_at = news_row.get("published_at")
        sentiment = news_row.get("sentiment_compound")
        if published_at is None or sentiment is None:
            continue
        if published_at >= cutoff:
            recent_scores.append(float(sentiment))

    if len(recent_scores) < MIN_HEADLINES_FOR_NEWS_SCORE:
        return 0.0, True

    mean_compound = sum(recent_scores) / len(recent_scores)
    return mean_compound * 100, False


def _label_for_composite(composite_score: float) -> str:
    if composite_score >= 50:
        return "Strong Buy"
    if composite_score >= 15:
        return "Buy"
    if composite_score >= -14:
        return "Neutral"
    if composite_score >= -49:
        return "Sell"
    return "Strong Sell"


def compute_rating(
    indicators_row: dict,
    close: Optional[float],
    news_rows: list[dict],
    now: Optional[datetime] = None,
) -> dict:
    """
    Compute the full rating breakdown for one ticker on one date.

    `indicators_row` is one row's worth of values from the `indicators`
    table (a dict or pandas Series both work, since both support .get()).
    `close` is that same date's closing price (used to normalize MACD).
    `news_rows` is a list of dicts with at least 'published_at' (a
    timezone-aware datetime) and 'sentiment_compound' keys - typically
    every news row on record for this ticker; this function does its own
    72-hour filtering.

    Returns a dict with every bucket score, the composite score, the
    label, and the low_news_coverage flag - i.e. everything the `ratings`
    table needs.
    """
    # Cast close to a plain Python float up front. Callers often pass a
    # value straight out of a pandas/numpy column (numpy.float64), and if
    # that leaks into the arithmetic below, every score built from it
    # ends up as numpy.float64 too - which psycopg2 cannot write into a
    # query correctly (it silently produces broken SQL text instead of a
    # number). Stopping it here means every code path after this point is
    # guaranteed to be working with plain Python floats.
    close = _safe_float(close)

    trend_score = _score_trend(indicators_row)
    momentum_score = _score_momentum(indicators_row, close)
    volatility_score = _score_volatility(indicators_row)
    volume_score = _score_volume(indicators_row)
    news_score, low_news_coverage = _score_news(news_rows, now)

    composite_score = (
        WEIGHTS["trend"] * trend_score
        + WEIGHTS["momentum"] * momentum_score
        + WEIGHTS["volatility"] * volatility_score
        + WEIGHTS["volume"] * volume_score
        + WEIGHTS["news"] * news_score
    )

    return {
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "volatility_score": volatility_score,
        "volume_score": volume_score,
        "news_score": news_score,
        "composite_score": composite_score,
        "label": _label_for_composite(composite_score),
        "low_news_coverage": low_news_coverage,
    }


def upsert_rating(conn, ticker: str, rating_date, rating: dict) -> None:
    """
    Insert (or update) one ticker's rating for one date into the
    `ratings` table. `conn` is a psycopg2 connection; the caller controls
    commit/rollback.
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO ratings (
                ticker, date, trend_score, momentum_score, volatility_score,
                volume_score, news_score, composite_score, label, low_news_coverage
            )
            VALUES %s
            ON CONFLICT (ticker, date) DO UPDATE SET
                trend_score = EXCLUDED.trend_score,
                momentum_score = EXCLUDED.momentum_score,
                volatility_score = EXCLUDED.volatility_score,
                volume_score = EXCLUDED.volume_score,
                news_score = EXCLUDED.news_score,
                composite_score = EXCLUDED.composite_score,
                label = EXCLUDED.label,
                low_news_coverage = EXCLUDED.low_news_coverage,
                computed_at = NOW()
            """,
            [
                (
                    ticker,
                    rating_date,
                    rating["trend_score"],
                    rating["momentum_score"],
                    rating["volatility_score"],
                    rating["volume_score"],
                    rating["news_score"],
                    rating["composite_score"],
                    rating["label"],
                    rating["low_news_coverage"],
                )
            ],
        )
