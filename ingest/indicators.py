"""
ingest/indicators.py
=====================
Computes technical indicators from daily OHLCV price history, using only
pandas and numpy (no `pandas_ta` - it does not work with numpy 2.x).

The main entry point is `compute_indicators(df)`. Give it a DataFrame of
daily bars for ONE ticker (columns: open, high, low, close, volume,
indexed by date, oldest row first) and it returns a new DataFrame with one
row per input date and one column per indicator, ready to be upserted into
the `indicators` table (see db/schema.sql for the exact column names).

For accurate results (especially SMA-200 and ADX), pass in at least 250
rows of history. With fewer rows, the long-window indicators will simply
be NaN for the early dates, which Postgres stores as NULL - that's fine,
it just means "not enough history yet" rather than a bug.

A note on "Wilder smoothing": several classic indicators (RSI, ATR, ADX)
were invented by J. Welles Wilder and use a specific kind of smoothed
moving average - not a simple average (SMA) and not the usual exponential
moving average (EMA). It starts with a plain average of the first
`period` values, then each new value is:

    smoothed[t] = (smoothed[t-1] * (period - 1) + raw[t]) / period

`_wilder_smooth()` below implements exactly this, using an explicit loop
so it's easy to follow step by step (this is also what TradingView calls
`ta.rma()`).
"""

import logging

import numpy as np
import pandas as pd
import psycopg2.extras

logger = logging.getLogger(__name__)

MIN_BARS_RECOMMENDED = 250

