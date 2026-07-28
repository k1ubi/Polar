"""The candle "spread" vocabulary — VPA's other measurement axis.

Coulling's term for a candle's height is SPREAD (high-to-low range), graded
narrow / average / wide by eye against recent bars, exactly as volume is.
Spread is never read alone: the entire method is the INTERACTION of spread
and volume on the same bar (see `signals.py`).

Two distinct measurements live here, and conflating them is a real error:

  - SPREAD  = high - low. The full extent of the bar's move. This is what
              the book means by "spread" and what pairs with volume.
  - BODY    = |close - open|. Where the bar actually *settled*. A bar can
              have a wide spread and a tiny body (a long-wicked rejection),
              which is one of VPA's most informative shapes precisely
              BECAUSE the two disagree.

Spread is normalized by ATR rather than percentile-ranked (the way volume
is) because spread has a natural volatility denominator and ATR is the
standard one — "this bar is 1.8 ATRs tall" is directly meaningful, whereas
volume has no comparable natural unit and genuinely needs a rank.

Causality: `atr` uses only completed prior bars via a shifted rolling mean,
so a bar is never normalized by a window containing itself.
"""
from __future__ import annotations

from enum import IntEnum

import pandas as pd

DEFAULT_ATR_PERIOD = 14


class Spread(IntEnum):
    NARROW = 0
    AVERAGE = 1
    WIDE = 2


# ATR multiples. Reasoned defaults, not from the book (which grades by eye).
CUTOFF_WIDE = 1.3
CUTOFF_NARROW = 0.6


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> pd.Series:
    """Trailing ATR measured on bars STRICTLY BEFORE each bar (`shift(1)`),
    so the bar being graded never inflates its own denominator."""
    return true_range(df).shift(1).rolling(period).mean()


def spread_series(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df["low"]


def body_series(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def spread_atr_series(df: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> pd.Series:
    """Spread expressed in ATRs. NaN until ATR has a full window."""
    a = atr(df, period=period)
    return spread_series(df) / a.where(a > 0)


def grade_from_atr_multiple(multiple: float) -> Spread:
    if multiple >= CUTOFF_WIDE:
        return Spread.WIDE
    if multiple <= CUTOFF_NARROW:
        return Spread.NARROW
    return Spread.AVERAGE


def grade_series(df: pd.DataFrame, period: int = DEFAULT_ATR_PERIOD) -> pd.Series:
    mult = spread_atr_series(df, period=period)
    return mult.map(lambda m: grade_from_atr_multiple(m) if pd.notna(m) else None)


# --- where the bar closed within its own range ---------------------------
#
# Coulling reads close position constantly ("closing on the high", "closing
# off the low", "closing in the middle"). It is the single most direct read
# of who won the bar, and it is what separates a wide-spread up bar that
# means strength from one that means a failed push.


def close_position(df: pd.DataFrame) -> pd.Series:
    """0.0 = closed on the low, 1.0 = closed on the high, 0.5 = mid-range.
    NaN on a zero-range bar (no position to speak of)."""
    rng = spread_series(df)
    return ((df["close"] - df["low"]) / rng.where(rng > 0))


CLOSE_HIGH = 0.7  # at/above -> "closing on the high"
CLOSE_LOW = 0.3  # at/below -> "closing on the low"
CLOSE_MID_LOW, CLOSE_MID_HIGH = 0.4, 0.6  # inside -> "closing mid-range"


def upper_wick(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df[["open", "close"]].max(axis=1)


def lower_wick(df: pd.DataFrame) -> pd.Series:
    return df[["open", "close"]].min(axis=1) - df["low"]
