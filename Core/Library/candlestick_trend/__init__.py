"""Candlestick pattern detection gated by trend context.

Self-contained on pandas: takes an OHLCV DataFrame (timestamp-indexed,
`open`/`high`/`low`/`close`/`volume` columns) and produces trend-graded
trade signals. No dependency on any platform/exchange/execution code.

See Doc/Core/Library/candlestick_trend.md for the mechanism and design rationale.
"""
from __future__ import annotations

from Core.Library.candlestick_trend.signal import Signal, generate_signals

__all__ = ["Signal", "generate_signals"]
