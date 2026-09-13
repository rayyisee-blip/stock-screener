"""
app/screener_tab.py
=====================
Tab 1: Screener - a searchable/filterable table of the whole universe.
"""

import streamlit as st

from app.data import get_filter_options, get_screener_page
from app.formatting import color_by_rating, format_market_cap

PAGE_SIZE = 50


def render():
    st.subheader("Screener")
    st.caption("Every ticker in the universe, with its latest composite rating.")

    filter_options = get_filter_options()

    # --- Filters ---------------------------------------------------
    col1, col2, col3 = st.columns(3)
    with col1:
        market = st.selectbox("Market", ["All"] + filter_options["markets"], key="screener_market")
    with col2:
        rating_label = st.selectbox("Rating", ["All"] + filter_options["labels"], key="screener_rating")
    with col3:
        sector = st.selectbox("Sector", ["All"] + filter_options["sectors"], key="screener_sector")

    col4, col5, col6 = st.columns(3)
    with col4:
        rsi_range = st.slider("RSI (14) range", min_value=0, max_value=100, value=(0, 100), key="screener_rsi")
    with col5:
        min_market_cap_billions = st.number_input(
            "Min market cap ($B)", min_value=0.0, value=0.0, step=1.0, key="screener_min_mcap"
        )
    with col6:
        sort_choice = st.selectbox(
            "Sort by",
            ["Composite score (high to low)", "Composite score (low to high)", "Ticker (A-Z)", "RSI (14)"],
            key="screener_sort",
        )

    sort_map = {
        "Composite score (high to low)": ("composite_score", True),
        "Composite score (low to high)": ("composite_score", False),
        "Ticker (A-Z)": ("ticker", False),
        "RSI (14)": ("rsi_14", True),
    }
    sort_by, sort_desc = sort_map[sort_choice]

    # --- Pagination state --------------------------------------------
    if "screener_page" not in st.session_state:
        st.session_state.screener_page = 1

    df, total_count = get_screener_page(
        market=None if market == "All" else market,
        rating_label=None if rating_label == "All" else rating_label,
        sector=None if sector == "All" else sector,
        # Only actually filter on RSI if the user moved the slider away
        # from its full 0-100 default - otherwise "i.rsi_14 >= 0" would
        # silently hide every ticker that has no indicators row yet
        # (SQL's "NULL >= 0" is neither true nor false, so those rows
        # would be excluded even though the user didn't ask to filter).
        rsi_min=rsi_range[0] if rsi_range[0] > 0 else None,
        rsi_max=rsi_range[1] if rsi_range[1] < 100 else None,
        market_cap_min=min_market_cap_billions * 1_000_000_000 if min_market_cap_billions > 0 else None,
        sort_by=sort_by,
        sort_desc=sort_desc,
        page=st.session_state.screener_page,
        page_size=PAGE_SIZE,
    )

    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    st.session_state.screener_page = min(st.session_state.screener_page, total_pages)

    st.write(f"**{total_count}** tickers match these filters.")

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

    # --- Pagination controls -----------------------------------------
    nav_col1, nav_col2, nav_col3 = st.columns([1, 2, 1])
    with nav_col1:
        if st.button("Previous page", disabled=st.session_state.screener_page <= 1):
            st.session_state.screener_page -= 1
            st.rerun()
    with nav_col2:
        st.write(f"Page {st.session_state.screener_page} of {total_pages}")
    with nav_col3:
        if st.button("Next page", disabled=st.session_state.screener_page >= total_pages):
            st.session_state.screener_page += 1
            st.rerun()
