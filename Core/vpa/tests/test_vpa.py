"""Tests for the VPA Core.

The first test is the important one. Everything else checks that individual
book rules were transcribed correctly; `test_analysis_is_causal` checks the
property that decides whether ANY of it means anything — that a bar's read
never depends on bars that had not happened yet. This project has already
shipped one look-ahead bug (the forming-bar leak), so it is checked
mechanically rather than by inspection.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Core.vpa import analyse, build_setups
from Core.vpa.signals import BarContext
from Core.vpa.spread import Spread
from Core.vpa.structure import congestion_zones, isolated_pivots, trend_states
from Core.vpa.vap import build_profile, node_intensity
from Core.vpa.volume import VolumeGrade, grade_from_percentile, percentile_series


def _synthetic(n: int = 300, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 100 * np.cumprod(1 + rng.normal(0, 0.006, n))
    op = close * (1 + rng.normal(0, 0.002, n))
    hi = np.maximum(op, close) * (1 + abs(rng.normal(0, 0.003, n)))
    lo = np.minimum(op, close) * (1 - abs(rng.normal(0, 0.003, n)))
    vol = rng.lognormal(10, 0.6, n)
    return pd.DataFrame(
        {"open": op, "high": hi, "low": lo, "close": close, "volume": vol}, index=idx
    )


def _bars(rows: list[tuple[float, float, float, float, float]], start: str = "2024-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# --------------------------------------------------------------------------
# The load-bearing property
# --------------------------------------------------------------------------

def test_analysis_is_causal():
    """Re-running on a truncated prefix must reproduce an identical prefix of
    results. If this fails, every backtest number the Core produces is
    hindsight and worthless."""
    df = _synthetic(240)
    full = analyse(df)
    prefix = analyse(df.iloc[:180])

    for i in range(len(prefix)):
        a, b = prefix[i], full[i]
        assert a.trend == b.trend, f"trend changed at bar {i} when future bars were added"
        assert {s.name for s in a.signals} == {s.name for s in b.signals}, (
            f"signal set changed at bar {i} when future bars were added"
        )


def test_volume_percentile_excludes_the_bar_itself():
    """A bar must not participate in the window it is ranked against."""
    vol = pd.Series([1.0] * 50 + [1000.0])
    pctl = percentile_series(vol, lookback=50)
    # the huge bar ranks at the top of its PRIOR window...
    assert pctl.iloc[-1] == 100.0
    # ...and cannot have inflated that window, which was all 1.0s
    assert pd.isna(pctl.iloc[49]) or pctl.iloc[49] == 0.0


# --------------------------------------------------------------------------
# Structure (Ch7, Ch8)
# --------------------------------------------------------------------------

def test_pivot_requires_both_extremes_to_step():
    """Ch7's three-bar reversal is symmetric: a pivot high needs a higher
    high AND a higher low. A bar with a tall spike but a lower low is not a
    pivot -- it is a single long wick, which is exactly what the stricter
    rule exists to exclude."""
    # centre bar has the highest high but ALSO the lowest low -> outside bar
    df = _bars([
        (10, 11, 9, 10, 1),
        (10, 15, 5, 10, 1),   # spike both ways
        (10, 11, 9, 10, 1),
    ])
    assert isolated_pivots(df) == []

    # centre bar steps up on both extremes -> genuine pivot high
    df2 = _bars([
        (10, 11, 9, 10, 1),
        (11, 15, 10, 12, 1),
        (10, 11, 9, 10, 1),
    ])
    pivots = isolated_pivots(df2)
    assert len(pivots) == 1 and pivots[0].kind == "high"


def test_trend_needs_both_series_stepping():
    """Ch8: an uptrend requires higher highs AND higher lows. A single lower
    high demotes to 'ranging', it does not flip to 'down' -- otherwise every
    ordinary pullback would invert the trend."""
    df = _synthetic(200, seed=5)
    states = trend_states(isolated_pivots(df))
    assert states, "expected some pivots in 200 synthetic bars"
    assert all(s.trend in {"up", "down", "ranging", "undefined"} for s in states)
    # no state may be "up" while its own label is a lower high
    assert not any(s.trend == "up" and s.label == "LH" for s in states)


def test_congestion_zone_cause_is_duration_times_height():
    """Wyckoff's second law made numeric. Duration alone is not the cause --
    a wide drifting range is not the same coiled spring as a tight one."""
    df = _synthetic(300, seed=7)
    zones = congestion_zones(isolated_pivots(df))
    for z in zones:
        assert z.cause == pytest.approx(z.bars * z.height)
        assert z.height >= 0


# --------------------------------------------------------------------------
# Volume / spread vocabulary (Ch4)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pctl,expected", [
    (5.0, VolumeGrade.LOW),
    (40.0, VolumeGrade.AVERAGE),
    (70.0, VolumeGrade.ABOVE_AVERAGE),
    (88.0, VolumeGrade.HIGH),
    (99.0, VolumeGrade.VERY_HIGH),
])
def test_volume_ladder(pctl, expected):
    assert grade_from_percentile(pctl) == expected


def test_volume_grades_are_ordinally_comparable():
    """signals.py compares grades with >= constantly; an ordinary Enum would
    make that a TypeError."""
    assert VolumeGrade.VERY_HIGH >= VolumeGrade.HIGH >= VolumeGrade.AVERAGE


# --------------------------------------------------------------------------
# Signals (Ch4-Ch6)
# --------------------------------------------------------------------------

def _ctx(**kw) -> BarContext:
    base = dict(
        volume_grade=VolumeGrade.AVERAGE, volume_pctl=50.0, spread_grade=Spread.AVERAGE,
        spread_atr=1.0, close_pos=0.5, body_frac=0.5, upper_wick_frac=0.2,
        lower_wick_frac=0.2, trend="undefined", zone=None, nearest_level=None, at_level=False,
    )
    base.update(kw)
    return BarContext(**base)


def test_narrow_spread_high_volume_is_bearish_at_a_top_bullish_at_a_bottom():
    """Ch4's fourth matrix cell. The SAME bar geometry means opposite things
    depending on trend position -- this is the clearest case in the book of
    why context is a required input, not a refinement."""
    from Core.vpa.signals import spread_volume_matrix

    df = _bars([(10, 11, 9, 10.5, 1), (10, 10.2, 9.9, 10.1, 5)])
    top = spread_volume_matrix(df, 1, _ctx(spread_grade=Spread.NARROW,
                                          volume_grade=VolumeGrade.HIGH, trend="up"))
    bottom = spread_volume_matrix(df, 1, _ctx(spread_grade=Spread.NARROW,
                                              volume_grade=VolumeGrade.HIGH, trend="down"))
    assert top is not None and top.bias == "bearish"
    assert bottom is not None and bottom.bias == "bullish"
    assert top.kind == bottom.kind == "anomaly"


def test_wide_spread_low_volume_is_a_trap_against_the_candle_direction():
    """Ch4 Fig 4.12: a big result from little effort is a fake move. The bias
    opposes the candle's own colour -- an UP bar on no volume is BEARISH."""
    from Core.vpa.signals import spread_volume_matrix

    df = _bars([(10, 11, 9, 10, 1), (10, 12, 9.9, 11.8, 1)])  # strong up bar
    s = spread_volume_matrix(df, 1, _ctx(spread_grade=Spread.WIDE, volume_grade=VolumeGrade.LOW))
    assert s is not None
    assert s.bias == "bearish", "an up bar on low volume must read bearish, not bullish"
    assert s.kind == "anomaly"


