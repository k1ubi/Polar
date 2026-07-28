"""Adaptive candle-size thresholds, ported from TA-Lib's candle-averaging
methodology (via pandas_ta_classic's `_cdl_math.py`, MIT licensed,
https://github.com/xgboosted/pandas-ta-classic, itself a translation of
TA-Lib's ta_global.c candle macros).

TA-Lib's CDL* patterns don't compare a candle's body/shadow size against
that SAME candle's own high-low range (the simpler style the rest of
patterns.py uses, ported from cm45t3r/candlestick) -- they compare it
against a TRAILING AVERAGE of that measure over the preceding N bars. N
depends on the setting: 10 for the body settings, 5 for near/far/equal,
0 for the two shadow-long settings, which per TA-Lib's own convention
means "no separate average -- compare the bar's shadow against its OWN
body, self-referential" rather than an actual trailing window. This
adaptive-average behavior is the whole reason pandas_ta_classic was chosen
over hand-picking fixed ratios for the ~46 patterns ported alongside this
module -- it means the same absolute candle size registers differently in
a volatile regime vs. a quiet one, matching TA-Lib's real output rather
than an approximation of it.

Causal: every trailing average ends at the bar BEFORE the one being
judged (`.shift(1)`), so a candle can never influence the threshold it's
being measured against -- same discipline as `trend.py`'s
`prior_trend_binary` and `geometry.py`'s design throughout this package.

Not vectorized/JIT'd (pandas_ta_classic uses Numba): `scan()`'s per-position
window-growth loop is already O(n^2) for the existing hammer-family
detectors that recompute a trailing EMA per call, so this follows the same
cost profile rather than introducing a second one. Worth revisiting if
`scan()` ever needs to run over long histories at speed.
"""
from __future__ import annotations

from enum import Enum

import pandas as pd


class RangeType(Enum):
    REAL_BODY = "real_body"
    HIGH_LOW = "high_low"
    SHADOWS = "shadows"


class Setting(Enum):
    BODY_LONG = "body_long"
    BODY_VERY_LONG = "body_very_long"
    BODY_SHORT = "body_short"
    BODY_DOJI = "body_doji"
    SHADOW_LONG = "shadow_long"
    SHADOW_VERY_LONG = "shadow_very_long"
    SHADOW_SHORT = "shadow_short"
    SHADOW_VERY_SHORT = "shadow_very_short"
    NEAR = "near"
    FAR = "far"
    EQUAL = "equal"


# (range_type, avg_period, factor) -- verbatim from TA-Lib's TA_CandleDefaultSettings
_DEFAULTS: dict[Setting, tuple[RangeType, int, float]] = {
    Setting.BODY_LONG: (RangeType.REAL_BODY, 10, 1.0),
    Setting.BODY_VERY_LONG: (RangeType.REAL_BODY, 10, 3.0),
    Setting.BODY_SHORT: (RangeType.REAL_BODY, 10, 1.0),
    Setting.BODY_DOJI: (RangeType.HIGH_LOW, 10, 0.1),
    Setting.SHADOW_LONG: (RangeType.REAL_BODY, 0, 1.0),
    Setting.SHADOW_VERY_LONG: (RangeType.REAL_BODY, 0, 2.0),
    Setting.SHADOW_SHORT: (RangeType.SHADOWS, 10, 1.0),
    Setting.SHADOW_VERY_SHORT: (RangeType.HIGH_LOW, 10, 0.1),
    Setting.NEAR: (RangeType.HIGH_LOW, 5, 0.2),
    Setting.FAR: (RangeType.HIGH_LOW, 5, 0.6),
    Setting.EQUAL: (RangeType.HIGH_LOW, 5, 0.05),
}


def _range_series(df: pd.DataFrame, range_type: RangeType) -> pd.Series:
    body_high = df[["open", "close"]].max(axis=1)
    body_low = df[["open", "close"]].min(axis=1)
    if range_type is RangeType.REAL_BODY:
        return (df["close"] - df["open"]).abs()
    if range_type is RangeType.HIGH_LOW:
        return df["high"] - df["low"]
    upper = df["high"] - body_high
    lower = body_low - df["low"]
    return upper + lower


_CACHE: dict[tuple[int, Setting], pd.Series] = {}


def threshold(df: pd.DataFrame, setting: Setting) -> pd.Series:
    """Series aligned with df.index: the adaptive comparison value for
    `setting` at every bar -- a trailing average of bars strictly before
    it (period > 0), or the bar's own range measure (period == 0,
    self-referential, see module docstring).

    Cached per (id(df), setting): this is causal (shift(1)/rolling never
    look ahead), so computing it once over the FULL series and indexing
    into it gives identical values to recomputing it over any growing
    prefix window ending at the same position -- patterns_talib.py's
    scan() relies on this to avoid recomputing an O(n)-per-call rolling
    average at every one of the O(n) positions it visits (was the
    dominant cost: ~80s for 300 bars before this cache). Keyed on object
    identity, not content -- safe within one scan() call where the same
    df object is reused throughout; a df mutated in place between calls
    would return a stale cached value, but nothing in this package ever
    mutates an OHLCV frame after loading it."""
    key = (id(df), setting)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    range_type, period, factor = _DEFAULTS[setting]
    ranges = _range_series(df, range_type)
    divisor = 2.0 if range_type is RangeType.SHADOWS else 1.0
    if period == 0:
        result = ranges * (factor / divisor)
    else:
        avg = ranges.shift(1).rolling(period).mean()
        result = avg * (factor / divisor)
    _CACHE[key] = result
    return result


def body_len_series(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def upper_shadow_series(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df[["open", "close"]].max(axis=1)


def lower_shadow_series(df: pd.DataFrame) -> pd.Series:
    return df[["open", "close"]].min(axis=1) - df["low"]
