"""Shared, generic technical indicators — RSI, MACD, ATR.

Not tied to any one Core. Ported faithfully from the prior Polar codebase
(`polar/patterns/atr.py`, `polar/patterns/momentum.py`,
`polar/patterns/confirmation.py::compute_rsi`), which is why the docstrings
below still explain choices made there (Wilder smoothing, why ATR is
opt-in) — those reasons are unchanged by the move.
"""
from __future__ import annotations

import pandas as pd


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI: EMA-smoothed average gain/loss (alpha=1/period), not a
    plain rolling mean — the standard definition, and the same smoothing
    convention `compute_atr` below uses."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series]:
    """Standard EMA-based MACD. Returns (macd_line, signal_line)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's original smoothing (an EMA with alpha=1/period) — the
    standard ATR definition, and volatility-adjusted stop-loss sizing is
    among the most widely validated alternatives to a fixed-percent stop:
    a fixed 2% SL treats a calm large-cap and a volatile small-cap
    identically, which doesn't match how either actually moves."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
