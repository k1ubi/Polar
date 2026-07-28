"""Weekend Hunter's own wallet and risk gates, on top of the generic
Core.Library.positions store.

WALLET_USDT = 500.0 matches the Pine script's own `initial_capital=500`
default exactly (not this project's usual 200 USDT convention elsewhere)
— the script's position sizing is in ABSOLUTE dollars ($120 base per
position, see strategy.position_size_usd), which only makes sense against
the same capital base it was designed around.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from Core.Library import positions as store
from Core.Library.positions import Position

DATA_DIR = Path(__file__).resolve().parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"

WALLET_USDT = 500.0  # matches the Pine script's own initial_capital default exactly
MAX_CONCURRENT_POSITIONS = 4  # Pine: max_concurrent_positions
MAX_CONSECUTIVE_LOSSES = 4  # Pine: max_consecutive_losses_allowed
DAILY_LOSS_LIMIT_PCT = 0.05  # Pine: max_daily_loss input, default 5.0
WEEKEND_LOSS_LIMIT_PCT = 0.15  # Pine: max_weekend_loss input, default 15.0
MAX_DRAWDOWN_PCT = 0.30  # Pine: account_emergency_stop, equity < initial_capital * 0.7
MIN_CAPITAL_PER_TRADE = 120.0  # Pine: min_capital_per_trade
SAFETY_BUFFER = 50.0  # Pine: safety_buffer


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


def available_capital_usdt() -> float:
    """Pine: available_capital = strategy.equity - (strategy.position_size * close),
    approximated as wallet minus committed margin (this store tracks
    margin directly rather than raw position_size*price)."""
    return store.available_balance_usdt(WALLET_USDT + total_realized_pnl(), POSITIONS_PATH)


def can_afford_position() -> bool:
    """Pine: can_afford_position = available_capital > (min_capital_per_trade + safety_buffer)."""
    return available_capital_usdt() > (MIN_CAPITAL_PER_TRADE + SAFETY_BUFFER)


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
    weekend_loss_pct <= -max_weekend_loss or consecutive_losses >= max_consecutive_losses_allowed."""
    daily_loss_pct = (realized_pnl_today() / WALLET_USDT) * 100
    weekend_loss_pct = (realized_pnl_this_weekend() / WALLET_USDT) * 100
    return (
        daily_loss_pct <= -DAILY_LOSS_LIMIT_PCT * 100
        or weekend_loss_pct <= -WEEKEND_LOSS_LIMIT_PCT * 100
        or consecutive_losses() >= MAX_CONSECUTIVE_LOSSES
    )


def account_emergency_stop() -> bool:
    """Pine: account_emergency_stop = strategy.equity < (strategy.initial_capital * 0.7)."""
    equity = WALLET_USDT + total_realized_pnl()
    return equity < WALLET_USDT * (1 - MAX_DRAWDOWN_PCT)


def risk_gate_blocked(symbol: str) -> str | None:
    """Every Pine entry-condition gate except the signal itself
    (strong_bullish_signal/strong_bearish_signal, checked by the caller)
    and flash_crash_detected (also caller-checked, needs price data):
    weekend_trading_active, loss_limits_active, position_available,
    can_afford_position, single-position-per-symbol (pyramiding=0),
    account_emergency_stop."""
    if account_emergency_stop():
        return "account emergency stop tripped (equity below 70% of initial capital)"
    if loss_limits_active():
        streak = consecutive_losses()
        return f"loss limits active (daily/weekend loss limit or {streak} consecutive losses)"
    if len(open_positions()) >= MAX_CONCURRENT_POSITIONS:
        return f"already at MAX_CONCURRENT_POSITIONS ({MAX_CONCURRENT_POSITIONS})"
    if store.count_open_for_symbol(symbol, POSITIONS_PATH) >= 1:
        return f"already have an open position on {symbol} (pyramiding=0)"
    if not can_afford_position():
        return f"insufficient available capital (need > {MIN_CAPITAL_PER_TRADE + SAFETY_BUFFER:.0f} USDT)"
    return None
