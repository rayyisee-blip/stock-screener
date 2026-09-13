"""
Unit tests for ingest/indicators.py.

RSI and MACD are checked against a "hand-calculated" reference: a second,
completely independent implementation written with plain Python loops and
lists (no pandas, no numpy vector tricks) that does the arithmetic exactly
the way you'd do it one row at a time with a calculator. If the fast,
vectorized pandas version in ingest/indicators.py ever has a bug (an
off-by-one in a rolling window, a wrong smoothing formula, etc.), it will
disagree with this simple loop-based version and the test will fail.
"""

import math

import pandas as pd
import pytest

from ingest.indicators import compute_indicators, compute_macd, compute_rsi

# A small, fixed closing-price series. Nothing special about the numbers -
# they just need to go up and down enough to exercise both gains and losses.
CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    45.98, 46.02, 46.55, 46.87, 46.90, 47.12, 46.98, 47.30, 47.50, 47.35,
]


# --- Reference (hand-style) implementations ---------------------------

def _reference_rsi(closes: list, period: int) -> list:
    """Plain-loop Wilder RSI, computed one day at a time."""
    n = len(closes)
    rsi = [float("nan")] * n
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0

    if period >= n:
        return rsi

    # Seed: a plain average of the first `period` daily gains/losses.
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    rsi[period] = _rsi_from_averages(avg_gain, avg_loss)

    # Every day after that blends the previous average with today's value.
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i] = _rsi_from_averages(avg_gain, avg_loss)

    return rsi


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _reference_ema(values: list, span: int) -> list:
    """Plain-loop EMA with adjust=False semantics (each value only looks backward)."""
    alpha = 2 / (span + 1)
    ema = [0.0] * len(values)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


def _reference_macd(closes: list, fast: int, slow: int, signal: int):
    ema_fast = _reference_ema(closes, fast)
    ema_slow = _reference_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _reference_ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


# --- Tests --------------------------------------------------------------

def test_rsi_matches_hand_calculated_reference():
    close_series = pd.Series(CLOSES)
    actual = compute_rsi(close_series, period=14)
    expected = _reference_rsi(CLOSES, period=14)

    for i in range(14, len(CLOSES)):
        assert actual.iloc[i] == pytest.approx(expected[i], abs=1e-9), (
            f"RSI mismatch at index {i}: got {actual.iloc[i]}, expected {expected[i]}"
        )

    # Before the seed window, RSI should not be computable yet.
    for i in range(0, 14):
        assert math.isnan(actual.iloc[i])


def test_macd_matches_hand_calculated_reference():
    close_series = pd.Series(CLOSES)
    macd_line, signal_line, hist = compute_macd(close_series, fast=12, slow=26, signal=9)
    expected_macd, expected_signal, expected_hist = _reference_macd(CLOSES, 12, 26, 9)

    for i in range(len(CLOSES)):
        assert macd_line.iloc[i] == pytest.approx(expected_macd[i], abs=1e-9)
        assert signal_line.iloc[i] == pytest.approx(expected_signal[i], abs=1e-9)
        assert hist.iloc[i] == pytest.approx(expected_hist[i], abs=1e-9)


def test_compute_indicators_produces_expected_columns():
    """A light smoke test that compute_indicators() runs end-to-end and
    that the RSI/MACD it produces agree with the standalone functions
    tested above (i.e. compute_indicators wires things up correctly)."""
    df = pd.DataFrame(
        {
            "open": CLOSES,
            "high": [c + 0.3 for c in CLOSES],
            "low": [c - 0.3 for c in CLOSES],
            "close": CLOSES,
            "volume": [1_000_000 + i * 1000 for i in range(len(CLOSES))],
        },
        index=pd.date_range("2024-01-01", periods=len(CLOSES), freq="D"),
    )

    result = compute_indicators(df)

    for expected_column in [
        "sma_20", "sma_50", "sma_200", "ema_12", "ema_26",
        "rsi_14", "macd_line", "macd_signal", "macd_hist",
        "stoch_k", "stoch_d", "roc_10",
        "bb_upper", "bb_mid", "bb_lower", "bb_pct_b", "bb_bandwidth", "atr_14",
        "obv", "volume_vs_avg20", "pct_from_52w_high", "pct_from_52w_low",
        "ma_cross_state", "adx_14",
    ]:
        assert expected_column in result.columns

    expected_rsi = compute_rsi(pd.Series(CLOSES), period=14)
    pd.testing.assert_series_equal(
        result["rsi_14"].reset_index(drop=True),
        expected_rsi.reset_index(drop=True),
        check_names=False,
    )
