"""Weekend Hunter's own wallet and risk gates, on top of the generic
Core.Library.positions store.

WALLET_USDT = 500.0 matches the Pine script's own `initial_capital=500`
default exactly (not this project's usual 200 USDT convention elsewhere)
— the script's position sizing is in ABSOLUTE dollars ($120 base per
position, see strategy.position_size_usd), which only makes sense against
the same capital base it was designed around.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Core.Library import positions as store
from Core.Library.positions import Position

DATA_DIR = Path(__file__).resolve().parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
# The account's equity the first time live trading ever ran. Persisted
# because every drawdown/loss-limit gate below is measured against the
# capital the strategy STARTED with -- recomputing that baseline from
# current equity on each restart would make a 30% drawdown mathematically
# unreachable, silently disabling the emergency stop.
LIVE_BASELINE_PATH = DATA_DIR / "live_baseline.json"

WALLET_USDT = 500.0  # matches the Pine script's own initial_capital default exactly
MAX_CONCURRENT_POSITIONS = 4  # Pine: max_concurrent_positions
MAX_CONSECUTIVE_LOSSES = 4  # Pine: max_consecutive_losses_allowed
DAILY_LOSS_LIMIT_PCT = 0.05  # Pine: max_daily_loss input, default 5.0
WEEKEND_LOSS_LIMIT_PCT = 0.15  # Pine: max_weekend_loss input, default 15.0
MAX_DRAWDOWN_PCT = 0.30  # Pine: account_emergency_stop, equity < initial_capital * 0.7
MIN_CAPITAL_PER_TRADE = 120.0  # Pine: min_capital_per_trade
SAFETY_BUFFER = 50.0  # Pine: safety_buffer

# The capital the Pine script's ABSOLUTE-DOLLAR figures were written
# against. Everything above ($120 min capital, $50 buffer, and
# strategy.position_size_usd's $120 base) is a fixed dollar amount, not a
# percentage -- which silently assumes an account of this size.
#
# On a smaller account those constants do not just shrink the returns, they
# break the strategy outright: at 163 USDT, `can_afford_position()` requires
# 170 and every single signal is refused. Scaling them by the real capital
# preserves the source's PROPORTIONS (24% of capital per position, 10%
# buffer) at whatever size the account actually is.
PINE_DESIGN_CAPITAL = 500.0


def capital_scale() -> float:
    """Ratio of real capital to the 500 USDT the dollar figures assume.

    1.0 on paper (WALLET_USDT is 500), so paper behaviour is bit-for-bit
    unchanged. Live, it follows the frozen baseline in live_baseline.json —
    which means it does NOT drift as the account gains or loses, exactly
    like the Pine constants it is scaling.
    """
    return initial_capital_usdt() / PINE_DESIGN_CAPITAL


def min_capital_per_trade() -> float:
    return MIN_CAPITAL_PER_TRADE * capital_scale()


def safety_buffer() -> float:
    return SAFETY_BUFFER * capital_scale()


def add(position: Position) -> None:
    store.add(position, POSITIONS_PATH)


def update(position: Position) -> None:
    store.update(position, POSITIONS_PATH)


def open_positions() -> list[Position]:
    return store.open_positions(POSITIONS_PATH)


def closed_positions() -> list[Position]:
    return store.closed_positions(POSITIONS_PATH)


def total_realized_pnl() -> float:
    return store.total_realized_pnl(POSITIONS_PATH)


def initial_capital_usdt() -> float:
    """The capital base every risk gate is measured against.

    Paper: the Pine script's own 500 default. Live: the account's equity
    when live trading first started, frozen at that moment (see
    LIVE_BASELINE_PATH).
    """
    if LIVE_BASELINE_PATH.exists():
        return float(json.loads(LIVE_BASELINE_PATH.read_text())["initial_equity_usdt"])
    return WALLET_USDT


def record_live_baseline(equity_usdt: float) -> None:
    """Freeze the starting capital on the first live run. Never overwrites:
    a later call on a drawn-down account would reset the baseline downward
    and hand the strategy a fresh 30% to lose."""
    if LIVE_BASELINE_PATH.exists():
        return
    LIVE_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_BASELINE_PATH.write_text(json.dumps({
        "initial_equity_usdt": equity_usdt,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def current_equity_usdt(broker_equity_usdt: float | None = None) -> float:
    """Live equity when a broker figure is supplied, otherwise the paper
    equity curve.

    The two must not be mixed: a venue's equity ALREADY reflects realized
    PnL, so adding `total_realized_pnl()` on top of it would count every
    closed trade twice and inflate the capital the risk gates believe is
    available.
    """
    if broker_equity_usdt is not None:
        return broker_equity_usdt
    return WALLET_USDT + total_realized_pnl()


def available_capital_usdt(broker_available_usdt: float | None = None) -> float:
    """Pine: available_capital = strategy.equity - (strategy.position_size * close),
    approximated as wallet minus committed margin (this store tracks
    margin directly rather than raw position_size*price).

    Live, the venue's own free-margin figure is authoritative and replaces
    the estimate — it accounts for fees already charged and for anything
    else sharing the account, neither of which the local store can see.
    """
    if broker_available_usdt is not None:
        return broker_available_usdt
    return store.available_balance_usdt(WALLET_USDT + total_realized_pnl(), POSITIONS_PATH)


def can_afford_position(broker_available_usdt: float | None = None) -> bool:
    """Pine: can_afford_position = available_capital > (min_capital_per_trade + safety_buffer),
    with both thresholds scaled to the real capital base — see capital_scale()."""
    return available_capital_usdt(broker_available_usdt) > (min_capital_per_trade() + safety_buffer())


def realized_pnl_today() -> float:
    return store.realized_pnl_today(POSITIONS_PATH)


def realized_pnl_this_weekend() -> float:
    """Pine: `weekend_pnl` only ever accumulates a closed trade's PnL when
    `is_weekend` is true AT THE MOMENT IT CLOSES (`if is_weekend:
    weekend_pnl := weekend_pnl + trade_pnl`), and resets to 0 every Monday
    (`if dayofmonth != last_day: ... if dayofweek == dayofweek.monday:
    weekend_pnl := 0.0`).

    A position opened Saturday/Sunday can still be sitting open into the
    following week and close on, say, Wednesday (TP/SL isn't gated to
    weekends, only new entries are) — that Wednesday close must NOT count
    toward weekend_pnl, since it didn't close on a weekend day and Pine's
    own reset would already have zeroed the accumulator by then. The
    window is therefore capped at the following Monday 00:00 UTC, not
    left open-ended from the most recent Saturday."""
    now = datetime.now(timezone.utc)
    days_since_saturday = (now.weekday() - 5) % 7
    weekend_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_saturday)
    weekend_end = weekend_start + timedelta(days=2)  # following Monday 00:00 -- Pine's reset point
    total = 0.0
    for p in closed_positions():
        if not p.closed_at:
            continue
        closed_at = datetime.fromisoformat(p.closed_at)
        if weekend_start <= closed_at < weekend_end:
            total += p.pnl_usdt or 0.0
    return total


def consecutive_losses() -> int:
    return store.consecutive_losses(POSITIONS_PATH)


def loss_limits_active() -> bool:
    """Pine: loss_limits_active = daily_loss_pct <= -max_daily_loss or
    weekend_loss_pct <= -max_weekend_loss or consecutive_losses >= max_consecutive_losses_allowed.

    Percentages are taken against the STARTING capital, not current equity —
    against current equity the same dollar loss would represent a smaller
    percentage as the account grows and a larger one as it shrinks, which
    inverts the intent (the limit should tighten in a drawdown, not loosen).
    """
    base = initial_capital_usdt()
    daily_loss_pct = (realized_pnl_today() / base) * 100
    weekend_loss_pct = (realized_pnl_this_weekend() / base) * 100
    return (
        daily_loss_pct <= -DAILY_LOSS_LIMIT_PCT * 100
        or weekend_loss_pct <= -WEEKEND_LOSS_LIMIT_PCT * 100
        or consecutive_losses() >= MAX_CONSECUTIVE_LOSSES
    )


def account_emergency_stop(broker_equity_usdt: float | None = None) -> bool:
    """Pine: account_emergency_stop = strategy.equity < (strategy.initial_capital * 0.7).

    Live, `broker_equity_usdt` is the venue's real equity INCLUDING open
    unrealized PnL — so this trips while losing positions are still open,
    rather than waiting for them to close and be recorded locally. That is
    the intended behaviour for an emergency stop.
    """
    base = initial_capital_usdt()
    equity = current_equity_usdt(broker_equity_usdt)
    return equity < base * (1 - MAX_DRAWDOWN_PCT)


def risk_gate_blocked(
    symbol: str,
    broker_equity_usdt: float | None = None,
    broker_available_usdt: float | None = None,
) -> str | None:
    """Every Pine entry-condition gate except the signal itself
    (strong_bullish_signal/strong_bearish_signal, checked by the caller)
    and flash_crash_detected (also caller-checked, needs price data):
    weekend_trading_active, loss_limits_active, position_available,
    can_afford_position, single-position-per-symbol (pyramiding=0),
    account_emergency_stop."""
    if account_emergency_stop(broker_equity_usdt):
        return "account emergency stop tripped (equity below 70% of initial capital)"
    if loss_limits_active():
        streak = consecutive_losses()
        return f"loss limits active (daily/weekend loss limit or {streak} consecutive losses)"
    if len(open_positions()) >= MAX_CONCURRENT_POSITIONS:
        return f"already at MAX_CONCURRENT_POSITIONS ({MAX_CONCURRENT_POSITIONS})"
    if store.count_open_for_symbol(symbol, POSITIONS_PATH) >= 1:
        return f"already have an open position on {symbol} (pyramiding=0)"
    if not can_afford_position(broker_available_usdt):
        return f"insufficient available capital (need > {min_capital_per_trade() + safety_buffer():.2f} USDT)"
    return None
