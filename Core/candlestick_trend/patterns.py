"""Classic single/multi-candle pattern detectors.

Detection rules ported from cm45t3r/candlestick (MIT license,
https://github.com/cm45t3r/candlestick) via TrAIder_Old's
polar/patterns/candlesticks.py — same thresholds and logic, adapted to a
plain function + `scan()` contract instead of the old Pattern/PatternResult
plugin registry (this module is self-contained, no platform dependency).

Each detector function answers "did this pattern just complete on the LAST
bar of the window passed to it?" — not "has this ever happened in history."
`scan()` walks a growing window across the full series to answer that
question at every bar, for backtesting/analysis.

Direction and base confidence per pattern name are NOT decided here — see
metadata.py. This module only decides pattern IDENTITY (which name, if any,
matches the latest bar(s)) and, for the hammer family, uses a binary prior-
trend gate for that identity decision (a hammer shape after a downtrend is a
different pattern than the same shape after an uptrend — see trend.py).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from Core.candlestick_trend.geometry import (
    body_ends, body_len, candle_range, has_gap_down, has_gap_up,
    is_bearish, is_bullish, is_engulfed, tail_len, wick_len,
)
from Core.candlestick_trend.trend import prior_trend_binary


@dataclass(frozen=True)
class PatternHit:
    pattern: str
    index: int  # position (iloc) of the LAST candle involved in the match
    start_index: int  # position of the FIRST candle involved in the match
    detail: str


def _next_bar_confirms(shape: pd.Series, confirm: pd.Series, direction: str) -> bool:
    """Nison: a single-candle reversal shape (hammer/hanging man/inverted
    hammer/shooting star) is a SETUP, not a signal on its own — confirmation
    requires the FOLLOWING candle to close beyond the shape candle's body in
    the reversal direction."""
    body_bottom, body_top = body_ends(shape)
    return confirm["close"] > body_top if direction == "bullish" else confirm["close"] < body_bottom


def _is_hammer_shape(c: pd.Series) -> bool:
    rng = candle_range(c)
    bl, wl, tl = body_len(c), wick_len(c), tail_len(c)
    return rng > 0 and tl >= 2 * bl and wl <= bl and max(c["open"], c["close"]) > c["low"] + rng * 2 / 3


def _is_inverted_hammer_shape(c: pd.Series) -> bool:
    rng = candle_range(c)
    bl, wl, tl = body_len(c), wick_len(c), tail_len(c)
    eps = 1e-8
    return rng > 0 and wl >= 2 * bl and tl <= bl + eps and min(c["open"], c["close"]) <= c["high"] - rng * 2 / 3 + eps


def _is_marubozu(c: pd.Series) -> bool:
    rng = candle_range(c)
    if rng == 0:
        return False
    bl = body_len(c)
    if bl / rng < 0.7:
        return False
    max_shadow = bl * 0.1
    return wick_len(c) <= max_shadow and tail_len(c) <= max_shadow


def _is_spinning_top(c: pd.Series) -> bool:
    rng = candle_range(c)
    if rng == 0:
        return False
    if body_len(c) / rng >= 0.3:
        return False
    return wick_len(c) / rng >= 0.2 and tail_len(c) / rng >= 0.2


def _tweezers_top(f: pd.Series, s: pd.Series) -> bool:
    avg_range = (candle_range(f) + candle_range(s)) / 2
    if avg_range == 0:
        return False
    if abs(f["high"] - s["high"]) > avg_range * 0.01:
        return False
    if not (is_bullish(f) and is_bearish(s)):
        return False
    return body_len(f) / candle_range(f) >= 0.4 and body_len(s) / candle_range(s) >= 0.4


def _tweezers_bottom(f: pd.Series, s: pd.Series) -> bool:
    avg_range = (candle_range(f) + candle_range(s)) / 2
    if avg_range == 0:
        return False
    if abs(f["low"] - s["low"]) > avg_range * 0.01:
        return False
    if not (is_bearish(f) and is_bullish(s)):
        return False
    return body_len(f) / candle_range(f) >= 0.4 and body_len(s) / candle_range(s) >= 0.4


# Each detector takes a window `pd.DataFrame` (only the tail is inspected)
# and returns a detail string on match, else None. `min_bars` is how many
# rows the window must have before this detector can possibly fire.

def _detect_doji(w: pd.DataFrame) -> str | None:
    c = w.iloc[-1]
    rng = candle_range(c)
    if rng > 0 and body_len(c) / rng < 0.1:
        return "small body vs range — indecision"
    return None


def _detect_hammer(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    shape, confirm = w.iloc[-2], w.iloc[-1]
    if not _is_hammer_shape(shape) or prior_trend_binary(w.iloc[:-1]) != "down":
        return None
    if not _next_bar_confirms(shape, confirm, "bullish"):
        return None
    return "small body near top, long lower shadow, after a downtrend, confirmed by the next candle"


def _detect_hanging_man(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    shape, confirm = w.iloc[-2], w.iloc[-1]
    if not _is_hammer_shape(shape) or prior_trend_binary(w.iloc[:-1]) != "up":
        return None
    if not _next_bar_confirms(shape, confirm, "bearish"):
        return None
    return "hammer shape after an uptrend, confirmed by a close below the body on the next candle"


def _detect_inverted_hammer(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    shape, confirm = w.iloc[-2], w.iloc[-1]
    if not _is_inverted_hammer_shape(shape) or prior_trend_binary(w.iloc[:-1]) != "down":
        return None
    if not _next_bar_confirms(shape, confirm, "bullish"):
        return None
    return "small body near bottom, long upper shadow, after a downtrend, confirmed by the next candle"


def _detect_shooting_star(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    shape, confirm = w.iloc[-2], w.iloc[-1]
    if not _is_inverted_hammer_shape(shape) or prior_trend_binary(w.iloc[:-1]) != "up":
        return None
    if not _next_bar_confirms(shape, confirm, "bearish"):
        return None
    return "inverted-hammer shape after an uptrend, confirmed by a close below the body on the next candle"


def _detect_bullish_marubozu(w: pd.DataFrame) -> str | None:
    c = w.iloc[-1]
    if is_bullish(c) and _is_marubozu(c):
        return "long body, negligible shadows — strong buying pressure"
    return None


def _detect_bearish_marubozu(w: pd.DataFrame) -> str | None:
    c = w.iloc[-1]
    if is_bearish(c) and _is_marubozu(c):
        return "long body, negligible shadows — strong selling pressure"
    return None


def _detect_bullish_spinning_top(w: pd.DataFrame) -> str | None:
    c = w.iloc[-1]
    if is_bullish(c) and _is_spinning_top(c):
        return "small body, long both shadows — indecision"
    return None


def _detect_bearish_spinning_top(w: pd.DataFrame) -> str | None:
    c = w.iloc[-1]
    if is_bearish(c) and _is_spinning_top(c):
        return "small body, long both shadows — indecision"
    return None


def _detect_bullish_engulfing(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    p, c = w.iloc[-2], w.iloc[-1]
    if is_bearish(p) and is_bullish(c) and is_engulfed(p, c) and prior_trend_binary(w.iloc[:-1]) == "down":
        return "bullish body engulfs prior bearish body, after a downtrend"
    return None


def _detect_bearish_engulfing(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    p, c = w.iloc[-2], w.iloc[-1]
    if is_bullish(p) and is_bearish(c) and is_engulfed(p, c) and prior_trend_binary(w.iloc[:-1]) == "up":
        return "bearish body engulfs prior bullish body, after an uptrend"
    return None


def _detect_bullish_harami(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    p, c = w.iloc[-2], w.iloc[-1]
    if is_bearish(p) and is_bullish(c) and is_engulfed(c, p) and prior_trend_binary(w.iloc[:-1]) == "down":
        return "small bullish body inside prior bearish body, after a downtrend"
    return None


def _detect_bearish_harami(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    p, c = w.iloc[-2], w.iloc[-1]
    if is_bullish(p) and is_bearish(c) and is_engulfed(c, p) and prior_trend_binary(w.iloc[:-1]) == "up":
        return "small bearish body inside prior bullish body, after an uptrend"
    return None


def _detect_piercing_line(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    c1, c2 = w.iloc[-2], w.iloc[-1]
    r1 = candle_range(c1)
    if not (is_bearish(c1) and r1 > 0 and body_len(c1) >= r1 * 0.5):
        return None
    r2 = candle_range(c2)
    if not (is_bullish(c2) and r2 > 0 and body_len(c2) >= r2 * 0.5):
        return None
    if c2["open"] >= c1["low"]:
        return None
    c1_bottom, c1_top = body_ends(c1)
    mid = (c1_top + c1_bottom) / 2
    if not (c2["close"] > mid and c2["close"] < c1_top):
        return None
    return "bullish candle gaps down then closes above prior body midpoint"


def _detect_dark_cloud_cover(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    c1, c2 = w.iloc[-2], w.iloc[-1]
    r1 = candle_range(c1)
    if not (is_bullish(c1) and r1 > 0 and body_len(c1) >= r1 * 0.5):
        return None
    r2 = candle_range(c2)
    if not (is_bearish(c2) and r2 > 0 and body_len(c2) >= r2 * 0.5):
        return None
    if c2["open"] <= c1["high"]:
        return None
    c1_bottom, c1_top = body_ends(c1)
    mid = (c1_top + c1_bottom) / 2
    if not (c2["close"] < mid and c2["close"] > c1_bottom):
        return None
    return "bearish candle gaps up then closes below prior body midpoint"


def _detect_tweezers_top(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    f, s = w.iloc[-2], w.iloc[-1]
    if _tweezers_top(f, s):
        return "matching highs, bullish then bearish — potential top"
    return None


def _detect_tweezers_bottom(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    f, s = w.iloc[-2], w.iloc[-1]
    if _tweezers_bottom(f, s):
        return "matching lows, bearish then bullish — potential bottom"
    return None


def _detect_bullish_kicker(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    p, c = w.iloc[-2], w.iloc[-1]
    hammer_shape = candle_range(c) > 0 and tail_len(c) >= 2 * body_len(c) and wick_len(c) <= body_len(c)
    inv_hammer_shape = candle_range(c) > 0 and wick_len(c) >= 2 * body_len(c) and tail_len(c) <= body_len(c)
    if is_bearish(p) and is_bullish(c) and has_gap_up(p, c) and not (hammer_shape or inv_hammer_shape):
        return "bearish candle followed by gap-up bullish candle"
    return None


def _detect_bearish_kicker(w: pd.DataFrame) -> str | None:
    if len(w) < 2:
        return None
    p, c = w.iloc[-2], w.iloc[-1]
    hammer_shape = candle_range(c) > 0 and tail_len(c) >= 2 * body_len(c) and wick_len(c) <= body_len(c)
    inv_hammer_shape = candle_range(c) > 0 and wick_len(c) >= 2 * body_len(c) and tail_len(c) <= body_len(c)
    if is_bullish(p) and is_bearish(c) and has_gap_down(p, c) and not (hammer_shape or inv_hammer_shape):
        return "bullish candle followed by gap-down bearish candle"
    return None


def _detect_morning_star(w: pd.DataFrame) -> str | None:
    if len(w) < 3:
        return None
    c1, c2, c3 = w.iloc[-3], w.iloc[-2], w.iloc[-1]
    r1 = candle_range(c1)
    if not (is_bearish(c1) and r1 > 0 and body_len(c1) >= r1 * 0.6):
        return None
    r2 = candle_range(c2)
    if r2 == 0 or body_len(c2) > r2 * 0.3:
        return None
    c1_bottom, _ = body_ends(c1)
    if body_ends(c2)[1] >= c1_bottom:
        return None
    r3 = candle_range(c3)
    if not (is_bullish(c3) and r3 > 0 and body_len(c3) >= r3 * 0.6):
        return None
    c1_bottom, c1_top = body_ends(c1)
    if c3["close"] < (c1_top + c1_bottom) / 2:
        return None
    return "bearish candle, small-body star gapping down, bullish candle closing into first body"


def _detect_evening_star(w: pd.DataFrame) -> str | None:
    if len(w) < 3:
        return None
    c1, c2, c3 = w.iloc[-3], w.iloc[-2], w.iloc[-1]
    r1 = candle_range(c1)
    if not (is_bullish(c1) and r1 > 0 and body_len(c1) >= r1 * 0.6):
        return None
    r2 = candle_range(c2)
    if r2 == 0 or body_len(c2) > r2 * 0.3:
        return None
    if body_ends(c2)[0] <= body_ends(c1)[1]:
        return None
    r3 = candle_range(c3)
    if not (is_bearish(c3) and r3 > 0 and body_len(c3) >= r3 * 0.6):
        return None
    c1_bottom, c1_top = body_ends(c1)
    if c3["close"] > (c1_top + c1_bottom) / 2:
        return None
    return "bullish candle, small-body star gapping up, bearish candle closing into first body"


def _detect_three_white_soldiers(w: pd.DataFrame) -> str | None:
    if len(w) < 3:
        return None
    c1, c2, c3 = w.iloc[-3], w.iloc[-2], w.iloc[-1]
    if not (is_bullish(c1) and is_bullish(c2) and is_bullish(c3)):
        return None
    for c in (c1, c2, c3):
        r = candle_range(c)
        if r == 0 or body_len(c) < r * 0.6:
            return None
    if not (c2["close"] > c1["close"] and c3["close"] > c2["close"]):
        return None
    if not (c1["open"] <= c2["open"] <= c1["close"]):
        return None
    if not (c2["open"] <= c3["open"] <= c2["close"]):
        return None
    for c in (c1, c2, c3):
        if wick_len(c) > body_len(c) * 0.3:
            return None
    return "three consecutive strong bullish candles, each opening within the prior body"


def _detect_three_black_crows(w: pd.DataFrame) -> str | None:
    if len(w) < 3:
        return None
    c1, c2, c3 = w.iloc[-3], w.iloc[-2], w.iloc[-1]
    if not (is_bearish(c1) and is_bearish(c2) and is_bearish(c3)):
        return None
    for c in (c1, c2, c3):
        r = candle_range(c)
        if r == 0 or body_len(c) < r * 0.6:
            return None
    if not (c2["close"] < c1["close"] and c3["close"] < c2["close"]):
        return None
    if not (c1["close"] <= c2["open"] <= c1["open"]):
        return None
    if not (c2["close"] <= c3["open"] <= c2["open"]):
        return None
    for c in (c1, c2, c3):
        if tail_len(c) > body_len(c) * 0.3:
            return None
    return "three consecutive strong bearish candles, each opening within the prior body"


# name -> (detector, candles_involved). candles_involved sets start_index
# relative to the match's end index (index - candles_involved + 1).
_DETECTORS: dict[str, tuple[Callable[[pd.DataFrame], str | None], int]] = {
    "doji": (_detect_doji, 1),
    "hammer": (_detect_hammer, 2),
    "hanging_man": (_detect_hanging_man, 2),
    "inverted_hammer": (_detect_inverted_hammer, 2),
    "shooting_star": (_detect_shooting_star, 2),
    "bullish_marubozu": (_detect_bullish_marubozu, 1),
    "bearish_marubozu": (_detect_bearish_marubozu, 1),
    "bullish_spinning_top": (_detect_bullish_spinning_top, 1),
    "bearish_spinning_top": (_detect_bearish_spinning_top, 1),
    "bullish_engulfing": (_detect_bullish_engulfing, 2),
    "bearish_engulfing": (_detect_bearish_engulfing, 2),
    "bullish_harami": (_detect_bullish_harami, 2),
    "bearish_harami": (_detect_bearish_harami, 2),
    "piercing_line": (_detect_piercing_line, 2),
    "dark_cloud_cover": (_detect_dark_cloud_cover, 2),
    "tweezers_top": (_detect_tweezers_top, 2),
    "tweezers_bottom": (_detect_tweezers_bottom, 2),
    "bullish_kicker": (_detect_bullish_kicker, 2),
    "bearish_kicker": (_detect_bearish_kicker, 2),
    "morning_star": (_detect_morning_star, 3),
    "evening_star": (_detect_evening_star, 3),
    "three_white_soldiers": (_detect_three_white_soldiers, 3),
    "three_black_crows": (_detect_three_black_crows, 3),
}

MAX_CANDLES_INVOLVED = max(n for _, n in _DETECTORS.values())


def detect_at(df: pd.DataFrame, index: int) -> list[PatternHit]:
    """Every pattern that completes on `df.iloc[index]`, using only bars up
    to and including that position (never looking ahead)."""
    window = df.iloc[: index + 1]
    hits = []
    for name, (detector, n_candles) in _DETECTORS.items():
        if len(window) < n_candles:
            continue
        detail = detector(window)
        if detail is not None:
            hits.append(PatternHit(pattern=name, index=index, start_index=index - n_candles + 1, detail=detail))
    return hits


def scan(df: pd.DataFrame) -> list[PatternHit]:
    """Every pattern match across the full series (backtest/analysis use)."""
    hits: list[PatternHit] = []
    for i in range(len(df)):
        hits.extend(detect_at(df, i))
    return hits
