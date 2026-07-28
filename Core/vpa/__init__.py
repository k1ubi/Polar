"""VPA — Volume Price Analysis, per Anna Coulling's "A Complete Guide To
Volume Price Analysis" (KnowledgeBase/Trading).

Self-contained on pandas: takes an OHLCV DataFrame in (timestamp-indexed,
`open`/`high`/`low`/`close`/`volume`), no platform/exchange dependency.

See Doc/Core/vpa.md for the mechanism, the book-to-code mapping, and the
calibration status (nothing here has been backtested yet — this Core
implements the book faithfully; whether the book's method has an edge on
crypto is a separate, unanswered question).

    from Core.vpa import analyse, build_setups, analyse_multi_timeframe

    reads = analyse(df)                       # single timeframe
    setups = build_setups(df, reads)          # tradeable setups from the reads
    mtf = analyse_multi_timeframe(df, "15min", "1h")   # Ch11's 3-TF frame
"""
from __future__ import annotations

from Core.vpa.analysis import (
    MultiTimeframeRead,
    VpaRead,
    VpaSetup,
    analyse,
    analyse_multi_timeframe,
    build_contexts,
    build_setups,
    resample,
)
from Core.vpa.signals import VpaSignal
from Core.vpa.structure import CongestionZone, Level, Pivot, TrendState
from Core.vpa.vap import VapNode, VapProfile, build_profile

__all__ = [
    "analyse",
    "analyse_multi_timeframe",
    "build_contexts",
    "build_setups",
    "resample",
    "VpaRead",
    "VpaSetup",
    "MultiTimeframeRead",
    "VpaSignal",
    "CongestionZone",
    "Level",
    "Pivot",
    "TrendState",
    "VapNode",
    "VapProfile",
    "build_profile",
]
