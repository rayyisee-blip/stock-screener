"""
app/performance_tab.py
========================
Tab 3: Performance - a table of % change in closing price over whatever
periods the user picks (1D, 5D, 10D, 1M, 3M, 6M, YTD, 1Y), computed from
prices already stored in Postgres (never fetched live - see the project
brief's read-only constraint for the Streamlit app).
"""

import pandas as pd
import streamlit as st

from app.data import get_price_history_for_tickers, get_watchlist_tickers

# For "1D"/"5D"/"10D" we count back that many TRADING days (rows in the
# stored history) - this is what "1-day return" conventionally means for
# a stock (yesterday's close to today's close, not literally 24 hours,
# since markets are closed on weekends). For the longer periods we count
# back calendar time instead, since "1 month ago" is a calendar concept.
ROW_BASED_PERIODS = {"1D": 1, "5D": 5, "10D": 10}
MONTH_BASED_PERIODS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}
ALL_PERIODS = list(ROW_BASED_PERIODS) + list(MONTH_BASED_PERIODS) + ["YTD"]


def _pct_change_for_one_period(ticker_history: pd.DataFrame, period: str):
    """
    ticker_history: DataFrame with 'date' (ascending) and 'close' columns
    for ONE ticker. Returns the % change for `period`, or None if there
    isn't enough history yet to answer.
    """
    if ticker_history.empty:
        return None

    latest_close = ticker_history["close"].iloc[-1]
    latest_date = ticker_history["date"].iloc[-1]

    if period in ROW_BASED_PERIODS:
        rows_back = ROW_BASED_PERIODS[period]
        if len(ticker_history) <= rows_back:
            return None
        past_close = ticker_history["close"].iloc[-1 - rows_back]

    elif period in MONTH_BASED_PERIODS:
        months_back = MONTH_BASED_PERIODS[period]
        target_date = latest_date - pd.DateOffset(months=months_back)
        past_rows = ticker_history[ticker_history["date"] <= target_date]
        if past_rows.empty:
            return None
        past_close = past_rows["close"].iloc[-1]

    elif period == "YTD":
        # Year-to-date compares against the last close of the PREVIOUS year.
        target_date = pd.Timestamp(year=latest_date.year - 1, month=12, day=31)
        past_rows = ticker_history[ticker_history["date"] <= target_date]
        if past_rows.empty:
            # No data from last year (e.g. a ticker only just added) - fall
            # back to the earliest close we have this year.
            this_year_rows = ticker_history[ticker_history["date"].dt.year == latest_date.year]
            if this_year_rows.empty:
                return None
            past_close = this_year_rows["close"].iloc[0]
        else:
            past_close = past_rows["close"].iloc[-1]
    else:
        return None

    if past_close is None or pd.isna(past_close) or past_close == 0:
        return None
    return (latest_close - past_close) / past_close * 100


def _build_performance_table(tickers: list, periods: list) -> pd.DataFrame:
    history = get_price_history_for_tickers(tickers, lookback_days=400)
    history["date"] = pd.to_datetime(history["date"])

    rows = []
    for ticker in tickers:
        ticker_history = history[history["ticker"] == ticker].sort_values("date")
        row = {"Ticker": ticker}
        for period in periods:
            row[period] = _pct_change_for_one_period(ticker_history, period)
        rows.append(row)

    return pd.DataFrame(rows)


def render():
    st.subheader("Performance")
    st.caption("% change in closing price, computed from stored prices - not fetched live.")

    watchlist_tickers = get_watchlist_tickers()
    if not watchlist_tickers:
        st.info("Your watchlist is empty - add tickers on the Watchlist tab to see their performance here.")
        return

    selected_periods = st.multiselect(
        "Periods to show", ALL_PERIODS, default=["1D", "5D", "1M", "YTD"], key="performance_periods"
    )
    if not selected_periods:
        st.info("Pick at least one period above.")
        return

    df = _build_performance_table(watchlist_tickers, selected_periods)

    def _color_by_sign(value):
        if value is None or pd.isna(value):
            return ""
        return "color: #1a7431; font-weight: 600" if value >= 0 else "color: #b33939; font-weight: 600"

    styled = df.style.map(_color_by_sign, subset=selected_periods).format(
        {period: "{:+.2f}%" for period in selected_periods}, na_rep="N/A"
    )
    st.dataframe(styled, width="stretch", hide_index=True)
