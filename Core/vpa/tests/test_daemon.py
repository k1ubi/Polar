"""Tests for VPA's daemon: position sizing, risk gates, and the pure
scan-result helpers (`_latest_setup`, `_update_pending`) that decide what a
scan cycle acts on. Network-touching pieces (`_scan_symbol`, `get_universe`,
Telegram delivery) are exercised live in Doc/Core/vpa.md's verification
notes, not here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from Core.vpa import daemon
from Core.vpa import positions as store
from Core.vpa.analysis import MultiTimeframeRead, VpaRead, VpaSetup
from Core.vpa.signals import VpaSignal
from Core.Library.positions import Position


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Every test gets its own empty JSON file -- must never touch the real
    data/positions.json."""
    monkeypatch.setattr(store, "POSITIONS_PATH", tmp_path / "positions.json")
    yield


def _open_position(symbol="BTCUSDT", margin=20.0, pnl=None, status="open", closed_at=None) -> Position:
    return Position(
        id=f"{symbol}:{closed_at or 'open'}:{margin}", symbol=symbol, platform="bitget", timeframe="1h",
        pattern="vpa_multi_timeframe", direction="bullish", confidence=0.6,
        entry=100.0, tp=None, sl=95.0, leverage=1, margin_usdt=margin,
        message_id=1, chat_id="1", opened_at=datetime.now(timezone.utc).isoformat(),
        status=status, closed_at=closed_at, pnl_usdt=pnl,
    )


def _read(bias="bullish", evidence=1.0, signals=None, bar_index=9) -> VpaRead:
    """`evidence` feeds VpaRead.conviction's own formula
    (min(net/total,1) * min(total/2,1)) -- evidence=1.0 with one-sided
    evidence gives conviction=0.5, comfortably above build_setups'
    min_conviction=0.35 default."""
    return VpaRead(
        bar_index=bar_index, timestamp=pd.Timestamp("2026-07-28T00:00:00Z"), close=100.0,
        trend="up", zone=None, signals=signals or [],
        bullish_evidence=evidence if bias == "bullish" else 0.0,
        bearish_evidence=evidence if bias == "bearish" else 0.0,
    )


def _setup(direction="long", bar_index=10) -> VpaSetup:
    return VpaSetup(
        bar_index=bar_index, timestamp=pd.Timestamp("2026-07-28T01:00:00Z"), direction=direction,
        entry_price=100.0, stop_price=95.0, stop_basis="last_pivot_low", trade_class="with_trend",
        conviction=0.5, expected_bars=12, vap_support=0.5, rationale=["test_of_supply_passed: detail"],
    )


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------

def test_position_size_scales_inversely_with_risk_fraction():
    tight = store.position_size_usd(0.01)
    wide = store.position_size_usd(0.05)
    assert tight > wide  # a tighter stop implies a larger size for the same $ risk


def test_position_size_capped_by_available_capital_fraction():
    # An absurdly tight stop would otherwise imply a huge position.
    size = store.position_size_usd(0.0001)
    assert size == pytest.approx(store.WALLET_USDT * store.MAX_POSITION_FRACTION)


def test_position_size_zero_for_degenerate_risk_fraction():
    assert store.position_size_usd(0.0) == 0.0


# --------------------------------------------------------------------------
# Risk gates
# --------------------------------------------------------------------------

def test_risk_gate_blocks_second_position_same_symbol():
    store.add(_open_position(symbol="BTCUSDT"))
    assert store.risk_gate_blocked("BTCUSDT", 20.0) is not None
    assert store.risk_gate_blocked("ETHUSDT", 20.0) is None


def test_risk_gate_blocks_at_max_concurrent_positions():
    for i in range(store.MAX_CONCURRENT_POSITIONS):
        store.add(_open_position(symbol=f"SYM{i}USDT", margin=5.0))
    assert store.risk_gate_blocked("NEWUSDT", 20.0) is not None


