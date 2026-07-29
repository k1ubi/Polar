"""Tests for the live-execution path — the Bitget broker's signing and
sizing, and Weekend Hunter's mode gating and reconciliation.

These cover the parts where a bug spends real money: a signature that is
subtly wrong, a size rounded the wrong way, a mode flag that defaults open,
a reconciler that invents a PnL. Nothing here touches the network — the
broker is faked at the interface in Core/Library/broker.py.

What these CANNOT check is that Bitget accepts the payloads; the field
names were verified against the venue's published schema and the public
endpoints were exercised live, but the authenticated round trip is only
provable with real keys. See Doc/Infrastructure/execution.md.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from Core.Library.broker import BrokerError, BrokerPosition, ClosedTrade, Fill
from Core.Library.positions import Position
from Core.weekend_hunter import execution


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------

def _client(monkeypatch):
    from Core.Library.bitget_futures import BitgetFutures

    monkeypatch.setenv("BITGET_API_KEY", "key")
    monkeypatch.setenv("BITGET_API_SECRET", "secret")
    monkeypatch.setenv("BITGET_API_PASSPHRASE", "pass")
    return BitgetFutures()


def test_signature_matches_bitget_formula(monkeypatch):
    """timestamp + METHOD + requestPath + body, HMAC-SHA256, base64."""
    client = _client(monkeypatch)
    ts, method, path, body = "1700000000000", "POST", "/api/v2/mix/order/place-order", '{"a":1}'

    expected = base64.b64encode(
        hmac.new(b"secret", f"{ts}{method}{path}{body}".encode(), hashlib.sha256).digest()
    ).decode()

    assert client._sign(ts, method, path, body) == expected


def test_signature_lowercase_method_is_upcased(monkeypatch):
    """Bitget signs the UPPERCASE verb; signing 'post' yields a valid-looking
    signature the venue rejects."""
    client = _client(monkeypatch)
    assert client._sign("1", "post", "/x", "") == client._sign("1", "POST", "/x", "")


def test_missing_credentials_raise(monkeypatch):
    from Core.Library.bitget_futures import BitgetFutures

    for var in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(BrokerError, match="not set"):
        BitgetFutures()


# --------------------------------------------------------------------------
# Sizing — the conversion from USDT exposure to base-coin contract size
# --------------------------------------------------------------------------

CONTRACTS = {
    "BTCUSDT": {"symbol": "BTCUSDT", "minTradeNum": "0.0001", "volumePlace": "4",
                "pricePlace": "1", "sizeMultiplier": "0.0001", "maxLever": "150",
                "minTradeUSDT": "5", "symbolStatus": "normal"},
    # PEPE is the awkward one: size must be a whole multiple of 1000.
    "PEPEUSDT": {"symbol": "PEPEUSDT", "minTradeNum": "1000", "volumePlace": "0",
                 "pricePlace": "10", "sizeMultiplier": "1000", "maxLever": "75",
                 "minTradeUSDT": "5", "symbolStatus": "normal"},
    "HALTEDUSDT": {"symbol": "HALTEDUSDT", "minTradeNum": "1", "volumePlace": "0",
                   "pricePlace": "4", "sizeMultiplier": "1", "maxLever": "20",
                   "minTradeUSDT": "5", "symbolStatus": "maintain"},
}


@pytest.fixture
def client(monkeypatch):
    c = _client(monkeypatch)
    c._contracts = dict(CONTRACTS)
    return c


def test_size_respects_step_multiple(client):
    """PEPE at 720 USDT / 0.00000275 is ~261.8M, which must floor to a clean
    multiple of 1000 — 261485000, not 261818181."""
    size = client.quantize_size("PEPEUSDT", 720.0, 0.00000275)
    assert float(size) % 1000 == 0
    assert float(size) * 0.00000275 <= 720.0


def test_size_always_rounds_down(client):
    """Rounding up would take more risk than the strategy asked for and can
    exceed the free margin the risk gates just checked."""
    size = client.quantize_size("BTCUSDT", 1440.0, 64393.3)
    assert float(size) * 64393.3 <= 1440.0
    assert size == "0.0223"


def test_size_below_venue_minimum_raises(client):
    with pytest.raises(BrokerError, match="minimum order size"):
        client.quantize_size("BTCUSDT", 0.50, 64393.3)


def test_untradable_symbol_raises(client):
    with pytest.raises(BrokerError, match="not tradable"):
        client.quantize_size("HALTEDUSDT", 100.0, 1.0)


def test_unlisted_symbol_raises(client):
    with pytest.raises(BrokerError, match="not listed"):
        client.quantize_size("TONUSDT", 100.0, 1.0)


def test_price_truncates_to_tick(client):
    """Truncate, not round: a rounded stop can sit further from entry than
    intended, which matters when the stop is only ~0.3% away."""
    assert client.round_price("BTCUSDT", 64135.789) == "64135.7"


def test_leverage_above_venue_max_raises(client, monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: None)
    with pytest.raises(BrokerError, match="exceeds the venue maximum"):
        client.set_leverage("BTCUSDT", 200.0)  # BTCUSDT caps at 150x


def test_leverage_within_venue_max_is_accepted(client, monkeypatch):
    """The strategy's 5-20x band is far below every pair's cap, so this must
    never be the thing that blocks a trade."""
    sent = {}
    monkeypatch.setattr(client, "_request", lambda *a, **k: sent.update(k.get("body") or {}))
    client.set_leverage("BTCUSDT", 20.0)
    assert sent["leverage"] == "20"


# --------------------------------------------------------------------------
# Mode gating — the flag that stands between a signal and real money
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, execution.MODE_OFF),
    ("", execution.MODE_OFF),
    ("false", execution.MODE_OFF),
    ("no", execution.MODE_OFF),
    ("garbage", execution.MODE_OFF),   # unrecognized must NOT arm it
    ("TRUE", execution.MODE_LIVE),
    ("true", execution.MODE_LIVE),
    ("1", execution.MODE_LIVE),
    ("dry-run", execution.MODE_DRY_RUN),
    ("dry_run", execution.MODE_DRY_RUN),
])
def test_trading_mode_defaults_closed(monkeypatch, value, expected):
    monkeypatch.delenv("POLAR_LIVE_TRADING", raising=False)
    if value is not None:
        monkeypatch.setenv("POLAR_LIVE_TRADING", value)
    assert execution.trading_mode() == expected


def _position(**overrides) -> Position:
    base = dict(
        id="SOLUSDT:2026-08-01T12:00:00+00:00", symbol="SOLUSDT", platform="bitget",
        timeframe="15min", pattern="weekend_hunter_faithful_port", direction="bullish",
        confidence=0.7, entry=74.0, tp=75.2, sl=73.7, leverage=12, margin_usdt=96.0,
        message_id=None, chat_id=None, opened_at="2026-08-01T12:00:00+00:00",
        original_sl=73.7,
    )
    base.update(overrides)
    return Position(**base)


def test_signal_only_mode_sends_nothing(monkeypatch):
    monkeypatch.setenv("POLAR_LIVE_TRADING", "false")
    monkeypatch.setattr(execution, "get_broker", lambda: pytest.fail("broker must not be built"))
    assert execution.open_position(_position()) is None


def test_halt_file_blocks_opening(monkeypatch, tmp_path):
    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(execution, "HALT_FILE", tmp_path / "HALT")
    (tmp_path / "HALT").write_text("stop")
    monkeypatch.setattr(execution, "get_broker", lambda: pytest.fail("broker must not be built"))
    assert execution.open_position(_position()) is None


def test_client_oid_is_deterministic_and_venue_safe():
    """Same position id must always yield the same clientOid, so a retry
    after an ambiguous failure is rejected as a duplicate rather than
    opening a second position."""
    oid = execution.client_oid("SOLUSDT:2026-08-01T12:00:00+00:00")
    assert oid == execution.client_oid("SOLUSDT:2026-08-01T12:00:00+00:00")
    assert oid.isalnum() and len(oid) <= 40


def test_client_oid_differs_per_position():
    assert execution.client_oid("SOLUSDT:2026-08-01T12:00:00+00:00") != \
           execution.client_oid("SOLUSDT:2026-08-01T12:15:00+00:00")


# --------------------------------------------------------------------------
# Reconciliation — how a venue-side stop/target gets discovered
# --------------------------------------------------------------------------

class FakeBroker:
    def __init__(self, position=None, trades=None):
        self._position = position
        self._trades = trades or []

    def get_position(self, symbol):
        return self._position

    def recent_closed_trades(self, symbol, since_iso):
        return self._trades


def test_reconcile_skips_position_still_open_at_venue(monkeypatch):
    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(execution, "get_broker", lambda: FakeBroker(position=BrokerPosition(
        symbol="SOLUSDT", direction="bullish", size_base=15.5, entry_price=74.0,
        leverage=12, unrealized_pnl_usdt=3.0, liquidation_price=68.0,
    )))
    assert execution.reconcile_closed(_position(exchange_order_id="1")) is None


def test_reconcile_uses_venue_pnl_not_modeled(monkeypatch):
    """The venue's netProfit is already net of real fees — Polar's modeled
    fee/slippage must not be applied on top of it."""
    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(execution, "get_broker", lambda: FakeBroker(trades=[ClosedTrade(
        symbol="SOLUSDT", direction="bullish", entry_price=74.0, exit_price=75.3,
        realized_pnl_usdt=17.42, closed_at="2026-08-01T13:00:00+00:00", reason="unknown",
    )]))
    result = execution.reconcile_closed(_position(exchange_order_id="1"))
    assert result["pnl_usdt"] == 17.42
    assert result["pnl_pct_of_margin"] == pytest.approx(17.42 / 96.0)
    assert result["exit_price"] == 75.3


def test_reconcile_classifies_target_vs_stop(monkeypatch):
    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")

    def result_for(exit_price):
        monkeypatch.setattr(execution, "get_broker", lambda: FakeBroker(trades=[ClosedTrade(
            symbol="SOLUSDT", direction="bullish", entry_price=74.0, exit_price=exit_price,
            realized_pnl_usdt=0.0, closed_at="2026-08-01T13:00:00+00:00", reason="unknown",
        )]))
        return execution.reconcile_closed(_position(exchange_order_id="1"))["status"]

    assert result_for(75.3) == "tp"   # at or beyond the 75.2 target
    assert result_for(73.6) == "sl"   # through the 73.7 stop


def test_reconcile_halts_rather_than_guessing(monkeypatch, tmp_path):
    """Venue says flat, history has no matching trade. Writing an invented
    PnL would corrupt every risk gate downstream, so trip the kill switch
    instead."""
    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(execution, "DATA_DIR", tmp_path)
    monkeypatch.setattr(execution, "HALT_FILE", tmp_path / "HALT")
    monkeypatch.setattr(execution, "get_broker", lambda: FakeBroker(trades=[]))

    assert execution.reconcile_closed(_position(exchange_order_id="1")) is None
    assert (tmp_path / "HALT").exists()


# --------------------------------------------------------------------------
# Capital scaling — the Pine source's absolute dollars vs a real account
# --------------------------------------------------------------------------

def test_paper_capital_scale_is_exactly_one(monkeypatch, tmp_path):
    """No live baseline means paper, and paper must reproduce the Pine
    figures bit-for-bit — the scaling must not perturb the existing track."""
    from Core.weekend_hunter import positions as store

    monkeypatch.setattr(store, "LIVE_BASELINE_PATH", tmp_path / "absent.json")
    assert store.capital_scale() == 1.0
    assert store.min_capital_per_trade() == 120.0
    assert store.safety_buffer() == 50.0


def test_scaling_preserves_pine_proportions(monkeypatch, tmp_path):
    """At 163 USDT the thresholds must fall to the same FRACTION of capital
    the Pine script used at 500 — 24% min capital, 10% buffer."""
    import json

    from Core.weekend_hunter import positions as store

    baseline = tmp_path / "live_baseline.json"
    baseline.write_text(json.dumps({"initial_equity_usdt": 163.0, "started_at": "x"}))
    monkeypatch.setattr(store, "LIVE_BASELINE_PATH", baseline)

    assert store.capital_scale() == pytest.approx(163.0 / 500.0)
    assert store.min_capital_per_trade() / 163.0 == pytest.approx(120.0 / 500.0)
    assert store.safety_buffer() / 163.0 == pytest.approx(50.0 / 500.0)


def test_unscaled_gate_would_block_every_signal(monkeypatch, tmp_path):
    """The bug this scaling exists to fix: at 163 USDT the unscaled 170
    threshold refuses every trade, so the daemon would run forever and
    never open a position."""
    import json

    from Core.weekend_hunter import positions as store

    assert 163.0 < store.MIN_CAPITAL_PER_TRADE + store.SAFETY_BUFFER  # the bug

    baseline = tmp_path / "live_baseline.json"
    baseline.write_text(json.dumps({"initial_equity_usdt": 163.0, "started_at": "x"}))
    monkeypatch.setattr(store, "LIVE_BASELINE_PATH", baseline)

    assert store.can_afford_position(163.0)  # scaled: 163 > 55.42


def test_baseline_is_never_overwritten(tmp_path, monkeypatch):
    """Re-baselining on a drawn-down account would reset the emergency stop
    and hand the strategy a fresh 30% to lose."""
    from Core.weekend_hunter import positions as store

    baseline = tmp_path / "live_baseline.json"
    monkeypatch.setattr(store, "LIVE_BASELINE_PATH", baseline)

    store.record_live_baseline(163.0)
    store.record_live_baseline(80.0)  # after a 50% drawdown
    assert store.initial_capital_usdt() == 163.0


def test_position_size_scales_with_capital():
    from dataclasses import dataclass

    from Core.weekend_hunter import strategy as wh

    @dataclass
    class Sig:
        signal_strength: float
        final_leverage: float

    sig = Sig(60.0, 12.0)
    assert wh.position_size_usd(sig, "BTCUSDT") == 120.0            # default = Pine
    assert wh.position_size_usd(sig, "BTCUSDT", 0.326) == pytest.approx(39.12)
    # pair and confidence multipliers must survive the scaling
    assert wh.position_size_usd(sig, "PEPEUSDT", 0.326) == pytest.approx(19.56)
    assert wh.position_size_usd(Sig(80.0, 12.0), "BTCUSDT", 0.326) == pytest.approx(50.856)


# --------------------------------------------------------------------------
# Offline sizing against REAL venue specs (fixtures/bitget_contracts.json)
# --------------------------------------------------------------------------

def _real_contracts() -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "bitget_contracts.json"
    return json.loads(path.read_text())["contracts"]


def test_fixture_covers_the_whole_traded_universe():
    """If a pair is added to the daemon, the fixture must be recaptured —
    otherwise its sizing rules are never exercised offline."""
    from Core.weekend_hunter.daemon import SYMBOLS

    assert set(_real_contracts()) == set(SYMBOLS)


@pytest.mark.parametrize("symbol", [
    "BTCUSDT", "ETHUSDT", "LINKUSDT", "XRPUSDT", "DOGEUSDT",
    "SOLUSDT", "AVAXUSDT", "PEPEUSDT", "POLUSDT",
])
def test_every_pair_sizes_correctly_against_real_specs(monkeypatch, symbol):
    """Exercises each pair's ACTUAL step/precision/minimum rules with no
    network — the awkward ones (PEPE's 1000-multiple, BTC's 4dp) are the
    reason this is parameterized rather than spot-checked."""
    from Core.Library.bitget_futures import BitgetFutures

    client = _client(monkeypatch)
    client._contracts = _real_contracts()

    spec = _real_contracts()[symbol]
    price = 1.0 / float(spec["sizeMultiplier"]) * 7.0  # a price giving a workable notional
    notional = 400.0
    size = client.quantize_size(symbol, notional, price)

    step = float(spec["sizeMultiplier"])
    # Must land on a clean multiple of the venue's step, and never round up.
    assert abs(round(float(size) / step) - float(size) / step) < 1e-6
    assert float(size) * price <= notional
    assert float(size) >= float(spec["minTradeNum"])


def test_strategy_leverage_band_fits_every_pair():
    """The strategy can ask for up to 20x on any pair; if a listing ever
    lowered its cap below that, orders would start failing at the venue."""
    for symbol, spec in _real_contracts().items():
        assert float(spec["maxLever"]) >= 20.0, f"{symbol} caps at {spec['maxLever']}x"


# --------------------------------------------------------------------------
# Telegram reporting — paper and real must never look alike
# --------------------------------------------------------------------------

def test_money_tag_distinguishes_real_from_paper():
    """A combined list shows VPA's paper positions next to Weekend Hunter's
    live ones. `exchange_order_id` is the only authoritative test for which
    is which, and the label must follow it."""
    from Core.weekend_hunter import telegram_bot as bot

    assert bot._money_tag(_position()) == "📝 paper"
    assert bot._money_tag(_position(exchange_order_id="123")) == "💵 REAL"


def test_position_block_shows_exposure_only_for_real_positions():
    """Margin alone understates a leveraged position by up to 20x, so a real
    position must show what is actually at risk."""
    from Core.weekend_hunter import telegram_bot as bot

    real = bot._position_block(_position(exchange_order_id="123"), "Weekend Hunter")
    assert "Exposure" in real
    assert "1152.00" in real  # 96 margin x 12x
    assert "💵 REAL" in real

    paper = bot._position_block(_position(), "VPA")
    assert "Exposure" not in paper
    assert "📝 paper" in paper


def test_live_view_shows_only_real_positions(monkeypatch):
    """Live trading on -> the bot reports the real book, nothing else."""
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    real, paper = _position(exchange_order_id="123"), _position()
    assert bot._visible([real, paper]) == [real]


def test_paper_view_shows_only_paper_positions(monkeypatch):
    """Live trading off -> the bot reports the paper book, nothing else."""
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "off")
    real, paper = _position(exchange_order_id="123"), _position()
    assert bot._visible([real, paper]) == [paper]


def test_dry_run_counts_as_paper_view(monkeypatch):
    """Dry-run sends no orders, so nothing it produces is real."""
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "dry-run")
    assert bot.live_view() is False
    assert bot._visible([_position()]) == [_position()]


def test_open_real_positions_are_never_silently_hidden(monkeypatch):
    """The one exemption to the filter: real exposure still open at the
    venue must surface even in the paper view, or turning live trading off
    would blind you to money at risk."""
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "off")
    monkeypatch.setattr(bot.store, "open_positions",
                        lambda: [_position(exchange_order_id="123")])
    monkeypatch.setattr(bot.vpa_store, "open_positions", lambda: [])

    warning = bot._hidden_real_warning()
    assert warning is not None
    assert "1 REAL position(s) still open" in warning
    assert "1152.00 USDT" in warning  # 96 margin x 12x


def test_no_warning_when_nothing_real_is_open(monkeypatch):
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "off")
    monkeypatch.setattr(bot.store, "open_positions", lambda: [_position()])
    monkeypatch.setattr(bot.vpa_store, "open_positions", lambda: [])
    assert bot._hidden_real_warning() is None


def test_mode_banner_reflects_trading_mode(monkeypatch):
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(bot.execution, "halted", lambda: False)
    assert "LIVE" in bot._mode_banner()

    monkeypatch.setenv("POLAR_LIVE_TRADING", "off")
    assert "SIGNAL ONLY" in bot._mode_banner()


def test_mode_banner_surfaces_halt(monkeypatch):
    """A tripped kill switch must be visible on the report, not just in logs."""
    from Core.weekend_hunter import telegram_bot as bot

    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(bot.execution, "halted", lambda: True)
    assert "HALTED" in bot._mode_banner()


def test_paper_position_is_never_reconciled(monkeypatch):
    """A position with no exchange_order_id predates live trading; touching
    it against the venue would close a paper trade off a real position that
    has nothing to do with it."""
    monkeypatch.setenv("POLAR_LIVE_TRADING", "true")
    monkeypatch.setattr(execution, "get_broker", lambda: pytest.fail("must not query venue"))
    assert execution.reconcile_closed(_position()) is None
