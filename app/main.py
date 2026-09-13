"""
app/main.py
=============
Entry point for the Streamlit app. Run it with:

    streamlit run app/main.py

This file only handles page setup and wiring the four tabs together - all
the actual tab content lives in screener_tab.py / watchlist_tab.py /
performance_tab.py / detail_tab.py, and every database read/write goes
through data.py. See those files' docstrings for details.
"""

import sys
from pathlib import Path

# Streamlit runs this file directly and only puts ITS OWN folder (app/) on
# sys.path, not the project root - so "from app.data import ..." and
# "from db.connection import ..." would fail without this. Adding the
# parent folder (the project root) to sys.path fixes that, and must
# happen before any of our own project imports below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from app import detail_tab, performance_tab, screener_tab, watchlist_tab
from app.data import get_last_updated_utc, to_singapore_time

st.set_page_config(page_title="Stock Screener", layout="wide")

st.title("Stock Screener")

# "Last updated" timestamp, required on every tab - shown once here at the
# top since it applies to the whole page, in Singapore time as specified
# in the project brief (everything is stored in UTC, only displayed in SGT).
last_updated_utc = get_last_updated_utc()
if last_updated_utc:
    last_updated_sgt = to_singapore_time(last_updated_utc)
    st.caption(f"Data last updated: {last_updated_sgt.strftime('%Y-%m-%d %H:%M:%S')} SGT")
else:
    st.caption("Data last updated: never - run the ingest pipeline first (see README.md).")

screener, watchlist, performance, detail = st.tabs(["Screener", "Watchlist", "Performance", "Detail"])

with screener:
    screener_tab.render()
with watchlist:
    watchlist_tab.render()
with performance:
    performance_tab.render()
with detail:
    detail_tab.render()

st.divider()
st.caption("Educational tool. Not financial advice. Data may be delayed or incorrect.")
