"""TA-Lib CDL* pattern catalog, ported from `xgboosted/pandas-ta-classic`
(MIT license, https://github.com/xgboosted/pandas-ta-classic) -- a
pure-Python, Numba-accelerated reimplementation of TA-Lib's exact CDL*
algorithms. Kept in a separate module from `patterns.py` deliberately,
mirroring how TrAIder_Old split `candlesticks.py` (cm45t3r-derived) from
`candlesticks_talib.py` (TA-Lib-derived) -- same reasoning: different
source, different thresholding philosophy (adaptive trailing averages via
`talib_thresholds.py`, not the fixed single-candle ratios `patterns.py`
uses), worth keeping visibly separate rather than blended.

These are the ~45 TA-Lib patterns NOT already covered by patterns.py's own
port of cm45t3r/candlestick (doji, hammer, hanging_man, inverted_hammer,
shooting_star, bullish/bearish_marubozu, bullish/bearish_spinning_top,
bullish/bearish_engulfing, bullish/bearish_harami, piercing_line,
dark_cloud_cover, tweezers_top/bottom, bullish/bearish_kicker, morning/
evening_star, three_white_soldiers, three_black_crows) -- do not duplicate
those here.

Two deliberate exclusions from TA-Lib's full catalog:
  - "inside" (TA-Lib's CDL_INSIDE isn't actually one of the CDL* reversal/
    continuation patterns -- it's a generic bar-containment flag with no
    Nison-style directional theory behind it, doesn't fit PATTERN_META's
    reversal/continuation/neutral model meaningfully).
  - "kicking" (TA-Lib's CDLKICKING is a stricter marubozu-shaped variant
    of what patterns.py already has as bullish_kicker/bearish_kicker --
    same market event, different strictness; porting a second "kicker"
    definition would be redundant rather than additive). "kickingbylength"
    IS ported, since it's a meaningfully distinct variant (same setup,
    but signal direction follows whichever candle has the longer body).

Same causal, window-based contract as patterns.py: each `_detect_<name>`
answers "did this complete on the LAST bar of the window passed to it?".
`scan()` here mirrors patterns.py's own, for this module's detector set;
signal.py merges both.
"""
from __future__ import annotations

import pandas as pd

from Core.Library.candlestick_trend import talib_thresholds
from Core.Library.candlestick_trend.geometry import body_ends
from Core.Library.candlestick_trend.patterns import PatternHit
from Core.Library.candlestick_trend.talib_thresholds import Setting, threshold


def _split(raw_fn):
    """Wraps a detector that returns `(direction, detail)` (direction is
    +1/-1) into a (bullish_detector, bearish_detector) pair, each
    returning `detail` only when direction matches -- needed because
    PATTERN_META fixes one direction per pattern NAME, but several ported
    TA-Lib patterns can fire either way depending on which candles are
    which color (matching the existing bullish_X/bearish_X split already
    used for engulfing/harami/marubozu/spinning_top in patterns.py)."""

    def bullish(w: pd.DataFrame, i: int) -> str | None:
        result = raw_fn(w, i)
        return result[1] if result and result[0] == 1 else None

    def bearish(w: pd.DataFrame, i: int) -> str | None:
        result = raw_fn(w, i)
        return result[1] if result and result[0] == -1 else None

    return bullish, bearish


def _color(c: pd.Series) -> int:
    return 1 if c["close"] >= c["open"] else -1


def _body_hi(c: pd.Series) -> float:
    return max(c["open"], c["close"])


def _body_lo(c: pd.Series) -> float:
    return min(c["open"], c["close"])


def _real_body(c: pd.Series) -> float:
    return abs(c["close"] - c["open"])


def _upper_shadow(c: pd.Series) -> float:
    return c["high"] - _body_hi(c)


def _lower_shadow(c: pd.Series) -> float:
    return _body_lo(c) - c["low"]


# --- 2 candles ---


def _detect_two_crows(w: pd.DataFrame, i: int) -> str | None:
    if i < 12:  # BodyLong period (10) + 2 + 1, 0-indexed
        return None
    o, c = w["open"], w["close"]
    bl = threshold(w, Setting.BODY_LONG)
    if (
        _color(w.iloc[i - 2]) == 1
        and _real_body(w.iloc[i - 2]) > bl.iloc[i - 2]
        and _color(w.iloc[i - 1]) == -1
        and _body_lo(w.iloc[i - 1]) > _body_hi(w.iloc[i - 2])
        and _color(w.iloc[i]) == -1
        and o.iloc[i] < o.iloc[i - 1]
        and o.iloc[i] > c.iloc[i - 1]
        and c.iloc[i] > o.iloc[i - 2]
        and c.iloc[i] < c.iloc[i - 2]
    ):
        return "white long body, black gapping up, black closing back inside the first body"
    return None


def _detect_three_inside(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 12:
        return None
    o, c = w["open"], w["close"]
    bl, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_SHORT)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if not (
        _real_body(c2) > bl.iloc[i - 2]
        and _real_body(c1) <= bs.iloc[i - 1]
        and _body_hi(c1) < _body_hi(c2)
        and _body_lo(c1) > _body_lo(c2)
    ):
        return None
    color2 = _color(c2)
    if color2 == 1 and _color(ci) == -1 and c.iloc[i] < o.iloc[i - 2]:
        return (-1, "bearish three inside down")
    if color2 == -1 and _color(ci) == 1 and c.iloc[i] > o.iloc[i - 2]:
        return (1, "bullish three inside up")
    return None


_detect_three_inside_bullish, _detect_three_inside_bearish = _split(_detect_three_inside)


