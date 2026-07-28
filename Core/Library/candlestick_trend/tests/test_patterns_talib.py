"""Sanity checks for the TA-Lib CDL* pattern catalog (patterns_talib.py).

Not exhaustive per-pattern coverage (45 patterns) -- a smoke test across
all of them (no crashes, integrates with generate_signals) plus hand-built
synthetic fixtures for a representative handful, proving the shape is
actually detected and that dual-direction patterns don't also fire their
opposite direction.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Core.Library.candlestick_trend.backtest import fetch_ohlcv
from Core.Library.candlestick_trend.patterns_talib import _TALIB_DETECTORS, scan
from Core.Library.candlestick_trend.signal import generate_signals

BASELINE_BARS = 20  # comfortably above every ported pattern's lookback need


def _baseline(n: int = BASELINE_BARS, start: float = 100.0) -> pd.DataFrame:
    """Small, near-flat candles (range ~1) so the adaptive trailing-average
    thresholds settle at a small, predictable value before the fixture's
    real subject candle(s) are appended."""
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(7)
    noise = rng.uniform(-0.05, 0.05, n)
    opens = start + noise
    closes = start + 0.1 + noise
    highs = np.maximum(opens, closes) + 0.4
    lows = np.minimum(opens, closes) - 0.4
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0}, index=idx)


def _append(df: pd.DataFrame, o: float, h: float, l: float, c: float) -> pd.DataFrame:
    new_idx = df.index[-1] + pd.Timedelta(hours=1)
    row = pd.DataFrame({"open": [o], "high": [h], "low": [l], "close": [c], "volume": [1.0]}, index=[new_idx])
    return pd.concat([df, row])


def _hits_at_last_bar(df: pd.DataFrame) -> set[str]:
    last = len(df) - 1
    return {h.pattern for h in scan(df) if h.index == last}


def test_dragonfly_doji_detected():
    df = _append(_baseline(), o=100.0, h=100.05, l=95.0, c=100.05)
    assert "dragonfly_doji" in _hits_at_last_bar(df)


def test_gravestone_doji_detected():
    df = _append(_baseline(), o=100.0, h=105.0, l=100.0, c=100.05)
    assert "gravestone_doji" in _hits_at_last_bar(df)


def test_matching_low_detected():
    df = _baseline()
    df = _append(df, o=105.0, h=105.2, l=99.9, c=100.0)  # black
    df = _append(df, o=103.0, h=103.2, l=99.95, c=100.02)  # black, closes ~same level
    hits = _hits_at_last_bar(df)
    assert "matching_low" in hits


def test_belt_hold_bullish_detected():
    df = _append(_baseline(), o=100.0, h=110.2, l=100.0, c=110.0)
    hits = _hits_at_last_bar(df)
    assert "belt_hold_bullish" in hits
    assert "belt_hold_bearish" not in hits


def test_closing_marubozu_bullish_detected():
    df = _append(_baseline(), o=100.0, h=110.05, l=95.0, c=110.0)
    hits = _hits_at_last_bar(df)
    assert "closing_marubozu_bullish" in hits
    assert "closing_marubozu_bearish" not in hits


def test_abandoned_baby_bullish_detected_and_not_bearish():
    df = _baseline()
    df = _append(df, o=110.0, h=110.2, l=99.5, c=100.0)  # long black
    df = _append(df, o=95.0, h=95.2, l=94.8, c=95.05)  # doji, gaps down
    df = _append(df, o=96.0, h=105.0, l=95.9, c=104.0)  # long white, closes back into first body
    hits = _hits_at_last_bar(df)
    assert "abandoned_baby_bullish" in hits
    assert "abandoned_baby_bearish" not in hits


def test_scan_smoke_synthetic_flat_data_no_crash():
    idx = pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0}, index=idx)
    hits = scan(df)  # must not raise
    assert isinstance(hits, list)


def test_scan_smoke_real_data_no_crash_and_produces_hits():
    df = fetch_ohlcv("BTCUSDT", "1h", 300)
    hits = scan(df)
    assert isinstance(hits, list)
    assert len(hits) > 0, "expected at least one TA-Lib pattern hit across 300 real bars"
    assert {h.pattern for h in hits} <= set(_TALIB_DETECTORS)


def test_generate_signals_integrates_talib_patterns_without_crashing():
    df = fetch_ohlcv("BTCUSDT", "1h", 300)
    signals = generate_signals(df)  # must not raise, merges patterns.py + patterns_talib.py hits
    assert isinstance(signals, list)


def test_every_registered_pattern_has_metadata():
    from Core.Library.candlestick_trend.metadata import PATTERN_META

    missing = set(_TALIB_DETECTORS) - set(PATTERN_META)
    assert not missing, f"registered TA-Lib patterns missing PATTERN_META entries: {missing}"
