"""Static per-pattern metadata: type, direction, base confidence.

`direction` is the pattern's own signal direction. `type` is `"reversal"`
(expects the OPPOSITE trend to have preceded it — that's what makes it a
reversal), `"continuation"` (expects the SAME trend to have preceded it), or
`"neutral"` (no trend expectation — indecision patterns like Doji/Spinning
Top, which score a neutral context_fit regardless of trend, see signal.py).

Base `confidence` values are carried over as-is from TrAIder_Old's
polar/patterns/candlesticks.py (calibration-tuned there on live trade
outcomes). `type` classification follows standard Nison candlestick theory
(the source cm45t3r/candlestick's own type table wasn't fully captured
during research) — most classic single/multi-candle patterns are reversal
patterns; Marubozu is the one clear continuation case (a strong-momentum
candle means more when it confirms an already-established trend); Doji and
Spinning Top are indecision/neutral.

The entries below the original 23 are the TA-Lib CDL* catalog ported in
patterns_talib.py (from pandas_ta_classic, see that module's docstring).
None of these have base confidence values from any calibration -- they're
assigned by the same standard-theory judgment call as the original 23,
scaled by how strict/rare the setup is (stricter multi-candle setups like
breakaway/mat_hold/ladder_bottom get the higher end of the range; single-
candle and common 2-candle setups sit lower). A few classification notes:
  - closing_marubozu and long_line are continuation, same reasoning as
    Marubozu above -- a strong-momentum candle confirms rather than
    reverses an existing trend.
  - high_wave, long_legged_doji, rickshaw_man, short_line are neutral,
    same reasoning as Doji/Spinning Top -- small body and/or
    directionless shadows, no forecast either way, despite TA-Lib's own
    raw output tagging some of these with the candle's own color.
  - gap_side_by_side_white, rise_fall_three_methods, separating_lines,
    tasuki_gap, side_by_side_gap_three_methods are continuation --
    textbook "pause within a trend" patterns, not reversals.
  - Everything else (three_inside/outside, doji_star, harami_cross,
    belt_hold, counterattack, kicking_by_length, hikkake(_modified),
    breakaway, abandoned_baby, tri_star, three_line_strike, and the
    unidirectional ones: two_crows, three_stars_in_south, advance_block,
    stalled_pattern, identical_three_crows, upside_gap_two_crows,
    homing_pigeon, matching_low, stick_sandwich, takuri, dragonfly/
    gravestone_doji, morning/evening_doji_star, in_neck/on_neck/
    thrusting, unique_three_river, conceal_baby_swallow, ladder_bottom)
    are reversal, per standard theory.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternMeta:
    type: str  # "reversal" | "continuation" | "neutral"
    direction: str  # "bullish" | "bearish" | "neutral"
    confidence: float  # base confidence, 0-1, before trend-context grading


PATTERN_META: dict[str, PatternMeta] = {
    "doji": PatternMeta("neutral", "neutral", 0.50),
    "hammer": PatternMeta("reversal", "bullish", 0.55),
    "hanging_man": PatternMeta("reversal", "bearish", 0.50),
    "inverted_hammer": PatternMeta("reversal", "bullish", 0.45),
    "shooting_star": PatternMeta("reversal", "bearish", 0.55),
    "bullish_marubozu": PatternMeta("continuation", "bullish", 0.60),
    "bearish_marubozu": PatternMeta("continuation", "bearish", 0.60),
    "bullish_spinning_top": PatternMeta("neutral", "neutral", 0.40),
    "bearish_spinning_top": PatternMeta("neutral", "neutral", 0.40),
    "bullish_engulfing": PatternMeta("reversal", "bullish", 0.60),
    "bearish_engulfing": PatternMeta("reversal", "bearish", 0.60),
    "bullish_harami": PatternMeta("reversal", "bullish", 0.50),
    "bearish_harami": PatternMeta("reversal", "bearish", 0.50),
    "piercing_line": PatternMeta("reversal", "bullish", 0.55),
    "dark_cloud_cover": PatternMeta("reversal", "bearish", 0.55),
    "tweezers_top": PatternMeta("reversal", "bearish", 0.50),
    "tweezers_bottom": PatternMeta("reversal", "bullish", 0.50),
    "bullish_kicker": PatternMeta("reversal", "bullish", 0.60),
    "bearish_kicker": PatternMeta("reversal", "bearish", 0.60),
    "morning_star": PatternMeta("reversal", "bullish", 0.65),
    "evening_star": PatternMeta("reversal", "bearish", 0.65),
    "three_white_soldiers": PatternMeta("reversal", "bullish", 0.65),
    "three_black_crows": PatternMeta("reversal", "bearish", 0.65),

    # --- TA-Lib CDL* catalog (patterns_talib.py, via pandas_ta_classic) ---
    "two_crows": PatternMeta("reversal", "bearish", 0.50),
    "three_stars_in_south": PatternMeta("reversal", "bullish", 0.55),
    "advance_block": PatternMeta("reversal", "bearish", 0.55),
    "conceal_baby_swallow": PatternMeta("reversal", "bullish", 0.60),
    "evening_doji_star": PatternMeta("reversal", "bearish", 0.65),
    "identical_three_crows": PatternMeta("reversal", "bearish", 0.60),
    "ladder_bottom": PatternMeta("reversal", "bullish", 0.60),
    "matching_low": PatternMeta("reversal", "bullish", 0.45),
    "mat_hold": PatternMeta("continuation", "bullish", 0.65),
    "morning_doji_star": PatternMeta("reversal", "bullish", 0.65),
    "in_neck": PatternMeta("continuation", "bearish", 0.45),
    "on_neck": PatternMeta("continuation", "bearish", 0.45),
    "stalled_pattern": PatternMeta("reversal", "bearish", 0.55),
    "stick_sandwich": PatternMeta("reversal", "bullish", 0.50),
    "takuri": PatternMeta("reversal", "bullish", 0.45),
    "thrusting": PatternMeta("continuation", "bearish", 0.45),
    "unique_three_river": PatternMeta("reversal", "bullish", 0.55),
    "upside_gap_two_crows": PatternMeta("reversal", "bearish", 0.55),
    "homing_pigeon": PatternMeta("reversal", "bullish", 0.50),
    "dragonfly_doji": PatternMeta("reversal", "bullish", 0.50),
    "gravestone_doji": PatternMeta("reversal", "bearish", 0.50),

    "high_wave": PatternMeta("neutral", "neutral", 0.35),
    "long_legged_doji": PatternMeta("neutral", "neutral", 0.40),
    "rickshaw_man": PatternMeta("neutral", "neutral", 0.35),
    "short_line": PatternMeta("neutral", "neutral", 0.35),

    "three_inside_bullish": PatternMeta("reversal", "bullish", 0.55),
    "three_inside_bearish": PatternMeta("reversal", "bearish", 0.55),
    "three_outside_bullish": PatternMeta("reversal", "bullish", 0.55),
    "three_outside_bearish": PatternMeta("reversal", "bearish", 0.55),
    "three_line_strike_bullish": PatternMeta("reversal", "bullish", 0.60),
    "three_line_strike_bearish": PatternMeta("reversal", "bearish", 0.60),
    "abandoned_baby_bullish": PatternMeta("reversal", "bullish", 0.65),
    "abandoned_baby_bearish": PatternMeta("reversal", "bearish", 0.65),
    "belt_hold_bullish": PatternMeta("reversal", "bullish", 0.45),
    "belt_hold_bearish": PatternMeta("reversal", "bearish", 0.45),
    "breakaway_bullish": PatternMeta("reversal", "bullish", 0.60),
    "breakaway_bearish": PatternMeta("reversal", "bearish", 0.60),
    "closing_marubozu_bullish": PatternMeta("continuation", "bullish", 0.55),
    "closing_marubozu_bearish": PatternMeta("continuation", "bearish", 0.55),
    "counterattack_bullish": PatternMeta("reversal", "bullish", 0.50),
    "counterattack_bearish": PatternMeta("reversal", "bearish", 0.50),
    "doji_star_bullish": PatternMeta("reversal", "bullish", 0.50),
    "doji_star_bearish": PatternMeta("reversal", "bearish", 0.50),
    "gap_side_by_side_white_bullish": PatternMeta("continuation", "bullish", 0.50),
    "gap_side_by_side_white_bearish": PatternMeta("continuation", "bearish", 0.50),
    "harami_cross_bullish": PatternMeta("reversal", "bullish", 0.50),
    "harami_cross_bearish": PatternMeta("reversal", "bearish", 0.50),
    "hikkake_bullish": PatternMeta("reversal", "bullish", 0.50),
    "hikkake_bearish": PatternMeta("reversal", "bearish", 0.50),
    "hikkake_modified_bullish": PatternMeta("reversal", "bullish", 0.50),
    "hikkake_modified_bearish": PatternMeta("reversal", "bearish", 0.50),
    "kicking_by_length_bullish": PatternMeta("reversal", "bullish", 0.60),
    "kicking_by_length_bearish": PatternMeta("reversal", "bearish", 0.60),
    "long_line_bullish": PatternMeta("continuation", "bullish", 0.55),
    "long_line_bearish": PatternMeta("continuation", "bearish", 0.55),
    "rise_fall_three_methods_bullish": PatternMeta("continuation", "bullish", 0.65),
    "rise_fall_three_methods_bearish": PatternMeta("continuation", "bearish", 0.65),
    "separating_lines_bullish": PatternMeta("continuation", "bullish", 0.50),
    "separating_lines_bearish": PatternMeta("continuation", "bearish", 0.50),
    "tasuki_gap_bullish": PatternMeta("continuation", "bullish", 0.50),
    "tasuki_gap_bearish": PatternMeta("continuation", "bearish", 0.50),
    "tri_star_bullish": PatternMeta("reversal", "bullish", 0.55),
    "tri_star_bearish": PatternMeta("reversal", "bearish", 0.55),
    "side_by_side_gap_three_methods_bullish": PatternMeta("continuation", "bullish", 0.50),
    "side_by_side_gap_three_methods_bearish": PatternMeta("continuation", "bearish", 0.50),
}