def _detect_three_outside(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 2:
        return None
    o, c = w["open"], w["close"]
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if _color(c1) == 1 and _color(c2) == -1 and c.iloc[i - 1] > o.iloc[i - 2] and o.iloc[i - 1] < c.iloc[i - 2] and c.iloc[i] > c.iloc[i - 1]:
        return (1, "bullish three outside up")
    if _color(c1) == -1 and _color(c2) == 1 and o.iloc[i - 1] > c.iloc[i - 2] and c.iloc[i - 1] < o.iloc[i - 2] and c.iloc[i] < c.iloc[i - 1]:
        return (-1, "bearish three outside down")
    return None


_detect_three_outside_bullish, _detect_three_outside_bearish = _split(_detect_three_outside)


def _detect_doji_star(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 11:
        return None
    bl, bd = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_DOJI)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if not (_real_body(c1) > bl.iloc[i - 1] and _real_body(ci) <= bd.iloc[i]):
        return None
    color1 = _color(c1)
    if color1 == 1 and _body_lo(ci) > _body_hi(c1):
        return (-1, "bearish doji star (gaps up after a long white candle)")
    if color1 == -1 and _body_hi(ci) < _body_lo(c1):
        return (1, "bullish doji star (gaps down after a long black candle)")
    return None


_detect_doji_star_bullish, _detect_doji_star_bearish = _split(_detect_doji_star)


def _detect_haramicross(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 11:
        return None
    bl, bd = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_DOJI)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if not (_real_body(c1) > bl.iloc[i - 1] and _real_body(ci) <= bd.iloc[i]):
        return None
    body_top1, body_bot1 = body_ends(c1)
    matched = (_body_hi(ci) < body_top1 and _body_lo(ci) > body_bot1) or (_body_hi(ci) <= body_top1 and _body_lo(ci) >= body_bot1)
    if not matched:
        return None
    direction = -_color(c1)  # opposite of the long prior candle's color -- classic reversal
    detail = "harami cross -- doji contained by a long prior body"
    return (direction, detail)


_detect_harami_cross_bullish, _detect_harami_cross_bearish = _split(_detect_haramicross)


def _detect_belt_hold(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bl, svs = threshold(w, Setting.BODY_LONG).iloc[i], threshold(w, Setting.SHADOW_VERY_SHORT).iloc[i]
    if _real_body(c) <= bl:
        return None
    if _color(c) == 1 and _lower_shadow(c) < svs:
        return (1, "bullish belt hold -- opens at/near the low, closes strong")
    if _color(c) == -1 and _upper_shadow(c) < svs:
        return (-1, "bearish belt hold -- opens at/near the high, closes weak")
    return None


_detect_belt_hold_bullish, _detect_belt_hold_bearish = _split(_detect_belt_hold)


def _detect_closing_marubozu(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bl, svs = threshold(w, Setting.BODY_LONG).iloc[i], threshold(w, Setting.SHADOW_VERY_SHORT).iloc[i]
    if _real_body(c) <= bl:
        return None
    if _color(c) == 1 and _upper_shadow(c) < svs:
        return (1, "bullish closing marubozu -- closes at/near the high")
    if _color(c) == -1 and _lower_shadow(c) < svs:
        return (-1, "bearish closing marubozu -- closes at/near the low")
    return None


_detect_closing_marubozu_bullish, _detect_closing_marubozu_bearish = _split(_detect_closing_marubozu)


def _detect_long_line(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bl, ss = threshold(w, Setting.BODY_LONG).iloc[i], threshold(w, Setting.SHADOW_SHORT).iloc[i]
    if _real_body(c) > bl and _upper_shadow(c) < ss and _lower_shadow(c) < ss:
        return (_color(c), "long line -- long body, short shadows both sides")
    return None


_detect_long_line_bullish, _detect_long_line_bearish = _split(_detect_long_line)


def _detect_short_line(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bs, ss = threshold(w, Setting.BODY_SHORT).iloc[i], threshold(w, Setting.SHADOW_SHORT).iloc[i]
    if _real_body(c) < bs and _upper_shadow(c) < ss and _lower_shadow(c) < ss:
        return "short line -- small body, short shadows both sides, low activity"
    return None


def _detect_high_wave(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bs = threshold(w, Setting.BODY_SHORT).iloc[i]
    svl = threshold(w, Setting.SHADOW_VERY_LONG).iloc[i]
    if _real_body(c) < bs and _upper_shadow(c) > svl and _lower_shadow(c) > svl:
        return "high wave -- small body, very long shadows both sides, extreme indecision"
    return None


def _detect_long_legged_doji(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bd = threshold(w, Setting.BODY_DOJI).iloc[i]
    sl = threshold(w, Setting.SHADOW_LONG).iloc[i]
    if _real_body(c) <= bd and (_lower_shadow(c) > sl or _upper_shadow(c) > sl):
        return "long-legged doji -- tiny body, at least one long shadow"
    return None


def _detect_dragonfly_doji(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bd, svs = threshold(w, Setting.BODY_DOJI).iloc[i], threshold(w, Setting.SHADOW_VERY_SHORT).iloc[i]
    if _real_body(c) <= bd and _upper_shadow(c) < svs and _lower_shadow(c) > svs:
        return "dragonfly doji -- tiny body at the top, long lower shadow"
    return None


def _detect_gravestone_doji(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bd, svs = threshold(w, Setting.BODY_DOJI).iloc[i], threshold(w, Setting.SHADOW_VERY_SHORT).iloc[i]
    if _real_body(c) <= bd and _lower_shadow(c) < svs and _upper_shadow(c) > svs:
        return "gravestone doji -- tiny body at the bottom, long upper shadow"
    return None


def _detect_takuri(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bd, svs, svl = threshold(w, Setting.BODY_DOJI).iloc[i], threshold(w, Setting.SHADOW_VERY_SHORT).iloc[i], threshold(w, Setting.SHADOW_VERY_LONG).iloc[i]
    if _real_body(c) <= bd and _upper_shadow(c) < svs and _lower_shadow(c) > svl:
        return "takuri -- dragonfly-doji shape with an extreme lower shadow"
    return None


def _detect_rickshaw_man(w: pd.DataFrame, i: int) -> str | None:
    if i < 10:
        return None
    c = w.iloc[i]
    bd = threshold(w, Setting.BODY_DOJI).iloc[i]
    sl = threshold(w, Setting.SHADOW_LONG).iloc[i]
    near = threshold(w, Setting.NEAR).iloc[i]
    hl_range = c["high"] - c["low"]
    mid = c["low"] + hl_range / 2.0
    if (
        _real_body(c) <= bd
        and _lower_shadow(c) > sl
        and _upper_shadow(c) > sl
        and _body_lo(c) <= mid + near
        and _body_hi(c) >= mid - near
    ):
        return "rickshaw man -- long-legged doji with the body centered in the range"
    return None


# --- 3 candles ---


def _detect_advance_block(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, c = w["open"], w["close"]
    near, bl, ss, far, sl = (
        threshold(w, Setting.NEAR),
        threshold(w, Setting.BODY_LONG),
        threshold(w, Setting.SHADOW_SHORT),
        threshold(w, Setting.FAR),
        threshold(w, Setting.SHADOW_LONG),
    )
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if not (
        _color(c2) == 1
        and _color(c1) == 1
        and _color(ci) == 1
        and c.iloc[i] > c.iloc[i - 1] > c.iloc[i - 2]
        and o.iloc[i - 1] > o.iloc[i - 2]
        and o.iloc[i - 1] <= c.iloc[i - 2] + near.iloc[i - 2]
        and o.iloc[i] > o.iloc[i - 1]
        and o.iloc[i] <= c.iloc[i - 1] + near.iloc[i - 1]
        and _real_body(c2) > bl.iloc[i - 2]
        and _upper_shadow(c2) < ss.iloc[i - 2]
    ):
        return None
    rb2, rb1, rbi = _real_body(c2), _real_body(c1), _real_body(ci)
    weakening = (
        (rb1 < rb2 - far.iloc[i - 2] and rbi < rb1 + near.iloc[i - 1])
        or (rbi < rb1 - far.iloc[i - 1])
        or (rbi < rb1 and rb1 < rb2 and (_upper_shadow(ci) > ss.iloc[i] or _upper_shadow(c1) > ss.iloc[i - 1]))
        or (rbi < rb1 and _upper_shadow(ci) > sl.iloc[i])
    )
    if weakening:
        return "advance block -- three rising white candles losing momentum"
    return None


def _detect_stalled_pattern(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, c = w["open"], w["close"]
    bl, svs, near, bs = (
        threshold(w, Setting.BODY_LONG),
        threshold(w, Setting.SHADOW_VERY_SHORT),
        threshold(w, Setting.NEAR),
        threshold(w, Setting.BODY_SHORT),
    )
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c2) == 1
        and _color(c1) == 1
        and _color(ci) == 1
        and c.iloc[i] > c.iloc[i - 1] > c.iloc[i - 2]
        and _real_body(c2) > bl.iloc[i - 2]
        and _real_body(c1) > bl.iloc[i - 1]
        and _upper_shadow(c1) < svs.iloc[i - 1]
        and o.iloc[i - 1] > o.iloc[i - 2]
        and o.iloc[i - 1] <= c.iloc[i - 2] + near.iloc[i - 2]
        and _real_body(ci) < bs.iloc[i]
        and o.iloc[i] >= c.iloc[i - 1] - _real_body(ci) - near.iloc[i - 1]
    ):
        return "stalled pattern -- rising three white candles, the third stalling on the second's shoulder"
    return None


def _detect_three_line_strike(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 7:
        return None
    o, c = w["open"], w["close"]
    near = threshold(w, Setting.NEAR)
    c3, c2, c1, ci = w.iloc[i - 3], w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if not (_color(c3) == _color(c2) == _color(c1) and _color(ci) == -_color(c1)):
        return None
    t3, b3 = body_ends(c3)
    t2, b2 = body_ends(c2)
    if not (b3 - near.iloc[i - 3] <= o.iloc[i - 2] <= t3 + near.iloc[i - 3]):
        return None
    if not (b2 - near.iloc[i - 2] <= o.iloc[i - 1] <= t2 + near.iloc[i - 2]):
        return None
    if _color(c1) == 1 and c.iloc[i - 1] > c.iloc[i - 2] > c.iloc[i - 3] and o.iloc[i] > c.iloc[i - 1] and c.iloc[i] < o.iloc[i - 3]:
        return (-1, "bearish three-line strike -- fourth candle engulfs three rising whites")
    if _color(c1) == -1 and c.iloc[i - 1] < c.iloc[i - 2] < c.iloc[i - 3] and o.iloc[i] < c.iloc[i - 1] and c.iloc[i] > o.iloc[i - 3]:
        return (1, "bullish three-line strike -- fourth candle engulfs three falling blacks")
    return None


_detect_three_line_strike_bullish, _detect_three_line_strike_bearish = _split(_detect_three_line_strike)


def _detect_three_stars_in_south(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, h, low, c = w["open"], w["high"], w["low"], w["close"]
    bl, sl, svs, bs = (
        threshold(w, Setting.BODY_LONG),
        threshold(w, Setting.SHADOW_LONG),
        threshold(w, Setting.SHADOW_VERY_SHORT),
        threshold(w, Setting.BODY_SHORT),
    )
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c2) == -1
        and _color(c1) == -1
        and _color(ci) == -1
        and _real_body(c2) > bl.iloc[i - 2]
        and _lower_shadow(c2) > sl.iloc[i - 2]
        and _real_body(c1) < _real_body(c2)
        and o.iloc[i - 1] > c.iloc[i - 2]
        and o.iloc[i - 1] <= h.iloc[i - 2]
        and low.iloc[i - 1] < c.iloc[i - 2]
        and low.iloc[i - 1] >= low.iloc[i - 2]
        and _lower_shadow(c1) > svs.iloc[i - 1]
        and _real_body(ci) < bs.iloc[i]
        and _lower_shadow(ci) < svs.iloc[i]
        and _upper_shadow(ci) < svs.iloc[i]
        and low.iloc[i] > low.iloc[i - 1]
        and h.iloc[i] < h.iloc[i - 1]
    ):
        return "three stars in the south -- bullish bottoming pattern in three black candles"
    return None


def _detect_identical_three_crows(w: pd.DataFrame, i: int) -> str | None:
    if i < 7:
        return None
    o, c = w["open"], w["close"]
    svs, eq = threshold(w, Setting.SHADOW_VERY_SHORT), threshold(w, Setting.EQUAL)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c2) == -1
        and _lower_shadow(c2) < svs.iloc[i - 2]
        and _color(c1) == -1
        and _lower_shadow(c1) < svs.iloc[i - 1]
        and _color(ci) == -1
        and _lower_shadow(ci) < svs.iloc[i]
        and c.iloc[i - 2] > c.iloc[i - 1] > c.iloc[i]
        and abs(o.iloc[i - 1] - c.iloc[i - 2]) <= eq.iloc[i - 2]
        and abs(o.iloc[i] - c.iloc[i - 1]) <= eq.iloc[i - 1]
    ):
        return "identical three crows -- three near-identical falling marubozu-like blacks"
    return None


def _detect_unique_three_river(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, low, c = w["open"], w["low"], w["close"]
    bl, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_SHORT)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _real_body(c2) > bl.iloc[i - 2]
        and _color(c2) == -1
        and _color(c1) == -1
        and c.iloc[i - 1] > c.iloc[i - 2]
        and o.iloc[i - 1] <= o.iloc[i - 2]
        and low.iloc[i - 1] < low.iloc[i - 2]
        and _real_body(ci) < bs.iloc[i]
        and _color(ci) == 1
        and o.iloc[i] > low.iloc[i - 1]
    ):
        return "unique three river -- bullish bottoming pattern"
    return None


def _detect_stick_sandwich(w: pd.DataFrame, i: int) -> str | None:
    if i < 5:
        return None
    low, c = w["low"], w["close"]
    eq = threshold(w, Setting.EQUAL)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c2) == -1
        and _color(c1) == 1
        and _color(ci) == -1
        and low.iloc[i - 1] > c.iloc[i - 2]
        and abs(c.iloc[i] - c.iloc[i - 2]) <= eq.iloc[i - 2]
    ):
        return "stick sandwich -- bullish, black-white-black with matching closes"
    return None


def _detect_matching_low(w: pd.DataFrame, i: int) -> str | None:
    if i < 5:
        return None
    c = w["close"]
    eq = threshold(w, Setting.EQUAL)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if _color(c1) == -1 and _color(ci) == -1 and abs(c.iloc[i] - c.iloc[i - 1]) <= eq.iloc[i - 1]:
        return "matching low -- bullish, two black candles closing at nearly the same level"
    return None


def _detect_separating_lines(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 10:
        return None
    o = w["open"]
    bl, eq, svs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.EQUAL), threshold(w, Setting.SHADOW_VERY_SHORT)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if not (
        _color(c1) == -_color(ci)
        and abs(o.iloc[i] - o.iloc[i - 1]) <= eq.iloc[i - 1]
        and _real_body(ci) > bl.iloc[i]
    ):
        return None
    if _color(ci) == 1 and _lower_shadow(ci) < svs.iloc[i]:
        return (1, "bullish separating lines -- continuation")
    if _color(ci) == -1 and _upper_shadow(ci) < svs.iloc[i]:
        return (-1, "bearish separating lines -- continuation")
    return None


_detect_separating_lines_bullish, _detect_separating_lines_bearish = _split(_detect_separating_lines)


def _detect_counterattack(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 11:
        return None
    c = w["close"]
    bl, eq = threshold(w, Setting.BODY_LONG), threshold(w, Setting.EQUAL)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if (
        _color(c1) == -_color(ci)
        and _real_body(c1) > bl.iloc[i - 1]
        and _real_body(ci) > bl.iloc[i]
        and abs(c.iloc[i] - c.iloc[i - 1]) <= eq.iloc[i - 1]
    ):
        return (_color(ci), "counterattack -- opposite-colored candles closing at nearly the same level")
    return None


_detect_counterattack_bullish, _detect_counterattack_bearish = _split(_detect_counterattack)


def _detect_upside_gap_two_crows(w: pd.DataFrame, i: int) -> str | None:
    if i < 12:
        return None
    o, c = w["open"], w["close"]
    bl, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_SHORT)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c2) == 1
        and _real_body(c2) > bl.iloc[i - 2]
        and _color(c1) == -1
        and _real_body(c1) <= bs.iloc[i - 1]
        and _body_lo(c1) > _body_hi(c2)
        and _color(ci) == -1
        and o.iloc[i] > o.iloc[i - 1]
        and c.iloc[i] < c.iloc[i - 1]
        and c.iloc[i] > c.iloc[i - 2]
    ):
        return "upside gap two crows -- bearish, gap up then two black candles closing back down but not filling the gap"
    return None


def _detect_homing_pigeon(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, c = w["open"], w["close"]
    bl, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_SHORT)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if (
        _color(c1) == -1
        and _color(ci) == -1
        and _real_body(c1) > bl.iloc[i - 1]
        and _real_body(ci) <= bs.iloc[i]
        and o.iloc[i] < o.iloc[i - 1]
        and c.iloc[i] > c.iloc[i - 1]
    ):
        return "homing pigeon -- bullish, small black body inside a long black body"
    return None


def _detect_in_neck(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, low, c = w["open"], w["low"], w["close"]
    bl, eq = threshold(w, Setting.BODY_LONG), threshold(w, Setting.EQUAL)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if (
        _color(c1) == -1
        and _real_body(c1) > bl.iloc[i - 1]
        and _color(ci) == 1
        and o.iloc[i] < low.iloc[i - 1]
        and c.iloc[i - 1] <= c.iloc[i] <= c.iloc[i - 1] + eq.iloc[i - 1]
    ):
        return "in neck -- bearish continuation, white candle barely pokes above the black close"
    return None


def _detect_on_neck(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, low, c = w["open"], w["low"], w["close"]
    bl, eq = threshold(w, Setting.BODY_LONG), threshold(w, Setting.EQUAL)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if (
        _color(c1) == -1
        and _real_body(c1) > bl.iloc[i - 1]
        and _color(ci) == 1
        and o.iloc[i] < low.iloc[i - 1]
        and abs(c.iloc[i] - low.iloc[i - 1]) <= eq.iloc[i - 1]
    ):
        return "on neck -- bearish continuation, white candle closes at the black candle's low"
    return None


def _detect_thrusting(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    o, low, c = w["open"], w["low"], w["close"]
    bl, eq = threshold(w, Setting.BODY_LONG), threshold(w, Setting.EQUAL)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if (
        _color(c1) == -1
        and _real_body(c1) > bl.iloc[i - 1]
        and _color(ci) == 1
        and o.iloc[i] < low.iloc[i - 1]
        and c.iloc[i] > c.iloc[i - 1] + eq.iloc[i - 1]
        and c.iloc[i] <= o.iloc[i - 1] - _real_body(c1) * 0.5
    ):
        return "thrusting -- bearish continuation, white candle closes just under the black body's midpoint"
    return None


def _detect_gap_side_by_side_white(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 5:
        return None
    o = w["open"]
    near, eq = threshold(w, Setting.NEAR), threshold(w, Setting.EQUAL)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    gap_up = _body_lo(c1) > _body_hi(c2)
    gap_down = _body_hi(c1) < _body_lo(c2)
    if not (
        (gap_up or gap_down)
        and _color(c1) == 1
        and _color(ci) == 1
        and abs(_real_body(ci) - _real_body(c1)) <= near.iloc[i - 1]
        and abs(o.iloc[i] - o.iloc[i - 1]) <= eq.iloc[i - 1]
    ):
        return None
    return (1, "gap side-by-side white -- continuation") if gap_up else (-1, "gap side-by-side white -- continuation (down-gap)")


_detect_gap_side_by_side_white_bullish, _detect_gap_side_by_side_white_bearish = _split(_detect_gap_side_by_side_white)


def _detect_side_by_side_gap_three_methods(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 2:
        return None
    o, c = w["open"], w["close"]
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    t2, b2 = body_ends(c2)
    t1, b1 = body_ends(c1)
    if not (_color(c2) == _color(c1) and _color(c1) == -_color(ci)):
        return None
    if not (b1 < o.iloc[i] < t1 and b2 < c.iloc[i] < t2):
        return None
    if _color(c2) == 1 and _body_lo(c1) > _body_hi(c2):
        return (1, "upside gap three methods -- bullish continuation")
    if _color(c2) == -1 and _body_hi(c1) < _body_lo(c2):
        return (-1, "downside gap three methods -- bearish continuation")
    return None


_detect_side_by_side_gap_three_methods_bullish, _detect_side_by_side_gap_three_methods_bearish = _split(_detect_side_by_side_gap_three_methods)


def _detect_tasuki_gap(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 5:
        return None
    o, c = w["open"], w["close"]
    near = threshold(w, Setting.NEAR)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    rb1, rbi = _real_body(c1), _real_body(ci)
    up = (
        _body_lo(c1) > _body_hi(c2)
        and _color(c1) == 1
        and _color(ci) == -1
        and o.iloc[i] < c.iloc[i - 1]
        and o.iloc[i] > o.iloc[i - 1]
        and c.iloc[i] < o.iloc[i - 1]
        and c.iloc[i] > _body_hi(c2)
        and abs(rb1 - rbi) < near.iloc[i - 1]
    )
    down = (
        _body_hi(c1) < _body_lo(c2)
        and _color(c1) == -1
        and _color(ci) == 1
        and o.iloc[i] < o.iloc[i - 1]
        and o.iloc[i] > c.iloc[i - 1]
        and c.iloc[i] > o.iloc[i - 1]
        and c.iloc[i] < _body_lo(c2)
        and abs(rb1 - rbi) < near.iloc[i - 1]
    )
    if up:
        return (1, "upside tasuki gap -- bullish continuation, the gap isn't filled")
    if down:
        return (-1, "downside tasuki gap -- bearish continuation, the gap isn't filled")
    return None


_detect_tasuki_gap_bullish, _detect_tasuki_gap_bearish = _split(_detect_tasuki_gap)


def _detect_kicking_by_length(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 10:
        return None
    h, low = w["high"], w["low"]
    bl, svs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.SHADOW_VERY_SHORT)
    c1, ci = w.iloc[i - 1], w.iloc[i]
    if not (
        _color(c1) == -_color(ci)
        and _real_body(c1) > bl.iloc[i - 1]
        and _upper_shadow(c1) < svs.iloc[i - 1]
        and _lower_shadow(c1) < svs.iloc[i - 1]
        and _real_body(ci) > bl.iloc[i]
        and _upper_shadow(ci) < svs.iloc[i]
        and _lower_shadow(ci) < svs.iloc[i]
        and ((_color(c1) == -1 and low.iloc[i] > h.iloc[i - 1]) or (_color(c1) == 1 and h.iloc[i] < low.iloc[i - 1]))
    ):
        return None
    winner = ci if _real_body(ci) > _real_body(c1) else c1
    direction = _color(winner)
    detail = "bullish kicking by length -- gap in the direction of the longer marubozu" if direction == 1 else "bearish kicking by length -- gap in the direction of the longer marubozu"
    return (direction, detail)


_detect_kicking_by_length_bullish, _detect_kicking_by_length_bearish = _split(_detect_kicking_by_length)


def _detect_ladder_bottom(w: pd.DataFrame, i: int) -> str | None:
    if i < 14:
        return None
    o, h, c = w["open"], w["high"], w["close"]
    svs = threshold(w, Setting.SHADOW_VERY_SHORT)
    c4, c3, c2, c1, ci = w.iloc[i - 4], w.iloc[i - 3], w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c4) == -1
        and _color(c3) == -1
        and _color(c2) == -1
        and o.iloc[i - 4] > o.iloc[i - 3] > o.iloc[i - 2]
        and c.iloc[i - 4] > c.iloc[i - 3] > c.iloc[i - 2]
        and _color(c1) == -1
        and _upper_shadow(c1) > svs.iloc[i - 1]
        and _color(ci) == 1
        and o.iloc[i] > o.iloc[i - 1]
        and c.iloc[i] > h.iloc[i - 1]
    ):
        return "ladder bottom -- bullish reversal after four falling black candles"
    return None


def _detect_mat_hold(w: pd.DataFrame, i: int) -> str | None:
    if i < 13:
        return None
    o, h, c = w["open"], w["high"], w["close"]
    bl, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_SHORT)
    penetration = 0.5
    c4, c3, c2, c1, ci = w.iloc[i - 4], w.iloc[i - 3], w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _real_body(c4) > bl.iloc[i - 4]
        and _real_body(c3) < bs.iloc[i - 3]
        and _real_body(c2) < bs.iloc[i - 2]
        and _real_body(c1) < bs.iloc[i - 1]
        and _color(c4) == 1
        and _color(c3) == -1
        and _color(ci) == 1
        and _body_lo(c3) > _body_hi(c4)
        and min(o.iloc[i - 2], c.iloc[i - 2]) < c.iloc[i - 4]
        and min(o.iloc[i - 1], c.iloc[i - 1]) < c.iloc[i - 4]
        and min(o.iloc[i - 2], c.iloc[i - 2]) > c.iloc[i - 4] - _real_body(c4) * penetration
        and min(o.iloc[i - 1], c.iloc[i - 1]) > c.iloc[i - 4] - _real_body(c4) * penetration
        and max(c.iloc[i - 2], o.iloc[i - 2]) < o.iloc[i - 3]
        and max(c.iloc[i - 1], o.iloc[i - 1]) < max(c.iloc[i - 2], o.iloc[i - 2])
        and o.iloc[i] > c.iloc[i - 1]
        and c.iloc[i] > max(h.iloc[i - 3], h.iloc[i - 2], h.iloc[i - 1])
    ):
        return "mat hold -- bullish continuation, long white then three small pullback candles then a strong white breakout"
    return None


def _detect_rise_fall_three_methods(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 13:
        return None
    o, h, low, c = w["open"], w["high"], w["low"], w["close"]
    bl, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_SHORT)
    c4, c3, c2, c1, ci = w.iloc[i - 4], w.iloc[i - 3], w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    color4 = _color(c4)
    if not (
        _real_body(c4) > bl.iloc[i - 4]
        and _real_body(c3) < bs.iloc[i - 3]
        and _real_body(c2) < bs.iloc[i - 2]
        and _real_body(c1) < bs.iloc[i - 1]
        and _real_body(ci) > bl.iloc[i]
        and color4 == -_color(c3)
        and _color(c3) == _color(c2)
        and _color(c2) == _color(c1)
        and _color(c1) == -_color(ci)
    ):
        return None
    for cc in (c3, c2, c1):
        if not (min(cc["open"], cc["close"]) < h.iloc[i - 4] and max(cc["open"], cc["close"]) > low.iloc[i - 4]):
            return None
    if not (c.iloc[i - 2] * color4 < c.iloc[i - 3] * color4 and c.iloc[i - 1] * color4 < c.iloc[i - 2] * color4):
        return None
    if not (o.iloc[i] * color4 > c.iloc[i - 1] * color4 and c.iloc[i] * color4 > c.iloc[i - 4] * color4):
        return None
    direction = color4
    detail = "rising three methods -- bullish continuation" if direction == 1 else "falling three methods -- bearish continuation"
    return (direction, detail)


_detect_rise_fall_three_methods_bullish, _detect_rise_fall_three_methods_bearish = _split(_detect_rise_fall_three_methods)


def _detect_tri_star(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 12:
        return None
    o, c = w["open"], w["close"]
    bd = threshold(w, Setting.BODY_DOJI)
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if not (_real_body(c2) <= bd.iloc[i - 2] and _real_body(c1) <= bd.iloc[i - 1] and _real_body(ci) <= bd.iloc[i]):
        return None
    if _body_lo(c1) > _body_hi(c2) and _body_hi(ci) < max(o.iloc[i - 1], c.iloc[i - 1]):
        return (-1, "bearish tri-star -- three dojis, middle gapping up then reversing down")
    if _body_hi(c1) < _body_lo(c2) and min(o.iloc[i], c.iloc[i]) > _body_lo(c1):
        return (1, "bullish tri-star -- three dojis, middle gapping down then reversing up")
    return None


_detect_tri_star_bullish, _detect_tri_star_bearish = _split(_detect_tri_star)


def _detect_abandoned_baby(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 11:
        return None
    c = w["close"]
    bl, bd, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_DOJI), threshold(w, Setting.BODY_SHORT)
    penetration = 0.3
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if not (_real_body(c2) > bl.iloc[i - 2] and _real_body(c1) <= bd.iloc[i - 1] and _real_body(ci) > bs.iloc[i]):
        return None
    if (
        _color(c2) == 1
        and _color(ci) == -1
        and c.iloc[i] < c.iloc[i - 2] - _real_body(c2) * penetration
        and _body_lo(c1) > _body_hi(c2)
        and _body_hi(ci) < _body_lo(c1)
    ):
        return (-1, "bearish abandoned baby -- doji gaps away on both sides")
    if (
        _color(c2) == -1
        and _color(ci) == 1
        and c.iloc[i] > c.iloc[i - 2] + _real_body(c2) * penetration
        and _body_hi(c1) < _body_lo(c2)
        and _body_lo(ci) > _body_hi(c1)
    ):
        return (1, "bullish abandoned baby -- doji gaps away on both sides")
    return None


_detect_abandoned_baby_bullish, _detect_abandoned_baby_bearish = _split(_detect_abandoned_baby)


def _detect_morning_doji_star(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    c = w["close"]
    bl, bd, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_DOJI), threshold(w, Setting.BODY_SHORT)
    penetration = 0.3
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _real_body(c2) > bl.iloc[i - 2]
        and _color(c2) == -1
        and _real_body(c1) <= bd.iloc[i - 1]
        and _body_hi(c1) < _body_lo(c2)
        and _real_body(ci) > bs.iloc[i]
        and _color(ci) == 1
        and c.iloc[i] > c.iloc[i - 2] + _real_body(c2) * penetration
    ):
        return "morning doji star -- bullish, doji gapping down then a strong white reversal"
    return None


def _detect_evening_doji_star(w: pd.DataFrame, i: int) -> str | None:
    if i < 11:
        return None
    c = w["close"]
    bl, bd, bs = threshold(w, Setting.BODY_LONG), threshold(w, Setting.BODY_DOJI), threshold(w, Setting.BODY_SHORT)
    penetration = 0.3
    c2, c1, ci = w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _real_body(c2) > bl.iloc[i - 2]
        and _color(c2) == 1
        and _real_body(c1) <= bd.iloc[i - 1]
        and _body_lo(c1) > _body_hi(c2)
        and _real_body(ci) > bs.iloc[i]
        and _color(ci) == -1
        and c.iloc[i] < c.iloc[i - 2] - _real_body(c2) * penetration
    ):
        return "evening doji star -- bearish, doji gapping up then a strong black reversal"
    return None


def _detect_conceal_baby_swallow(w: pd.DataFrame, i: int) -> str | None:
    if i < 5:
        return None
    h, low, c = w["high"], w["low"], w["close"]
    svs = threshold(w, Setting.SHADOW_VERY_SHORT)
    c3, c2, c1, ci = w.iloc[i - 3], w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if (
        _color(c3) == -1
        and _color(c2) == -1
        and _color(c1) == -1
        and _color(ci) == -1
        and _lower_shadow(c3) < svs.iloc[i - 3]
        and _upper_shadow(c3) < svs.iloc[i - 3]
        and _lower_shadow(c2) < svs.iloc[i - 2]
        and _upper_shadow(c2) < svs.iloc[i - 2]
        and _body_hi(c1) < _body_lo(c2)
        and _upper_shadow(c1) > svs.iloc[i - 1]
        and h.iloc[i - 1] > c.iloc[i - 2]
        and h.iloc[i] > h.iloc[i - 1]
        and low.iloc[i] < low.iloc[i - 1]
    ):
        return "concealing baby swallow -- bullish, fourth candle engulfs the third's shadow"
    return None


# --- stateful (hikkake family) ---


def _hikkake_setup(w: pd.DataFrame, i: int) -> bool:
    h, low = w["high"], w["low"]
    return (
        h.iloc[i - 1] < h.iloc[i - 2]
        and low.iloc[i - 1] > low.iloc[i - 2]
        and ((h.iloc[i] < h.iloc[i - 1] and low.iloc[i] < low.iloc[i - 1]) or (h.iloc[i] > h.iloc[i - 1] and low.iloc[i] > low.iloc[i - 1]))
    )


def _detect_hikkake(w: pd.DataFrame, index: int) -> tuple[int, str] | None:
    if index < 7:
        return None
    h, low, c = w["high"], w["low"], w["close"]
    last = index
    pattern_idx, pattern_dir = None, 0
    for i in range(max(2, last - 6), last + 1):
        if _hikkake_setup(w, i):
            pattern_dir = 1 if h.iloc[i] < h.iloc[i - 1] else -1
            pattern_idx = i
        elif pattern_idx is not None and i <= pattern_idx + 3:
            confirmed = (pattern_dir > 0 and c.iloc[i] > h.iloc[pattern_idx - 1]) or (pattern_dir < 0 and c.iloc[i] < low.iloc[pattern_idx - 1])
            if confirmed:
                if i == last:
                    detail = "bullish hikkake confirmation" if pattern_dir > 0 else "bearish hikkake confirmation"
                    return (pattern_dir, detail)
                pattern_idx = None
    return None


_detect_hikkake_bullish, _detect_hikkake_bearish = _split(_detect_hikkake)


def _hikkakemod_setup(w: pd.DataFrame, i: int, near: pd.Series) -> bool:
    h, low, c = w["high"], w["low"], w["close"]
    if not (h.iloc[i - 2] < h.iloc[i - 3] and low.iloc[i - 2] > low.iloc[i - 3] and h.iloc[i - 1] < h.iloc[i - 2] and low.iloc[i - 1] > low.iloc[i - 2]):
        return False
    return (h.iloc[i] < h.iloc[i - 1] and low.iloc[i] < low.iloc[i - 1] and c.iloc[i - 2] <= low.iloc[i - 2] + near.iloc[i - 2]) or (
        h.iloc[i] > h.iloc[i - 1] and low.iloc[i] > low.iloc[i - 1] and c.iloc[i - 2] >= h.iloc[i - 2] - near.iloc[i - 2]
    )


def _detect_hikkake_modified(w: pd.DataFrame, index: int) -> tuple[int, str] | None:
    if index < 11:
        return None
    h, low, c = w["high"], w["low"], w["close"]
    near = threshold(w, Setting.NEAR)
    last = index
    pattern_idx, pattern_dir = None, 0
    for i in range(max(3, last - 6), last + 1):
        if _hikkakemod_setup(w, i, near):
            pattern_dir = 1 if h.iloc[i] < h.iloc[i - 1] else -1
            pattern_idx = i
        elif pattern_idx is not None and i <= pattern_idx + 3:
            confirmed = (pattern_dir > 0 and c.iloc[i] > h.iloc[pattern_idx - 1]) or (pattern_dir < 0 and c.iloc[i] < low.iloc[pattern_idx - 1])
            if confirmed:
                if i == last:
                    detail = "bullish modified-hikkake confirmation" if pattern_dir > 0 else "bearish modified-hikkake confirmation"
                    return (pattern_dir, detail)
                pattern_idx = None
    return None


_detect_hikkake_modified_bullish, _detect_hikkake_modified_bearish = _split(_detect_hikkake_modified)


def _detect_breakaway(w: pd.DataFrame, i: int) -> tuple[int, str] | None:
    if i < 14:
        return None
    o, h, low, c = w["open"], w["high"], w["low"], w["close"]
    bl = threshold(w, Setting.BODY_LONG)
    c4, c3, c2, c1, ci = w.iloc[i - 4], w.iloc[i - 3], w.iloc[i - 2], w.iloc[i - 1], w.iloc[i]
    if not (
        _real_body(c4) > bl.iloc[i - 4]
        and _color(c4) == _color(c3)
        and _color(c3) == _color(c1)
        and _color(c1) == -_color(ci)
    ):
        return None
    if _color(c4) == -1:
        ok = (
            _body_hi(c3) < _body_lo(c4)
            and h.iloc[i - 2] < h.iloc[i - 3]
            and low.iloc[i - 2] < low.iloc[i - 3]
            and h.iloc[i - 1] < h.iloc[i - 2]
            and low.iloc[i - 1] < low.iloc[i - 2]
            and c.iloc[i] > o.iloc[i - 3]
            and c.iloc[i] < c.iloc[i - 4]
        )
        return (1, "bullish breakaway -- five-candle reversal") if ok else None
    ok = (
        _body_lo(c3) > _body_hi(c4)
        and h.iloc[i - 2] > h.iloc[i - 3]
        and low.iloc[i - 2] > low.iloc[i - 3]
        and h.iloc[i - 1] > h.iloc[i - 2]
        and low.iloc[i - 1] > low.iloc[i - 2]
        and c.iloc[i] < o.iloc[i - 3]
        and c.iloc[i] > c.iloc[i - 4]
    )
    return (-1, "bearish breakaway -- five-candle reversal") if ok else None


_detect_breakaway_bullish, _detect_breakaway_bearish = _split(_detect_breakaway)


# name -> (detector, candles_involved)
_TALIB_DETECTORS: dict = {
    # single-direction (unidirectional per TA-Lib's own algorithm)
    "two_crows": (_detect_two_crows, 3),
    "three_stars_in_south": (_detect_three_stars_in_south, 3),
    "advance_block": (_detect_advance_block, 3),
    "conceal_baby_swallow": (_detect_conceal_baby_swallow, 4),
    "evening_doji_star": (_detect_evening_doji_star, 3),
    "identical_three_crows": (_detect_identical_three_crows, 3),
    "ladder_bottom": (_detect_ladder_bottom, 5),
    "matching_low": (_detect_matching_low, 2),
    "mat_hold": (_detect_mat_hold, 5),
    "morning_doji_star": (_detect_morning_doji_star, 3),
    "in_neck": (_detect_in_neck, 2),
    "on_neck": (_detect_on_neck, 2),
    "stalled_pattern": (_detect_stalled_pattern, 3),
    "stick_sandwich": (_detect_stick_sandwich, 3),
    "takuri": (_detect_takuri, 1),
    "thrusting": (_detect_thrusting, 2),
    "unique_three_river": (_detect_unique_three_river, 3),
    "upside_gap_two_crows": (_detect_upside_gap_two_crows, 3),
    "homing_pigeon": (_detect_homing_pigeon, 2),
    "dragonfly_doji": (_detect_dragonfly_doji, 1),
    "gravestone_doji": (_detect_gravestone_doji, 1),
    # neutral/indecision (no direction -- filtered out in signal.py like doji/spinning_top)
    "high_wave": (_detect_high_wave, 1),
    "long_legged_doji": (_detect_long_legged_doji, 1),
    "rickshaw_man": (_detect_rickshaw_man, 1),
    "short_line": (_detect_short_line, 1),
    # dual-direction -- registered as split bullish_X/bearish_X pairs
    "three_inside_bullish": (_detect_three_inside_bullish, 3),
    "three_inside_bearish": (_detect_three_inside_bearish, 3),
    "three_outside_bullish": (_detect_three_outside_bullish, 3),
    "three_outside_bearish": (_detect_three_outside_bearish, 3),
    "three_line_strike_bullish": (_detect_three_line_strike_bullish, 4),
    "three_line_strike_bearish": (_detect_three_line_strike_bearish, 4),
    "abandoned_baby_bullish": (_detect_abandoned_baby_bullish, 3),
    "abandoned_baby_bearish": (_detect_abandoned_baby_bearish, 3),
    "belt_hold_bullish": (_detect_belt_hold_bullish, 1),
    "belt_hold_bearish": (_detect_belt_hold_bearish, 1),
    "breakaway_bullish": (_detect_breakaway_bullish, 5),
    "breakaway_bearish": (_detect_breakaway_bearish, 5),
    "closing_marubozu_bullish": (_detect_closing_marubozu_bullish, 1),
    "closing_marubozu_bearish": (_detect_closing_marubozu_bearish, 1),
    "counterattack_bullish": (_detect_counterattack_bullish, 2),
    "counterattack_bearish": (_detect_counterattack_bearish, 2),
    "doji_star_bullish": (_detect_doji_star_bullish, 2),
    "doji_star_bearish": (_detect_doji_star_bearish, 2),
    "gap_side_by_side_white_bullish": (_detect_gap_side_by_side_white_bullish, 3),
    "gap_side_by_side_white_bearish": (_detect_gap_side_by_side_white_bearish, 3),
    "harami_cross_bullish": (_detect_harami_cross_bullish, 2),
    "harami_cross_bearish": (_detect_harami_cross_bearish, 2),
    "hikkake_bullish": (_detect_hikkake_bullish, 4),
    "hikkake_bearish": (_detect_hikkake_bearish, 4),
    "hikkake_modified_bullish": (_detect_hikkake_modified_bullish, 5),
    "hikkake_modified_bearish": (_detect_hikkake_modified_bearish, 5),
    "kicking_by_length_bullish": (_detect_kicking_by_length_bullish, 2),
    "kicking_by_length_bearish": (_detect_kicking_by_length_bearish, 2),
    "long_line_bullish": (_detect_long_line_bullish, 1),
    "long_line_bearish": (_detect_long_line_bearish, 1),
    "rise_fall_three_methods_bullish": (_detect_rise_fall_three_methods_bullish, 5),
    "rise_fall_three_methods_bearish": (_detect_rise_fall_three_methods_bearish, 5),
    "separating_lines_bullish": (_detect_separating_lines_bullish, 2),
    "separating_lines_bearish": (_detect_separating_lines_bearish, 2),
    "tasuki_gap_bullish": (_detect_tasuki_gap_bullish, 3),
    "tasuki_gap_bearish": (_detect_tasuki_gap_bearish, 3),
    "tri_star_bullish": (_detect_tri_star_bullish, 3),
    "tri_star_bearish": (_detect_tri_star_bearish, 3),
    "side_by_side_gap_three_methods_bullish": (_detect_side_by_side_gap_three_methods_bullish, 3),
    "side_by_side_gap_three_methods_bearish": (_detect_side_by_side_gap_three_methods_bearish, 3),
}

MAX_CANDLES_INVOLVED = max(n for _, n in _TALIB_DETECTORS.values())


def detect_at(df: pd.DataFrame, index: int) -> list[PatternHit]:
    """Every pattern that completes on `df.iloc[index]`. Passes the FULL
    `df` (not a truncated prefix) plus the absolute `index` to each
    detector -- correctness is unaffected (every detector only ever reads
    `.iloc[index - k]` for k >= 0, and never looks past `index`) but this
    lets `talib_thresholds.threshold()` cache its rolling-average series
    per (id(df), setting) across the whole scan instead of recomputing it
    from scratch at every position (was the dominant cost)."""
    hits = []
    for name, (detector, n_candles) in _TALIB_DETECTORS.items():
        if index < n_candles - 1:
            continue
        detail = detector(df, index)
        if detail is not None:
            hits.append(PatternHit(pattern=name, index=index, start_index=index - n_candles + 1, detail=detail))
    return hits


def scan(df: pd.DataFrame) -> list[PatternHit]:
    # threshold()'s cache is keyed by id(df), which Python can reuse for an
    # unrelated DataFrame once the original is garbage collected -- a real
    # risk here since callers (e.g. backtest.py) build and discard many
    # DataFrames across pairs/timeframes in a loop. Clearing at the start
    # of every scan() means the cache only ever holds entries for the df
    # this exact call is processing, so a stale id-collision can't leak a
    # wrong series in; it still gives the intended win (computed once per
    # scan() across O(n) positions, not once per position).
    talib_thresholds._CACHE.clear()
    hits: list[PatternHit] = []
    for i in range(len(df)):
        hits.extend(detect_at(df, i))
    return hits