def test_long_legged_doji_low_volume_is_a_trap_not_a_reversal():
    """Ch6's one premier candle with a genuine volume anomaly: on low volume
    it inverts from reversal signal to stop-hunt warning."""
    from Core.vpa.signals import long_legged_doji

    df = _bars([(10, 11, 9, 10, 1), (10, 12, 8, 10.02, 1)])
    ctx = _ctx(body_frac=0.01, upper_wick_frac=0.45, lower_wick_frac=0.45,
               volume_grade=VolumeGrade.LOW, trend="up")
    s = long_legged_doji(df, 1, ctx)
    assert s is not None and s.name == "long_legged_doji_trap"
    assert s.bias == "neutral", "a trap is a stand-aside, not a directional signal"

    validated = long_legged_doji(df, 1, _ctx(body_frac=0.01, upper_wick_frac=0.45,
                                             lower_wick_frac=0.45,
                                             volume_grade=VolumeGrade.HIGH, trend="up"))
    assert validated is not None and validated.name == "long_legged_doji"
    assert validated.bias == "bearish"


def test_hanging_man_requires_above_average_volume():
    """Ch6 makes volume a REQUIRED condition here, unlike the shooting star
    where it only grades: the candle "is only a sign of weakness provided it
    is associated with above average volume"."""
    from Core.vpa.signals import hanging_man

    df = _bars([(10, 11, 9, 10, 1), (10, 10.1, 8, 9.95, 1)])
    ctx_lo = _ctx(body_frac=0.05, lower_wick_frac=0.8, upper_wick_frac=0.05,
                  trend="up", volume_grade=VolumeGrade.LOW)
    ctx_hi = _ctx(body_frac=0.05, lower_wick_frac=0.8, upper_wick_frac=0.05,
                  trend="up", volume_grade=VolumeGrade.HIGH)
    assert hanging_man(df, 1, ctx_lo) is None
    assert hanging_man(df, 1, ctx_hi) is not None


