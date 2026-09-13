"""
app/formatting.py
===================
Small display-only formatting helpers shared by the Screener and
Watchlist tabs (both render the same style of ticker table), so the two
tabs can't drift and look different from each other.
"""

import pandas as pd


def format_market_cap(value) -> str:
    """Render a raw market cap number as e.g. '$2.85T', '$312.40B', '$45.10M' instead of a long raw number."""
    if value is None or pd.isna(value):
        return "N/A"
    value = float(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= threshold:
            return f"${value / threshold:.2f}{suffix}"
    return f"${value:,.2f}"


def color_by_rating(label: str) -> str:
    colors = {
        "Strong Buy": "background-color: #1a7431; color: white",
        "Buy": "background-color: #6fbf73; color: black",
        "Neutral": "background-color: #bdbdbd; color: black",
        "Sell": "background-color: #e59a9a; color: black",
        "Strong Sell": "background-color: #b33939; color: white",
    }
    return colors.get(label, "")
