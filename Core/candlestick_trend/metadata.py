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
}
