import pytest

from Core.Library.paper_fills import Exit, pnl_usdt, resolve_exit


def test_long_wick_through_stop_is_a_stop_even_if_close_recovers():
    bars = [(1, 100, 101, 98.9, 100.5)]
    assert resolve_exit("bullish", 99, 105, bars) == Exit("sl", 99, 1)


def test_short_wick_through_stop():
    bars = [(1, 100, 101.2, 99.5, 100)]
    assert resolve_exit("bearish", 101, 95, bars) == Exit("sl", 101, 1)


def test_stop_wins_when_one_bar_touches_both():
    bars = [(1, 100, 106, 98, 103)]
    assert resolve_exit("bullish", 99, 105, bars).status == "sl"


def test_gap_through_stop_fills_at_open():
    bars = [(1, 100, 100.5, 99.5, 100), (2, 97, 98, 96, 97.5)]
    assert resolve_exit("bullish", 99, 105, bars) == Exit("sl", 97, 2)


def test_target_fills_at_target_not_better():
    bars = [(1, 100, 100.5, 99.5, 100), (2, 107, 108, 106, 107)]
    assert resolve_exit("bullish", 99, 105, bars) == Exit("tp", 105, 2)


def test_short_target():
    bars = [(1, 100, 100.5, 94.8, 95)]
    assert resolve_exit("bearish", 101, 95, bars) == Exit("tp", 95, 1)


def test_no_target_and_no_stop_hit_stays_open():
    assert resolve_exit("bullish", 90, None, [(1, 100, 110, 95, 105)]) is None


def test_pnl_includes_fees_both_directions():
    assert pnl_usdt("bullish", 100, 110, 10, 3) == pytest.approx(30 * (0.10 - 0.0018))
    assert pnl_usdt("bearish", 100, 110, 10, 3) == pytest.approx(30 * (-0.10 - 0.0018))


def test_regression_weekend_hunter_avax_2026_10_03():
    """Recorded as a +11.80 TP by the polled paper engine; price hit the stop first at 04:27."""
    entry, stop = 11.07, 11.07 * (1 - 0.0075)
    target = entry + 3.3 * (entry - stop)
    bars = [
        (1, entry, entry * 1.001, entry * 0.999, entry),
        (2, entry, entry * 1.002, stop * 0.999, entry * 0.998),  # wick through stop, recovers
        (3, entry, target * 1.001, entry, target),
    ]
    assert resolve_exit("bullish", stop, target, bars).status == "sl"
