"""OHLC geometry helpers shared by every single/multi-candle pattern.

Ported from cm45t3r/candlestick's utils.js (MIT license,
https://github.com/cm45t3r/candlestick) via TrAIder_Old's
polar/patterns/candle_geometry.py — same definitions, rewritten against
pandas Series instead of a JS candle object.
"""
from __future__ import annotations

import pandas as pd


def body_len(c: pd.Series) -> float:
    return abs(c["open"] - c["close"])


def wick_len(c: pd.Series) -> float:
    return c["high"] - max(c["open"], c["close"])


def tail_len(c: pd.Series) -> float:
    return min(c["open"], c["close"]) - c["low"]


def body_ends(c: pd.Series) -> tuple[float, float]:
    """Returns (bottom, top) of the candle body."""
    return (c["open"], c["close"]) if c["open"] <= c["close"] else (c["close"], c["open"])


def candle_range(c: pd.Series) -> float:
    return c["high"] - c["low"]


def is_bullish(c: pd.Series) -> bool:
    return c["open"] < c["close"]


def is_bearish(c: pd.Series) -> bool:
    return c["open"] > c["close"]


def has_gap_up(prev: pd.Series, cur: pd.Series) -> bool:
    return body_ends(prev)[1] < body_ends(cur)[0]


def has_gap_down(prev: pd.Series, cur: pd.Series) -> bool:
    return body_ends(prev)[0] > body_ends(cur)[1]


def is_engulfed(prev: pd.Series, cur: pd.Series) -> bool:
    """True if `prev`'s body is fully contained within `cur`'s body."""
    prev_bottom, prev_top = body_ends(prev)
    cur_bottom, cur_top = body_ends(cur)
    return prev_top <= cur_top and prev_bottom >= cur_bottom
