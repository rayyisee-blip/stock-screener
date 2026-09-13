"""
app/detail_tab.py
===================
Tab 4: Detail - a deep dive on one ticker: a price chart with moving
average overlays, the five rating buckets as a bar breakdown (so the
rating is never a black box), and its last 10 headlines with sentiment.
"""

import pandas as pd
import streamlit as st

from app.data import (
    get_all_active_tickers_with_names,
    get_latest_rating,
    get_price_and_indicator_history,
    get_recent_news,
    get_ticker_description,
    get_watchlist_tickers,
)


def render():
    st.subheader("Detail")

    tickers_df = get_all_active_tickers_with_names()
    if tickers_df.empty:
        st.info("The universe is empty - run the ingest pipeline first.")
        return

    # Default to the first watchlist ticker if there is one, since that's
    # probably what the user wants to look at first.
    watchlist_tickers = get_watchlist_tickers()
    default_index = 0
    ticker_list = tickers_df["ticker"].tolist()
    if watchlist_tickers and watchlist_tickers[0] in ticker_list:
        default_index = ticker_list.index(watchlist_tickers[0])

    ticker = st.selectbox(
        "Ticker",
        ticker_list,
        index=default_index,
        format_func=lambda t: f"{t} - {tickers_df.set_index('ticker').loc[t, 'name']}",
        key="detail_ticker",
    )

    description = get_ticker_description(ticker)
    if description:
        st.caption(description)

    # --- Price chart with moving average overlays -----------------------
    st.markdown("#### Price with moving averages")
    history = get_price_and_indicator_history(ticker, lookback_days=400)
    if history.empty:
        st.warning(f"No price history stored yet for {ticker}. Run the ingest pipeline for this ticker first.")
    else:
        chart_columns = [c for c in ["close", "sma_20", "sma_50", "sma_200"] if history[c].notna().any()]
        st.line_chart(history[chart_columns], width="stretch")

    # --- Five bucket scores as a bar breakdown -----------------------
    st.markdown("#### Rating breakdown")
    rating = get_latest_rating(ticker)
    if rating is None:
        st.warning(f"No rating computed yet for {ticker}.")
    else:
        label_col, score_col = st.columns([1, 3])
        with label_col:
            st.metric("Composite rating", rating["label"], f"{rating['composite_score']:+.1f}")
            if rating["low_news_coverage"]:
                st.caption("Low news coverage: fewer than 3 recent headlines, so the News bucket is neutral (0).")
        with score_col:
            bucket_scores = pd.DataFrame(
                {
                    "Bucket": ["Trend", "Momentum", "Volatility", "Volume", "News Sentiment"],
                    "Score": [
                        rating["trend_score"],
                        rating["momentum_score"],
                        rating["volatility_score"],
                        rating["volume_score"],
                        rating["news_score"],
                    ],
                }
            ).set_index("Bucket")
            st.bar_chart(bucket_scores, width="stretch", horizontal=True)

    # --- Last 10 headlines --------------------------------------------
    st.markdown("#### Recent headlines")
    news_df = get_recent_news(ticker, limit=10)
    if news_df.empty:
        st.info(f"No recent headlines stored for {ticker}.")
    else:
        st.dataframe(
            news_df,
            width="stretch",
            hide_index=True,
            column_config={
                "URL": st.column_config.LinkColumn("Link", display_text="Open article"),
                "Sentiment": st.column_config.NumberColumn("Sentiment", format="%.2f"),
            },
        )
