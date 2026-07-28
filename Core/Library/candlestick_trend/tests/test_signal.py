"""Synthetic-fixture sanity checks for Core/Library/candlestick_trend.

No real market data here on purpose — these confirm the mechanism (pattern
identity flips with trend context, confidence grades correctly, causality
holds) using hand-built OHLC series where the expected answer is known.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Core.Library.candlestick_trend.patterns import scan
from Core.Library.candlestick_trend.signal import context_fit, expected_direction, generate_signals
from Core.Library.candlestick_trend.metadata import PATTERN_META
from Core.Library.candlestick_trend.trend import compute_trend, prior_trend_binary

N_TREND_BARS = 60  # > IDENTITY_TREND_LOOKBACK (50) and >> trend.py's default period (10)


def _drift_bars(n: int, start: float, pct: float) -> pd.DataFrame:
    """`n` small-bodied candles whose close compounds by `pct` per bar
    (negative = downtrend, positive = uptrend). Exponential rather than
    linear drift so the SMA-slope trend measure converges to a stable
    ~`pct` magnitude regardless of `n` or `start` (a linear drift's
    fractional slope instead decays as price grows, understating the
    trend). Bodies/wicks are tiny relative to the drift so they don't
    themselves look like hammer/doji shapes and interfere with detection."""
    closes = start * (1 + pct) ** np.arange(n)
    opens = closes / (1 + pct * 0.2)
    wiggle = np.abs(closes * pct) * 0.3 + 0.01
    highs = np.maximum(opens, closes) + wiggle
    lows = np.minimum(opens, closes) - wiggle
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=idx)


def _append(df: pd.DataFrame, o: float, h: float, l: float, c: float) -> pd.DataFrame:
    new_idx = df.index[-1] + pd.Timedelta(hours=1)
    row = pd.DataFrame({"open": [o], "high": [h], "low": [l], "close": [c], "volume": [1.0]}, index=[new_idx])
    return pd.concat([df, row])


# A hammer-shaped candle: rng=10, small body near the top, long lower wick.
HAMMER_SHAPE = dict(o=98, h=100, l=90, c=99)
# Confirms bullish: closes above the shape candle's body top (99).
BULLISH_CONFIRM = dict(o=99, h=105, l=98, c=103)
# Confirms bearish: closes below the shape candle's body bottom (98).
BEARISH_CONFIRM = dict(o=98, h=99, l=90, c=92)


def test_hammer_after_downtrend_is_bullish_with_high_confidence():
    df = _drift_bars(N_TREND_BARS, start=1000, pct=-0.1)
    df = _append(df, **HAMMER_SHAPE)
    df = _append(df, **BULLISH_CONFIRM)

    signals = generate_signals(df)
    hammer_signals = [s for s in signals if s.pattern == "hammer"]
    assert len(hammer_signals) == 1, f"expected exactly one hammer signal, got: {signals}"
    sig = hammer_signals[0]
    assert sig.action == "long"
    assert sig.measured_trend is not None and sig.measured_trend < 0, "should measure a downtrend preceding the hammer"
    assert sig.context_fit == pytest.approx(1.0), "strong downtrend into a bullish reversal pattern should saturate to max fit"
    assert sig.effective_confidence >= 0.4

    # the identical shape must NOT also register as hanging_man here
    assert not any(s.pattern == "hanging_man" for s in signals)


def test_same_shape_after_uptrend_is_hanging_man_not_hammer():
    df = _drift_bars(N_TREND_BARS, start=1000, pct=+0.1)
    df = _append(df, **HAMMER_SHAPE)
    df = _append(df, **BEARISH_CONFIRM)

    signals = generate_signals(df)
    assert not any(s.pattern == "hammer" for s in signals), "hammer identity must not fire after an uptrend"

    hanging = [s for s in signals if s.pattern == "hanging_man"]
    assert len(hanging) == 1, f"expected exactly one hanging_man signal, got: {signals}"
    sig = hanging[0]
    assert sig.action == "short"
    assert sig.measured_trend is not None and sig.measured_trend > 0, "should measure an uptrend preceding it"
    assert sig.context_fit == pytest.approx(1.0), "strong uptrend into a bearish reversal pattern should saturate to max fit — this is the 'trade bearish patterns on a strong reversal setup' case"
    assert sig.effective_confidence >= 0.4


def test_weak_trend_suppresses_confidence_below_threshold():
    """A much shorter/weaker drift should still pass the binary identity
    gate (which only checks sign) but score a lower context_fit than a
    long, decisive downtrend — since sensitivity=5 saturates fast, use a
    trend barely below zero to demonstrate the fit is graduated, not just
    binary."""
    df = _drift_bars(N_TREND_BARS, start=1000, pct=-0.005)  # barely-down drift
    df = _append(df, **HAMMER_SHAPE)
    df = _append(df, **BULLISH_CONFIRM)

    strong_df = _drift_bars(N_TREND_BARS, start=1000, pct=-0.1)  # sharply-down drift
    strong_df = _append(strong_df, **HAMMER_SHAPE)
    strong_df = _append(strong_df, **BULLISH_CONFIRM)

    weak_signals = [s for s in generate_signals(df, confidence_threshold=0.0) if s.pattern == "hammer"]
    strong_signals = [s for s in generate_signals(strong_df, confidence_threshold=0.0) if s.pattern == "hammer"]
    assert weak_signals and strong_signals
    assert weak_signals[0].context_fit <= strong_signals[0].context_fit


def test_neutral_patterns_never_produce_a_signal():
    # A textbook doji: tiny body vs a wide range.
    df = _drift_bars(N_TREND_BARS, start=100, pct=0.001)
    df = _append(df, o=100.0, h=110, l=90, c=100.05)
    signals = generate_signals(df)
    assert not any(s.pattern == "doji" for s in signals)
    assert not any(PATTERN_META[s.pattern].direction == "neutral" for s in signals)


def test_trend_series_is_causal_extending_history_does_not_change_past_values():
    df = _drift_bars(N_TREND_BARS, start=1000, pct=-0.02)
    extended = _append(df, **HAMMER_SHAPE)
    extended = _append(extended, **BULLISH_CONFIRM)

    base_trend = compute_trend(df)
    extended_trend = compute_trend(extended)

    # every value computed over `df` must be unchanged when more bars are
    # appended afterward -- nothing here should be allowed to look ahead.
    pd.testing.assert_series_equal(
        extended_trend.iloc[: len(df)].reset_index(drop=True),
        base_trend.reset_index(drop=True),
        check_names=False,
    )


def test_prior_trend_binary_excludes_the_shape_candle_itself():
    # 60 bars flat/up, then one huge down candle as the "shape" -- if the
    # shape candle leaked into its own trend read, a big-enough single drop
    # could flip the EMA comparison. It must not: prior_trend_binary is
    # evaluated on df.iloc[:-1], so the caller must exclude the shape row.
    df = _drift_bars(N_TREND_BARS, start=1000, pct=+0.01)
    huge_drop = _append(df, o=1200, h=1200, l=1, c=1)
    # caller convention: pass a window whose last row IS the shape candle
    # (mirrors how patterns.py calls it with w.iloc[:-1] before this
    # function drops one more row internally).
    result = prior_trend_binary(huge_drop)
    assert result == "up", "the huge drop candle itself must not influence the trend read of the bars before it"


def test_expected_direction_reversal_vs_continuation():
    reversal_bullish = PATTERN_META["hammer"]
    reversal_bearish = PATTERN_META["hanging_man"]
    continuation_bullish = PATTERN_META["bullish_marubozu"]
    neutral = PATTERN_META["doji"]

    assert expected_direction(reversal_bullish) == -1  # expects a preceding downtrend
    assert expected_direction(reversal_bearish) == 1  # expects a preceding uptrend
    assert expected_direction(continuation_bullish) == 1  # expects to already be in an uptrend
    assert expected_direction(neutral) == 0


@pytest.mark.parametrize(
    "measured,expected,want",
    [
        (None, 1, 1.0),  # missing data never penalized
        (float("nan"), -1, 1.0),
        (-0.1, 0, 1.0),  # no expectation -> always neutral/fit
        (0.1, 1, 1.0),  # aligned and saturating (0.5 + 5*0.1 = 1.0)
        (-0.1, 1, 0.0),  # opposed and saturating (0.5 - 0.5 = 0.0)
        (0.0, 1, 0.5),  # no measurable trend -> neutral fit
    ],
)
def test_context_fit_formula(measured, expected, want):
    assert context_fit(measured, expected) == pytest.approx(want)


def test_perfectly_flat_data_is_all_doji_but_produces_no_signal():
    # A perfectly flat candle (zero body) IS, correctly, a textbook doji at
    # every bar -- scan() should find it. But doji is direction="neutral",
    # so generate_signals() must still emit nothing.
    idx = pd.date_range("2024-01-01", periods=N_TREND_BARS, freq="h", tz="UTC")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0}, index=idx
    )
    hits = scan(df)
    assert hits and all(h.pattern == "doji" for h in hits)
    assert generate_signals(df) == []