# Every column compute_indicators() produces, in the same order as the
# `indicators` table's columns (see db/schema.sql). Keeping this list here
# means upsert_indicators() and compute_indicators() can never drift apart.
INDICATOR_COLUMNS = [
    "sma_20", "sma_50", "sma_200", "ema_12", "ema_26",
    "price_vs_sma20_pct", "price_vs_sma50_pct", "price_vs_sma200_pct",
    "ma_cross_state", "adx_14",
    "rsi_14", "macd_line", "macd_signal", "macd_hist",
    "stoch_k", "stoch_d", "roc_10",
    "bb_upper", "bb_mid", "bb_lower", "bb_pct_b", "bb_bandwidth", "atr_14",
    "obv", "volume_vs_avg20",
    "pct_from_52w_high", "pct_from_52w_low",
]


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's smoothing method, used by RSI, ATR and ADX.

    The first output value (at the position where `period` values have
    been seen) is a plain average of those `period` values. Every value
    after that blends the previous smoothed value with the new raw value,
    weighted so the previous value counts (period - 1) times as much as
    the new one - this is what makes it "smoother" than a plain average.
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    smoothed = np.full(n, np.nan)

    # Find the first row that actually has a number (the first row or two
    # of a diff-based series like "gain"/"loss" is NaN because there's no
    # previous day to compare against).
    first_valid = None
    for i in range(n):
        if not np.isnan(values[i]):
            first_valid = i
            break
    if first_valid is None:
        return pd.Series(smoothed, index=series.index)

    seed_end = first_valid + period  # the seed average covers [first_valid, seed_end)
    if seed_end > n:
        # Not enough history to even produce one smoothed value.
        return pd.Series(smoothed, index=series.index)

    smoothed[seed_end - 1] = np.mean(values[first_valid:seed_end])
    for i in range(seed_end, n):
        smoothed[i] = (smoothed[i - 1] * (period - 1) + values[i]) / period

    return pd.Series(smoothed, index=series.index)


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's original version, also what
    TradingView shows by default).

    RSI compares the size of recent up-moves to the size of recent
    down-moves and scales the result to 0-100. Above 70 is traditionally
    considered "overbought", below 30 "oversold".
    """
    delta = close.diff()
    gain = delta.clip(lower=0)      # up-moves only, down-moves become 0
    loss = -delta.clip(upper=0)     # down-moves only, made positive

    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)  # totally flat price -> neutral
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)  # no losses at all -> max RSI
    return rsi


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD (Moving Average Convergence Divergence).

    Returns (macd_line, signal_line, histogram). `adjust=False` on the EMA
    matches how MACD is calculated on every trading platform: each new
    value only looks backward, it never "corrects" using future data.
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute every indicator listed in the project brief for one ticker.

    `df` must have lowercase columns open/high/low/close/volume, indexed
    by date with the OLDEST row first (this matters for rolling/EMA
    calculations - they read history left-to-right).

    Returns a new DataFrame, same index, with one column per indicator.
    """
    if len(df) < MIN_BARS_RECOMMENDED:
        logger.warning(
            "Only %d bars available (recommended minimum is %d) - "
            "long-window indicators like SMA-200 will be NaN for most rows",
            len(df),
            MIN_BARS_RECOMMENDED,
        )

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    out = pd.DataFrame(index=df.index)

    # --- Trend ------------------------------------------------------
    out["sma_20"] = close.rolling(window=20).mean()
    out["sma_50"] = close.rolling(window=50).mean()
    out["sma_200"] = close.rolling(window=200).mean()
    out["ema_12"] = close.ewm(span=12, adjust=False).mean()
    out["ema_26"] = close.ewm(span=26, adjust=False).mean()

    out["price_vs_sma20_pct"] = (close - out["sma_20"]) / out["sma_20"] * 100
    out["price_vs_sma50_pct"] = (close - out["sma_50"]) / out["sma_50"] * 100
    out["price_vs_sma200_pct"] = (close - out["sma_200"]) / out["sma_200"] * 100

    # "Golden cross" / "death cross" here means the CURRENT relationship
    # between the 50-day and 200-day averages (50 above 200 = bullish
    # "golden" state, 50 below 200 = bearish "death" state) - a simpler,
    # ongoing-state version of the classic one-time crossover event.
    cross_state = pd.Series("none", index=df.index, dtype=object)
    cross_state[out["sma_50"] > out["sma_200"]] = "golden_cross"
    cross_state[out["sma_50"] < out["sma_200"]] = "death_cross"
    cross_state[out["sma_50"].isna() | out["sma_200"].isna()] = None
    out["ma_cross_state"] = cross_state

    # ADX(14): trend STRENGTH (not direction). Built from +DI/-DI, which
    # measure how much of the day's range was upward vs downward movement.
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    atr_14 = _wilder_smooth(true_range, 14)
    plus_dm_smooth = _wilder_smooth(plus_dm, 14)
    minus_dm_smooth = _wilder_smooth(minus_dm, 14)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * plus_dm_smooth / atr_14
        minus_di = 100 * minus_dm_smooth / atr_14
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    out["adx_14"] = _wilder_smooth(dx, 14)

    # --- Momentum -----------------------------------------------------
    out["rsi_14"] = compute_rsi(close, 14)
    out["macd_line"], out["macd_signal"], out["macd_hist"] = compute_macd(close, 12, 26, 9)

    lowest_low_14 = low.rolling(window=14).min()
    highest_high_14 = high.rolling(window=14).max()
    with np.errstate(divide="ignore", invalid="ignore"):
        stoch_k = 100 * (close - lowest_low_14) / (highest_high_14 - lowest_low_14)
    out["stoch_k"] = stoch_k
    out["stoch_d"] = stoch_k.rolling(window=3).mean()

    out["roc_10"] = (close - close.shift(10)) / close.shift(10) * 100

    # --- Volatility -----------------------------------------------------
    bb_mid = close.rolling(window=20).mean()
    bb_std = close.rolling(window=20).std(ddof=0)
    out["bb_mid"] = bb_mid
    out["bb_upper"] = bb_mid + 2 * bb_std
    out["bb_lower"] = bb_mid - 2 * bb_std
    with np.errstate(divide="ignore", invalid="ignore"):
        out["bb_pct_b"] = (close - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"])
        out["bb_bandwidth"] = (out["bb_upper"] - out["bb_lower"]) / bb_mid

    out["atr_14"] = atr_14

    # --- Volume -----------------------------------------------------
    # On-Balance Volume: running total that adds today's volume on an up
    # day, subtracts it on a down day, and leaves it unchanged on a flat day.
    price_direction = np.sign(close.diff().fillna(0))
    out["obv"] = (price_direction * volume).cumsum()

    volume_avg_20 = volume.rolling(window=20).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out["volume_vs_avg20"] = volume / volume_avg_20

    # --- Position within the 52-week range -----------------------------
    # 252 trading days is the standard stand-in for "52 weeks".
    rolling_high_52w = close.rolling(window=252, min_periods=1).max()
    rolling_low_52w = close.rolling(window=252, min_periods=1).min()
    out["pct_from_52w_high"] = (close - rolling_high_52w) / rolling_high_52w * 100
    out["pct_from_52w_low"] = (close - rolling_low_52w) / rolling_low_52w * 100

    return out


def upsert_indicators(conn, ticker: str, indicators_df: pd.DataFrame) -> int:
    """
    Insert (or update) rows from compute_indicators()'s output into the
    `indicators` table, one row per date.

    Rows where every indicator is still NaN (the first few dates of a
    short history, before any rolling window has enough data) are skipped
    - there is nothing useful to store yet.

    `conn` is a psycopg2 connection (see db/connection.py). This function
    does not commit or close the connection - the caller controls the
    transaction.
    """
    rows = []
    for row_date, row in indicators_df.iterrows():
        values = [row.get(col) for col in INDICATOR_COLUMNS]
        if all(v is None or (isinstance(v, float) and np.isnan(v)) for v in values):
            continue

        def clean(value):
            if value is None:
                return None
            if isinstance(value, float) and np.isnan(value):
                return None
            if isinstance(value, (int, float, np.integer, np.floating)):
                # psycopg2 doesn't know how to write numpy number types
                # (e.g. numpy.float64) into a query - it silently falls
                # back to str(value), which produces broken SQL. Casting
                # to a plain Python float/int avoids that trap.
                return float(value)
            return value  # e.g. the ma_cross_state string

        cleaned = [clean(v) for v in values]
        rows.append((ticker, row_date.date() if hasattr(row_date, "date") else row_date, *cleaned))

    if not rows:
        return 0

    columns_sql = ", ".join(INDICATOR_COLUMNS)
    update_sql = ", ".join(f"{col} = EXCLUDED.{col}" for col in INDICATOR_COLUMNS)

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"""
            INSERT INTO indicators (ticker, date, {columns_sql})
            VALUES %s
            ON CONFLICT (ticker, date) DO UPDATE SET
                {update_sql},
                computed_at = NOW()
            """,
            rows,
        )
    return len(rows)
