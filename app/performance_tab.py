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

# The windows offered on the "Daily breakdown" view below - a subset of
# ALL_PERIODS (10D/1Y/YTD add little there and would just make the table
# longer without changing the point: seeing each day's move on its own).
BREAKDOWN_WINDOWS = ["1D", "5D", "1M", "3M", "6M"]


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


def _build_performance_table(history: pd.DataFrame, tickers: list, periods: list) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        ticker_history = history[history["ticker"] == ticker].sort_values("date")
        row = {"Ticker": ticker}
        for period in periods:
            row[period] = _pct_change_for_one_period(ticker_history, period)
        rows.append(row)

    return pd.DataFrame(rows)


def _daily_changes_for_ticker(ticker_history: pd.DataFrame, window: str) -> pd.DataFrame:
    """
    One row per trading day within `window` (e.g. "5D", "1M"), each with
    THAT DAY's own % change from the previous close - not the cumulative
    change over the whole window. This is what answers "Monday +5%,
    Tuesday -12%" instead of just "net +... % over the period".
    """
    if ticker_history.empty:
        return pd.DataFrame(columns=["Date", "Close", "% Change"])

    ordered = ticker_history.sort_values("date").reset_index(drop=True)
    ordered["% Change"] = ordered["close"].pct_change() * 100
    latest_date = ordered["date"].iloc[-1]

    if window in ROW_BASED_PERIODS:
        window_rows = ordered.tail(ROW_BASED_PERIODS[window])
    elif window in MONTH_BASED_PERIODS:
        start_date = latest_date - pd.DateOffset(months=MONTH_BASED_PERIODS[window])
        window_rows = ordered[ordered["date"] > start_date]
    else:
        window_rows = ordered.tail(1)

    return window_rows[["date", "close", "% Change"]].rename(columns={"date": "Date", "close": "Close"})


def _color_by_sign(value):
    if value is None or pd.isna(value):
        return ""
    return "color: #1a7431; font-weight: 600" if value >= 0 else "color: #b33939; font-weight: 600"


def render():
    st.subheader("Performance")
    st.caption("% change in closing price, computed from stored prices - not fetched live.")

    watchlist_tickers = get_watchlist_tickers()
    if not watchlist_tickers:
        st.info("Your watchlist is empty - add tickers on the Watchlist tab to see their performance here.")
        return

    history = get_price_history_for_tickers(watchlist_tickers, lookback_days=400)
    history["date"] = pd.to_datetime(history["date"])

    selected_periods = st.multiselect(
        "Periods to show", ALL_PERIODS, default=["1D", "5D", "1M", "YTD"], key="performance_periods"
    )
    if not selected_periods:
        st.info("Pick at least one period above.")
    else:
        df = _build_performance_table(history, watchlist_tickers, selected_periods)
        styled = df.style.map(_color_by_sign, subset=selected_periods).format(
            {period: "{:+.2f}%" for period in selected_periods}, na_rep="N/A"
        )
        st.dataframe(styled, width="stretch", hide_index=True)

    # --- Daily breakdown: each day's own move, not the net over the window ---
    st.divider()
    st.markdown("#### Daily breakdown")
    st.caption(
        "The individual % change for each trading day within the chosen window "
        "(e.g. Monday +5%, Tuesday -12%) - not the overall change across the whole window."
    )

    breakdown_col1, breakdown_col2 = st.columns(2)
    with breakdown_col1:
        breakdown_ticker = st.selectbox("Ticker", watchlist_tickers, key="performance_breakdown_ticker")
    with breakdown_col2:
        breakdown_window = st.selectbox(
            "Window", BREAKDOWN_WINDOWS, index=BREAKDOWN_WINDOWS.index("5D"), key="performance_breakdown_window"
        )

    ticker_history = history[history["ticker"] == breakdown_ticker]
    daily_df = _daily_changes_for_ticker(ticker_history, breakdown_window)

    if daily_df.empty:
        st.info(f"Not enough stored price history yet for {breakdown_ticker}.")
        return

    display_df = daily_df.copy()
    display_df["Date"] = display_df["Date"].dt.strftime("%a %Y-%m-%d")
    styled_daily = display_df.style.map(_color_by_sign, subset=["% Change"]).format(
        {"Close": "{:.2f}", "% Change": "{:+.2f}%"}, na_rep="N/A"
    )
    st.dataframe(styled_daily, width="stretch", hide_index=True)

    chart_data = daily_df.set_index("Date")[["% Change"]]
    st.bar_chart(chart_data, width="stretch")
