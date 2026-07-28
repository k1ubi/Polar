"""The graded volume vocabulary — VPA's foundational measurement.

Coulling's method never uses an absolute volume number. Every reading is
RELATIVE: "is this bar's volume high *for this instrument, lately*?" She
grades it with a qualitative ladder (low / average / above average / high /
very high) and never supplies numeric cutoffs — the grading is done by eye
against the recent bars visible on the chart.

This module makes that ladder mechanical the only way that preserves its
meaning: as a PERCENTILE RANK against a trailing window of the same
instrument's own volume. A fixed multiple of a moving average would not
survive the regime shifts crypto routinely produces (a "2x average" bar
means something completely different in a dead weekend than in a listing
pump); a percentile self-normalizes to whatever the instrument has actually
been doing.

Cutoffs are declared here as named constants, not scattered as magic
numbers, because they are the single most consequential unvalidated choice
in this Core -- see Doc/Core/vpa.md's "Calibration status" section. The
prior Polar codebase used the same percentile approach with 25/60/90
cutoffs and marked them explicitly as reasoned-but-unvalidated defaults;
those are carried forward here, plus a fifth tier, so this module's
vocabulary matches the book's five-rung ladder rather than the prior
four-rung compression of it.

Causality: `grade_series` computes each bar's rank against bars STRICTLY
BEFORE it. A bar never participates in the window it is judged against --
the same discipline `trend.py` and `structure.py` hold, and the property
`tests/test_causality.py` checks mechanically.
"""
from __future__ import annotations

from enum import IntEnum

import pandas as pd

DEFAULT_LOOKBACK = 100  # bars of trailing history a grade is measured against


class VolumeGrade(IntEnum):
    """The book's ladder. IntEnum so callers can compare ordinally
    (`grade >= VolumeGrade.HIGH`) — that comparison is used constantly in
    signals.py, and an ordinary Enum would force a lookup table for it."""

    LOW = 0
    AVERAGE = 1
    ABOVE_AVERAGE = 2
    HIGH = 3
    VERY_HIGH = 4


# Percentile cutoffs (inclusive lower bound of each grade). Reasoned
# defaults, NOT derived from the book (which gives no numbers) and NOT yet
# validated against outcomes.
CUTOFF_AVERAGE = 25.0
CUTOFF_ABOVE_AVERAGE = 60.0
CUTOFF_HIGH = 85.0
CUTOFF_VERY_HIGH = 95.0


def grade_from_percentile(pctl: float) -> VolumeGrade:
    if pctl >= CUTOFF_VERY_HIGH:
        return VolumeGrade.VERY_HIGH
    if pctl >= CUTOFF_HIGH:
        return VolumeGrade.HIGH
    if pctl >= CUTOFF_ABOVE_AVERAGE:
        return VolumeGrade.ABOVE_AVERAGE
    if pctl >= CUTOFF_AVERAGE:
        return VolumeGrade.AVERAGE
    return VolumeGrade.LOW


def percentile_series(volume: pd.Series, lookback: int = DEFAULT_LOOKBACK) -> pd.Series:
    """Each bar's volume percentile rank (0-100) within the `lookback` bars
    immediately BEFORE it. NaN until there is a full window of prior bars —
    deliberately not a partial-window estimate, because an early bar ranked
    against 6 predecessors produces a confident-looking number off almost no
    information, which is worse than declining to answer."""
    # rank of the current value among the prior window, expressed 0-100.
    # `closed="left"` is what makes it strictly-prior: the window ends at
    # the bar before this one.
    def _rank(window: pd.Series) -> float:
        current = window.iloc[-1]
        prior = window.iloc[:-1]
        return float((prior < current).sum()) / len(prior) * 100.0

    return volume.rolling(lookback + 1).apply(_rank, raw=False)


def grade_series(volume: pd.Series, lookback: int = DEFAULT_LOOKBACK) -> pd.Series:
    """VolumeGrade per bar (dtype object; NaN where history is insufficient)."""
    pctl = percentile_series(volume, lookback=lookback)
    return pctl.map(lambda p: grade_from_percentile(p) if pd.notna(p) else None)


def is_rising(volume: pd.Series, bars: int = 3) -> bool:
    """Strictly rising volume across the last `bars` — the signature of a
    genuine push (Ch4's "effort"), as opposed to a move drifting on fading
    participation."""
    if len(volume) < bars:
        return False
    tail = volume.iloc[-bars:]
    return bool(all(tail.iloc[i] < tail.iloc[i + 1] for i in range(len(tail) - 1)))


def is_falling(volume: pd.Series, bars: int = 3) -> bool:
    if len(volume) < bars:
        return False
    tail = volume.iloc[-bars:]
    return bool(all(tail.iloc[i] > tail.iloc[i + 1] for i in range(len(tail) - 1)))
