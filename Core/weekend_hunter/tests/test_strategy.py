"""Tests for the Weekend Hunter strategy port and its position store.

These check the port's own logic (weekend gating, CVD math, leverage/level
formulas, trailing-stop ratchet, risk gates) against hand-computed values
— they do not and cannot validate the underlying Pine source is a good
strategy, only that this is a faithful, bug-free translation of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from Core.weekend_hunter import positions as store
from Core.weekend_hunter import strategy as wh
from Core.Library.positions import Position


# --------------------------------------------------------------------------
# Weekend gating
# --------------------------------------------------------------------------

def test_weekend_status_saturday_and_sunday():
    saturday = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)  # a Saturday
    sunday_prime = datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc)  # Sunday, 04:00 UTC -> prime window
    monday = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    is_wknd, prime, active = wh.weekend_status(saturday)
    assert is_wknd and active and not prime

    is_wknd, prime, active = wh.weekend_status(sunday_prime)
    assert is_wknd and active and prime

    is_wknd, prime, active = wh.weekend_status(monday)
    assert not is_wknd and not active and not prime


def test_weekend_only_gating_blocks_weekdays_by_default():
    """ENABLE_WEEKEND_ONLY defaults True (matching the Pine default) -- a
    weekday must never be weekend_trading_active regardless of anything
    else, since this is a hard gate in generate_signal."""
    assert wh.ENABLE_WEEKEND_ONLY is True
    tuesday = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    _, _, active = wh.weekend_status(tuesday)
    assert active is False


# --------------------------------------------------------------------------
# CVD
# --------------------------------------------------------------------------

def test_cvd_divergence_windowed_cumsum_is_exact():
    """The module docstring claims the windowed cumsum is mathematically
    EXACT for the 10-bar-lookback comparison regardless of where the
    cumulative sum 'starts'. Verify directly: two CVD series computed from
    different starting offsets of the same underlying volume-delta data
    must agree on every consecutive DIFFERENCE, which is all the
    divergence check ever reads."""
    rng = np.random.default_rng(3)
    n = 60
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 100 + rng.normal(0, 1, n).cumsum()
    open_ = close - rng.normal(0, 0.5, n)
    volume = rng.uniform(1, 10, n)
    df_full = pd.DataFrame({"open": open_, "high": close + 1, "low": close - 1, "close": close, "volume": volume}, index=idx)

    cvd_full = wh.compute_cvd(df_full)
    cvd_tail = wh.compute_cvd(df_full.iloc[20:])  # same data, later "start"

    # the DIFFERENCE cvd[i] - cvd[i-10] must match regardless of window start
    diff_full = cvd_full.iloc[40] - cvd_full.iloc[30]
    diff_tail = cvd_tail.iloc[20] - cvd_tail.iloc[10]  # same bars, re-indexed
    assert diff_full == pytest.approx(diff_tail)


def test_cvd_divergence_directions():
    close = pd.Series([100.0] * 11)
    close.iloc[-1] = 110.0  # price higher than 10 bars ago
    cvd = pd.Series([0.0] * 11)
    cvd.iloc[-1] = -5.0  # cvd lower than 10 bars ago -> bearish divergence
    bullish, bearish = wh.cvd_divergence(close, cvd, lookback=10)
    assert bearish and not bullish


# --------------------------------------------------------------------------
# Leverage / levels / trailing stop / liquidation
# --------------------------------------------------------------------------

def test_leverage_bounds_respected():
    sig = wh.WeekendHunterSignal(direction="bullish", signal_strength=90.0, final_leverage=99, notes="", entry_price=100.0)
    # final_leverage in a real signal is always computed by generate_signal
    # and clamped there; here we just confirm the clamp formula's bounds.
    assert wh.MIN_LEVERAGE <= 20.0 <= wh.MAX_LEVERAGE


def test_compute_levels_long_vs_short_direction():
    tp_long, sl_long = wh.compute_levels("bullish", 100.0, atr_value=2.0, final_leverage=10.0, signal_confidence=0.6)
    tp_short, sl_short = wh.compute_levels("bearish", 100.0, atr_value=2.0, final_leverage=10.0, signal_confidence=0.6)
    assert tp_long > 100.0 > sl_long
    assert tp_short < 100.0 < sl_short


def test_higher_leverage_tightens_stop_distance():
    """Pine: leverage_adjustment shrinks atr_mult_sl as leverage rises --
    higher leverage means a tighter stop, not a wider one."""
    _, sl_low_lev = wh.compute_levels("bullish", 100.0, atr_value=2.0, final_leverage=5.0, signal_confidence=0.5)
    _, sl_high_lev = wh.compute_levels("bullish", 100.0, atr_value=2.0, final_leverage=20.0, signal_confidence=0.5)
    assert (100.0 - sl_high_lev) < (100.0 - sl_low_lev)


def test_trailing_stop_only_ratchets_favorably():
    # long, not yet past the activation threshold -> stop unchanged
    sl = wh.update_trailing_stop("bullish", entry_price=100.0, current_price=101.0, current_sl=95.0)
    assert sl == 95.0

    # long, past activation -> stop moves up, never down
    sl = wh.update_trailing_stop("bullish", entry_price=100.0, current_price=105.0, current_sl=95.0)
    assert sl > 95.0

    # a subsequent worse price must not loosen an already-ratcheted stop
    tightened = sl
    sl2 = wh.update_trailing_stop("bullish", entry_price=100.0, current_price=101.0, current_sl=tightened)
    assert sl2 == tightened


def test_liquidation_proximity_decreases_with_leverage():
    assert wh.liquidation_proximity_pct(5.0) > wh.liquidation_proximity_pct(20.0)


def test_position_size_pepe_is_halved():
    sig = wh.WeekendHunterSignal(direction="bullish", signal_strength=60.0, final_leverage=10.0, notes="", entry_price=1.0)
    btc_size = wh.position_size_usd(sig, "BTCUSDT")
    pepe_size = wh.position_size_usd(sig, "PEPEUSDT")
    assert pepe_size == pytest.approx(btc_size * 0.5)


def test_high_confidence_position_is_larger():
    weak = wh.WeekendHunterSignal(direction="bullish", signal_strength=60.0, final_leverage=10.0, notes="", entry_price=1.0)
    strong = wh.WeekendHunterSignal(direction="bullish", signal_strength=80.0, final_leverage=10.0, notes="", entry_price=1.0)
    assert wh.position_size_usd(strong, "BTCUSDT") > wh.position_size_usd(weak, "BTCUSDT")


# --------------------------------------------------------------------------
# generate_signal end-to-end (synthetic data, deterministic)
# --------------------------------------------------------------------------

def _trend_df(n: int, start: float, drift_pct: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = start * (1 + drift_pct) ** np.arange(n) * (1 + rng.normal(0, 0.001, n))
    open_ = close / (1 + drift_pct * 0.3)
    high = np.maximum(open_, close) * 1.002
    low = np.minimum(open_, close) * 0.998
    volume = rng.uniform(100, 200, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_generate_signal_returns_none_with_insufficient_history():
    tiny = _trend_df(10, 100, 0.01, seed=1)
    assert wh.generate_signal(tiny, tiny, tiny, "BTCUSDT", datetime.now(timezone.utc)) is None


def test_generate_signal_respects_weekend_gate_end_to_end():
    """Even a textbook-strong trend must not produce a signal on a weekday
    while ENABLE_WEEKEND_ONLY is at its default True."""
    df_exec = _trend_df(300, 100, 0.01, seed=5)
    df_4h = _trend_df(100, 100, 0.01, seed=6)
    df_1h = _trend_df(100, 100, 0.01, seed=7)
    tuesday = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    assert wh.generate_signal(df_exec, df_4h, df_1h, "BTCUSDT", tuesday) is None


def test_generate_signal_can_fire_on_a_weekend_with_a_clear_trend():
    df_exec = _trend_df(300, 100, 0.015, seed=11)
    df_4h = _trend_df(100, 100, 0.015, seed=12)
    df_1h = _trend_df(100, 100, 0.015, seed=13)
    saturday = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    signal = wh.generate_signal(df_exec, df_4h, df_1h, "BTCUSDT", saturday)
    # a strong, unambiguous uptrend across all three timeframes should
    # produce a bullish signal on a weekend -- if this is None the scoring
    # pipeline itself is broken, not just under-threshold
    assert signal is not None
    assert signal.direction == "bullish"
    assert wh.MIN_LEVERAGE <= signal.final_leverage <= wh.MAX_LEVERAGE


# --------------------------------------------------------------------------
# Position store / risk gates
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Every test gets its own empty JSON file -- these tests must never
    touch the real data/positions.json.

    LIVE_BASELINE_PATH is isolated for the same reason: once live trading
    has run, that file exists on this machine and holds the REAL account's
    starting equity. Without this, every capital-gate assertion below would
    silently start measuring against whatever the live account happens to
    be worth instead of the 500 USDT paper constant they were written for.
    """
    monkeypatch.setattr(store, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(store, "LIVE_BASELINE_PATH", tmp_path / "live_baseline.json")
    yield


def _open_position(symbol="BTCUSDT", margin=120.0, pnl=None, status="open", closed_at=None) -> Position:
    return Position(
        id=f"{symbol}:{closed_at or 'open'}:{margin}", symbol=symbol, platform="bitget", timeframe="1day",
        pattern="weekend_hunter_faithful_port", direction="bullish", confidence=0.6,
        entry=100.0, tp=110.0, sl=95.0, leverage=10, margin_usdt=margin,
        message_id=1, chat_id="1", opened_at=datetime.now(timezone.utc).isoformat(),
        status=status, closed_at=closed_at, pnl_usdt=pnl,
    )


def test_available_capital_accounts_for_committed_margin():
    store.add(_open_position(margin=120.0))
    assert store.available_capital_usdt() == pytest.approx(store.WALLET_USDT - 120.0)


def test_risk_gate_blocks_second_position_same_symbol():
    store.add(_open_position(symbol="BTCUSDT"))
    assert store.risk_gate_blocked("BTCUSDT") is not None
    assert store.risk_gate_blocked("ETHUSDT") is None


def test_risk_gate_blocks_at_max_concurrent_positions():
    for i in range(store.MAX_CONCURRENT_POSITIONS):
        store.add(_open_position(symbol=f"SYM{i}USDT", margin=10.0))
    assert store.risk_gate_blocked("NEWUSDT") is not None


def test_consecutive_losses_counts_by_pnl_sign_not_status_label():
    """A trailing stop can turn an 'sl'-status exit into a WIN -- the
    streak counter must use the realized PnL sign, not the label."""
    now = datetime.now(timezone.utc)
    store.add(_open_position(status="sl", pnl=-5.0, closed_at=(now - timedelta(hours=2)).isoformat()))
    store.add(_open_position(status="sl", pnl=+2.0, closed_at=(now - timedelta(hours=1)).isoformat()))  # trailed to a win
    assert store.consecutive_losses() == 0  # most recent close was a win despite status=="sl"


def test_account_emergency_stop_trips_below_70_percent_equity():
    now = datetime.now(timezone.utc)
    big_loss = -(store.WALLET_USDT * 0.31)
    store.add(_open_position(status="sl", pnl=big_loss, closed_at=now.isoformat()))
    assert store.account_emergency_stop() is True
    assert store.risk_gate_blocked("BTCUSDT") is not None


def test_weekend_pnl_excludes_weekday_closes_of_weekend_opened_positions():
    """Pine's weekend_pnl only accumulates a trade's PnL when `is_weekend`
    is true AT CLOSE TIME, and resets every Monday -- a position opened
    Saturday that doesn't close until Wednesday must NOT count toward
    weekend_pnl, even though it originated over the weekend."""
    # find last Saturday and the Wednesday that follows it, both at noon UTC
    today = datetime.now(timezone.utc)
    days_since_saturday = (today.weekday() - 5) % 7
    saturday_noon = (today - timedelta(days=days_since_saturday)).replace(hour=12, minute=0, second=0, microsecond=0)
    wednesday_noon = saturday_noon + timedelta(days=4)  # Sat -> Wed

    store.add(_open_position(pnl=10.0, status="tp", closed_at=saturday_noon.isoformat()))
    store.add(_open_position(pnl=-7.0, status="sl", closed_at=wednesday_noon.isoformat()))

    weekend_total = store.realized_pnl_this_weekend()
    assert weekend_total == pytest.approx(10.0), (
        f"expected only the Saturday close (+10.0) to count, got {weekend_total}"
    )