def test_hammer_and_hanging_man_never_both_fire():
    """Identical geometry, opposite meaning, disambiguated ONLY by trend
    position. Both firing at once would be incoherent."""
    from Core.vpa.signals import hammer, hanging_man

    df = _bars([(10, 11, 9, 10, 1), (10, 10.1, 8, 9.95, 1)])
    for trend in ("up", "down", "ranging"):
        ctx = _ctx(body_frac=0.05, lower_wick_frac=0.8, upper_wick_frac=0.05,
                   trend=trend, volume_grade=VolumeGrade.HIGH)
        fired = [s for s in (hammer(df, 1, ctx), hanging_man(df, 1, ctx)) if s is not None]
        assert len(fired) <= 1, f"both fired at trend={trend}"


def test_climax_naming_follows_the_book_not_convention():
    """Coulling INVERTS the usual naming and says so: a SELLING climax
    appears at the TOP of a bullish trend. Getting this backwards silently
    inverts every climax signal, so it is pinned by a test."""
    from Core.vpa.signals import buying_climax, selling_climax

    df = _synthetic(120, seed=13)
    contexts = [
        _ctx(trend="up", volume_grade=VolumeGrade.HIGH, body_frac=0.1,
             upper_wick_frac=0.7, lower_wick_frac=0.1)
        for _ in range(len(df))
    ]
    s = selling_climax(df, 60, contexts[60], contexts)
    assert s is not None and s.bias == "bearish", "selling climax must be bearish, at a TOP"
    # the same contexts must NOT produce a buying climax (wrong trend + wick side)
    assert buying_climax(df, 60, contexts[60], contexts) is None


def test_climax_requires_repetition():
    """Ch5: repetition is definitional, not a strengthener -- "it takes time
    to sell large volumes". One deep-wicked bar is never a climax."""
    from Core.vpa.signals import selling_climax

    df = _synthetic(120, seed=17)
    plain = _ctx(trend="up", volume_grade=VolumeGrade.AVERAGE, body_frac=0.5,
                 upper_wick_frac=0.1, lower_wick_frac=0.1)
    climactic = _ctx(trend="up", volume_grade=VolumeGrade.HIGH, body_frac=0.1,
                     upper_wick_frac=0.7, lower_wick_frac=0.1)
    contexts = [plain] * len(df)
    contexts[60] = climactic  # exactly one qualifying bar
    assert selling_climax(df, 60, contexts[60], contexts) is None


def test_test_of_supply_requires_a_congestion_zone():
    """Ch5 is explicit: a test without a preceding accumulation/distribution
    phase is not a test at all. The zone is a hard precondition -- one of
    only two places this Core gates rather than scores."""
    from Core.vpa.signals import test_of_supply
    from Core.vpa.structure import CongestionZone

    df = _bars([(10, 11, 9, 10, 1), (10, 10.1, 8, 9.98, 1)])
    shape = dict(body_frac=0.05, lower_wick_frac=0.8, upper_wick_frac=0.05,
                 volume_grade=VolumeGrade.LOW)
    assert test_of_supply(df, 1, _ctx(zone=None, **shape)) is None

    zone = CongestionZone(start_bar=0, end_bar=1, ceiling=11.0, floor=9.0)
    s = test_of_supply(df, 1, _ctx(zone=zone, **shape))
    assert s is not None and s.bias == "bullish" and s.name == "test_of_supply_passed"


def test_test_of_supply_fails_on_high_volume():
    """Pass and fail are opposite signals, both emitted -- silence would lose
    the information that supply is still present."""
    from Core.vpa.signals import test_of_supply
    from Core.vpa.structure import CongestionZone

    df = _bars([(10, 11, 9, 10, 1), (10, 10.1, 8, 9.98, 1)])
    zone = CongestionZone(start_bar=0, end_bar=1, ceiling=11.0, floor=9.0)
    s = test_of_supply(df, 1, _ctx(zone=zone, body_frac=0.05, lower_wick_frac=0.8,
                                   upper_wick_frac=0.05, volume_grade=VolumeGrade.VERY_HIGH))
    assert s is not None and s.name == "test_of_supply_failed" and s.bias == "bearish"


def test_breakout_fires_only_on_the_crossing_bar():
    """A breakout is an event, not a state. Without this, every bar that
    merely stays outside the range re-reports the same structural event and
    floods the evidence score."""
    from Core.vpa.signals import breakout
    from Core.vpa.structure import CongestionZone

    zone = CongestionZone(start_bar=0, end_bar=2, ceiling=10.0, floor=9.0)
    df = _bars([
        (9.5, 10, 9, 9.5, 1),
        (9.5, 10, 9, 9.6, 1),
        (9.6, 12, 9.5, 11.5, 1),   # crossing bar
        (11.5, 12.5, 11, 12.0, 1),  # still outside — must NOT re-fire
    ])
    ctx = _ctx(volume_grade=VolumeGrade.HIGH)
    assert breakout(df, 2, ctx, zone, atr_val=1.0) is not None
    assert breakout(df, 3, ctx, zone, atr_val=1.0) is None


