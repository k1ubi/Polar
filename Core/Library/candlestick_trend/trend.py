"""Trend measurement, at two different granularities used by this module.

1. A continuous, signed trend series (`compute_trend`) — feeds the graduated
   `context_fit` scoring in `signal.py`. Ported from cm45t3r/candlestick's
   `trend.js` (MIT license, https://github.com/cm45t3r/candlestick): a
   one-step % slope of a moving average of closes. Positive = uptrend,
   negative = downtrend, magnitude = strength. `"sma-slope"` is the JS
   library's own default and the one used here.

   (Old Polar's `polar/trend_context.py` used a swing/pivot-based read
   instead, but that one is categorical only — bullish/bearish/None, no
   magnitude — so it can't drive a graduated confidence formula. This
   continuous measure is what `context_fit` actually needs.)

2. A binary trend gate (`prior_trend_binary`) — this is *not* about grading
   confidence, it's about pattern IDENTITY: a hammer-shaped candle after a
   downtrend is literally a different pattern (Hammer, bullish) than the
   same shape after an uptrend (Hanging Man, bearish). Ported as-is from
   TrAIder_Old's `polar/patterns/candlesticks.py::_prior_trend` (EMA(50) vs.
   close), which was calibration-tuned on live trade outcomes.

Both are strictly causal: computed only from bars strictly BEFORE the one
being evaluated, so a pattern's own candles never bias the trend context
it's being scored against.
"""
from __future__ import annotations

import pandas as pd

DEFAULT_PERIOD = 10  # matches cm45t3r/candlestick's trend.js DEFAULT_PERIOD
IDENTITY_TREND_LOOKBACK = 50  # matches TrAIder_Old's candlesticks.py TREND_LOOKBACK


def _sma_slope(close: pd.Series, period: int) -> pd.Series:
    sma = close.rolling(period).mean()
    return sma.pct_change()


def _ema_slope(close: pd.Series, period: int) -> pd.Series:
    ema = close.ewm(span=period, adjust=False).mean()
    return ema.pct_change()


def _pct_change(close: pd.Series, period: int) -> pd.Series:
    return close.pct_change(periods=period)


_METHODS = {
    "sma-slope": _sma_slope,
    "ema-slope": _ema_slope,
    "pct-change": _pct_change,
}


def compute_trend(df: pd.DataFrame, method: str = "sma-slope", period: int = DEFAULT_PERIOD) -> pd.Series:
    """Signed continuous trend series, one value per bar in `df`. NaN where
    there isn't enough history yet. `df.index`-aligned."""
    if method not in _METHODS:
        raise ValueError(f"unknown trend method {method!r}, expected one of {sorted(_METHODS)}")
    return _METHODS[method](df["close"], period)


def classify(value: float | None) -> str | None:
    """Continuous trend value -> coarse label, for readability/logging only
    (the graduated `context_fit` score is what actually drives decisions)."""
    if value is None or pd.isna(value):
        return None
    if value > 0:
        return "bullish"
    if value < 0:
        return "bearish"
    return "sideways"


def prior_trend_binary(df: pd.DataFrame) -> str | None:
    """'up'/'down' from every bar strictly before `df`'s last row. None if
    there isn't enough history. Used only for hammer-family pattern
    identity (see module docstring), not for confidence grading.

    Callers pass a window that still includes the shape candle as its last
    row (e.g. `prior_trend_binary(w.iloc[:-1])` where `w`'s own last row is
    the confirmation candle) — this function drops that last row itself, so
    the shape candle never influences the trend context it's supposedly
    reversing."""
    prior = df.iloc[:-1]
    if len(prior) < IDENTITY_TREND_LOOKBACK:
        return None
    ema = prior["close"].ewm(span=IDENTITY_TREND_LOOKBACK).mean().iloc[-1]
    return "up" if prior["close"].iloc[-1] > ema else "down"
