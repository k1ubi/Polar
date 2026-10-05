"""Turns pattern hits + trend context into trade signals.

The scoring formula is cm45t3r/candlestick's `contextFit.js`, ported as-is
(MIT license, https://github.com/cm45t3r/candlestick) — this graduated-
confidence layer doesn't exist in TrAIder_Old's implementation, which only
had a binary trend gate baked into a handful of patterns. Every pattern
gets it here, uniformly:

    expected_direction = 0                          if type == "neutral"
                        = -sign(direction)           if type == "reversal"
                        =  sign(direction)            if type == "continuation"

    context_fit = 1                                              if expected_direction == 0
                = 1                                              if measured_trend is unavailable (fail-safe: never penalize missing data)
                = clamp(0.5 + sensitivity * expected_direction * measured_trend, 0, 1)   otherwise

    effective_confidence = base_confidence * context_fit

A signal only fires when `effective_confidence` clears `confidence_threshold`.
This is what makes "trade a bearish reversal pattern even in a bullish
trend" fall out correctly: a bearish reversal pattern's expected_direction
is +1 (it expects to have been preceded by an uptrend); a strong measured
uptrend maximizes its context_fit, not suppresses it.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from Core.Library.candlestick_trend.metadata import PATTERN_META, PatternMeta
from Core.Library.candlestick_trend.patterns import PatternHit, scan as scan_core
from Core.Library.candlestick_trend.patterns_talib import scan as scan_talib
from Core.Library.candlestick_trend.trend import DEFAULT_PERIOD, compute_trend

# cm45t3r/candlestick's own default (5.0) assumes measured_trend magnitudes
# far larger than what sma-slope(10) actually produces on real hourly crypto
# bars: sanity-checking against 1000 hours of real BTC/USDT data showed
# |trend| rarely exceeds ~0.005 (90th percentile ~0.002), so sensitivity=5.0
# left context_fit within ~0.01 of neutral (0.5) for nearly every hit --
# effectively no gating signal at all, regardless of real trend direction.
# 150.0 was chosen empirically against that same sample (sweeping 50-300) as
# the point producing a believable signal frequency (~1/day) with a real
# confidence spread rather than either ~0 signals or saturating everything.
# This is coupled to trend_method="sma-slope" + trend_period=10 specifically
# -- changing either invalidates this tuning and it would need to be redone.
DEFAULT_SENSITIVITY = 150.0
# effective_confidence = base_confidence * context_fit, and context_fit maxes
# at 1.0 -- so effective_confidence can never exceed base_confidence. Several
# patterns have a base confidence below 0.5 (inverted_hammer=0.45,
# hanging_man=0.5), so a threshold above ~0.45 would make those patterns
# mathematically unreachable regardless of trend strength. 0.4 keeps every
# directional pattern reachable at a sufficiently strong trend-context fit.
DEFAULT_CONFIDENCE_THRESHOLD = 0.4


@dataclass(frozen=True)
class Signal:
    index: int  # position of the LAST candle involved in the match
    start_index: int  # position of the FIRST candle involved in the match
    pattern: str
    action: str  # "long" | "short" (neutral-direction patterns never reach the threshold meaningfully, see note below)
    type: str  # "reversal" | "continuation" | "neutral"
    base_confidence: float
    measured_trend: float | None
    context_fit: float
    effective_confidence: float
    detail: str


def expected_direction(meta: PatternMeta) -> int:
    if meta.type == "neutral" or meta.direction == "neutral":
        return 0
    signal_sign = 1 if meta.direction == "bullish" else -1
    if meta.type == "reversal":
        return -signal_sign
    if meta.type == "continuation":
        return signal_sign
    return 0


def context_fit(measured_trend: float | None, expected: int, sensitivity: float = DEFAULT_SENSITIVITY) -> float:
    if expected == 0:
        return 1.0
    if measured_trend is None or pd.isna(measured_trend):
        return 1.0
    alignment = expected * measured_trend
    return max(0.0, min(1.0, 0.5 + sensitivity * alignment))


def generate_signals(
    df: pd.DataFrame,
    trend_method: str = "sma-slope",
    trend_period: int = DEFAULT_PERIOD,
    sensitivity: float = DEFAULT_SENSITIVITY,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[Signal]:
    """Scans `df` for every pattern, grades each hit's confidence against
    the trend measured strictly before that pattern's own candles, and
    returns only the hits that clear `confidence_threshold`. Patterns with
    `direction == "neutral"` (Doji, Spinning Top) never produce a signal —
    they have nothing to act on, by design."""
    trend_series = compute_trend(df, method=trend_method, period=trend_period)
    # scan_core: cm45t3r-derived patterns (patterns.py); scan_talib: TA-Lib
    # CDL* catalog ported from pandas_ta_classic (patterns_talib.py). Same
    # PatternHit shape, same scoring below -- merged before grading.
    hits: list[PatternHit] = scan_core(df) + scan_talib(df)

    signals: list[Signal] = []
    for hit in hits:
        meta = PATTERN_META[hit.pattern]
        if meta.direction == "neutral":
            continue

        measured = trend_series.iloc[hit.start_index - 1] if hit.start_index > 0 else None
        expected = expected_direction(meta)
        fit = context_fit(measured, expected, sensitivity)
        effective_confidence = meta.confidence * fit

        if effective_confidence < confidence_threshold:
            continue

        signals.append(
            Signal(
                index=hit.index,
                start_index=hit.start_index,
                pattern=hit.pattern,
                action="long" if meta.direction == "bullish" else "short",
                type=meta.type,
                base_confidence=meta.confidence,
                measured_trend=None if measured is None or pd.isna(measured) else float(measured),
                context_fit=fit,
                effective_confidence=effective_confidence,
                detail=hit.detail,
            )
        )
    return signals