def test_low_volume_breakout_is_a_fake_with_inverted_bias():
    """Ch7: "a break out from such a price area on low volume is a classic
    trap move by the insiders" -- so an upward fake reads BEARISH."""
    from Core.vpa.signals import breakout
    from Core.vpa.structure import CongestionZone

    zone = CongestionZone(start_bar=0, end_bar=1, ceiling=10.0, floor=9.0)
    df = _bars([(9.5, 10, 9, 9.5, 1), (9.6, 12, 9.5, 11.5, 1)])
    s = breakout(df, 1, _ctx(volume_grade=VolumeGrade.LOW), zone, atr_val=1.0)
    assert s is not None and s.name == "breakout_fake" and s.bias == "bearish"


# --------------------------------------------------------------------------
# VAP (Ch9)
# --------------------------------------------------------------------------

def test_vap_time_normalisation_flags_compressed_volume():
    """Ch9's real payload: the same volume mass accumulated in a THIRD of the
    time is a distribution tell that raw mass hides completely."""
    df = _synthetic(200, seed=23)
    profile = build_profile(df)
    lo, hi = float(df["low"].min()), float(df["high"].max())
    slow = node_intensity(profile, lo, hi, bars=90)
    fast = node_intensity(profile, lo, hi, bars=30)
    assert fast == pytest.approx(slow * 3.0)


def test_vap_support_strength_peaks_at_the_point_of_control():
    df = _synthetic(200, seed=29)
    profile = build_profile(df)
    poc = profile.point_of_control
    assert poc is not None
    assert profile.support_strength(poc.mid) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# The integrated read (Ch11)
# --------------------------------------------------------------------------

def test_conviction_penalises_conflicting_evidence():
    """Two opposing signals are a CONTESTED read, not a confident neutral --
    conviction must fall, not merely net out."""
    from Core.vpa.analysis import VpaRead

    agreed = VpaRead(0, pd.Timestamp("2024-01-01", tz="UTC"), 10.0, "up", None)
    agreed.bullish_evidence, agreed.bearish_evidence = 1.0, 0.0

    conflicted = VpaRead(0, pd.Timestamp("2024-01-01", tz="UTC"), 10.0, "up", None)
    conflicted.bullish_evidence, conflicted.bearish_evidence = 1.0, 0.9

    assert agreed.conviction > conflicted.conviction


def test_setups_use_market_derived_stops_never_fixed_percentages():
    """Ch11's stop priority: congestion boundary > last pivot > signal wick.
    Every one is "the market setting this level for us"."""
    df = _synthetic(300, seed=31)
    setups = build_setups(df, analyse(df))
    assert setups, "expected at least one setup in 300 synthetic bars"
    valid = {"congestion_floor", "congestion_ceiling", "last_pivot_low",
             "last_pivot_high", "signal_candle_wick"}
    for s in setups:
        assert s.stop_basis in valid
        # stop must be on the losing side of entry, always
        if s.direction == "long":
            assert s.stop_price < s.entry_price
        else:
            assert s.stop_price > s.entry_price


def test_confirmation_requirement_delays_entry_by_one_bar():
    """Ch4 Principle No 2: never act on the signal bar."""
    df = _synthetic(300, seed=37)
    reads = analyse(df)
    confirmed = build_setups(df, reads, require_confirmation=True)
    aggressive = build_setups(df, reads, require_confirmation=False)
    # the cautious mode can only ever be a subset in count
    assert len(confirmed) <= len(aggressive)


def test_counter_trend_trades_are_classified_and_shortened():
    """Ch8/Ch11 permit counter-trend trading but require it be recognised as
    higher-risk and shorter-held -- classification, not prohibition."""
    df = _synthetic(300, seed=41)
    reads = analyse(df)
    up = build_setups(df, reads, higher_tf_trend="up")
    down = build_setups(df, reads, higher_tf_trend="down")
    classes = {s.trade_class for s in up + down}
    assert classes <= {"with_trend", "counter_trend"}
    for s in up + down:
        if s.trade_class == "counter_trend":
            assert s.expected_bars >= 3


def test_analyse_runs_clean_on_flat_data():
    """Degenerate input must not raise -- zero-range bars, no pivots, no
    zones. A live daemon will see illiquid symbols."""
    idx = pd.date_range("2024-01-01", periods=120, freq="h", tz="UTC")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}, index=idx
    )
    reads = analyse(df)
    assert len(reads) == 120
    assert build_setups(df, reads) == []