def test_risk_gate_blocks_margin_below_minimum():
    assert store.risk_gate_blocked("BTCUSDT", store.MIN_MARGIN_USDT - 1) is not None
    assert store.risk_gate_blocked("BTCUSDT", store.MIN_MARGIN_USDT + 1) is None


def test_account_emergency_stop_trips_below_70_percent_equity():
    now = datetime.now(timezone.utc)
    big_loss = -(store.WALLET_USDT * 0.31)
    store.add(_open_position(status="sl", pnl=big_loss, closed_at=now.isoformat()))
    assert store.account_emergency_stop() is True
    assert store.risk_gate_blocked("BTCUSDT", 20.0) is not None


def test_consecutive_losses_counts_by_pnl_sign_not_status_label():
    now = datetime.now(timezone.utc)
    store.add(_open_position(status="sl", pnl=-2.0, closed_at=(now - timedelta(hours=2)).isoformat()))
    store.add(_open_position(status="time", pnl=+1.0, closed_at=(now - timedelta(hours=1)).isoformat()))
    assert store.consecutive_losses() == 0


# --------------------------------------------------------------------------
# _latest_setup -- only the bar just closed is actionable
# --------------------------------------------------------------------------

def test_latest_setup_actionable_when_bar_index_matches_final_bar():
    middle = [_read(bar_index=i) for i in range(11)]  # bars 0..10, len == 11
    mtf = MultiTimeframeRead(fast=[], middle=middle, slow=[], slow_trend="up", setups=[_setup(bar_index=10)])
    assert daemon._latest_setup(mtf) is not None


def test_latest_setup_none_when_stale():
    middle = [_read(bar_index=i) for i in range(11)]
    # The setup entered several bars ago -- its entry price is no longer live.
    mtf = MultiTimeframeRead(fast=[], middle=middle, slow=[], slow_trend="up", setups=[_setup(bar_index=5)])
    assert daemon._latest_setup(mtf) is None


def test_latest_setup_none_when_no_setups():
    middle = [_read(bar_index=i) for i in range(11)]
    mtf = MultiTimeframeRead(fast=[], middle=middle, slow=[], slow_trend="up", setups=[])
    assert daemon._latest_setup(mtf) is None


# --------------------------------------------------------------------------
# _update_pending -- the final bar's own (unconfirmed) read
# --------------------------------------------------------------------------

def test_update_pending_records_qualifying_final_read():
    signals = [VpaSignal("test_of_supply_passed", 9, "bullish", "campaign", 0.65, "detail")]
    read = _read(bias="bullish", evidence=1.0, signals=signals)
    mtf = MultiTimeframeRead(fast=[], middle=[read], slow=[], slow_trend="up", setups=[])
    pending: dict = {}
    daemon._update_pending(pending, "BTCUSDT", mtf)
    assert pending["BTCUSDT"]["bias"] == "bullish"
    assert pending["BTCUSDT"]["conviction"] == pytest.approx(read.conviction)


def test_update_pending_clears_stale_entry_once_neutral():
    pending = {"BTCUSDT": {"bias": "bullish", "conviction": 0.5}}
    mtf = MultiTimeframeRead(fast=[], middle=[_read(bias="bullish", evidence=0.0)],
                              slow=[], slow_trend="up", setups=[])
    daemon._update_pending(pending, "BTCUSDT", mtf)
    assert "BTCUSDT" not in pending


def test_update_pending_ignores_below_threshold_conviction():
    # evidence=0.3 clears the >0.15 net-evidence bar for a non-neutral bias
    # (net_evidence=0.3) while its conviction (0.15) stays below the 0.35
    # pending threshold -- isolates the conviction gate from the bias gate.
    read = _read(bias="bullish", evidence=0.3)
    assert read.bias == "bullish" and read.conviction < 0.35
    mtf = MultiTimeframeRead(fast=[], middle=[read], slow=[], slow_trend="up", setups=[])
    pending: dict = {}
    daemon._update_pending(pending, "BTCUSDT", mtf)
    assert "BTCUSDT" not in pending
