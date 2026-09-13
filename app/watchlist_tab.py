"""
app/watchlist_tab.py
======================
Tab 2: Watchlist - add/remove tickers, and see the same screener-style
table filtered to just the saved tickers. This is the ONE place in the
whole app that writes to Postgres (the `watchlist` table).
"""

import streamlit as st

from app.data import (
    add_ticker_to_watchlist,
    get_screener_page,
    get_watchlist_tickers,
    remove_ticker_from_watchlist,
)
from app.formatting import color_by_rating, format_market_cap


def render():
    st.subheader("Watchlist")
    st.caption("Tickers you're tracking closely. Adding one here also adds it to the universe permanently.")

    # --- Add a ticker --------------------------------------------------
    with st.form("add_to_watchlist_form", clear_on_submit=True):
        new_ticker = st.text_input("Add a ticker (any valid yfinance symbol, e.g. AAPL, SAP.DE, 2330.TW)")
        submitted = st.form_submit_button("Add to watchlist")
        if submitted and new_ticker.strip():
            success = add_ticker_to_watchlist(new_ticker)
            if success:
                st.success(f"Added {new_ticker.strip().upper()} to your watchlist.")
                st.rerun()
            else:
                st.error(f"Couldn't find '{new_ticker.strip().upper()}' on yfinance - check the symbol and try again.")

    watchlist_tickers = get_watchlist_tickers()

    if not watchlist_tickers:
        st.info("Your watchlist is empty. Add a ticker above to get started.")
        return

    # --- Remove a ticker -------------------------------------------
    remove_col1, remove_col2 = st.columns([3, 1])
    with remove_col1:
        ticker_to_remove = st.selectbox("Remove a ticker", watchlist_tickers, key="watchlist_remove_select")
    with remove_col2:
        st.write("")  # vertical spacer so the button lines up with the selectbox
        if st.button("Remove"):
            remove_ticker_from_watchlist(ticker_to_remove)
            st.success(f"Removed {ticker_to_remove} from your watchlist.")
            st.rerun()

    # --- The watchlist table itself ------------------------------------
    df, _ = get_screener_page(tickers_only=watchlist_tickers, page_size=len(watchlist_tickers))

    styled = (
        df.style.map(color_by_rating, subset=["Rating"]).format(
            {
                "Market Cap": format_market_cap,
                "Score": "{:.2f}",
                "RSI (14)": "{:.2f}",
                "% vs SMA50": "{:+.2f}%",
            },
            na_rep="N/A",
        )
    )
    st.dataframe(styled, width="stretch", hide_index=True)
