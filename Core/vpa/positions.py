"""VPA's own paper wallet and risk gates, on top of the generic
Core.Library.positions store.

WALLET_USDT = 200.0 — this project's usual convention for a Core with no
source-specified capital base (unlike Weekend Hunter, which matches its
Pine script's own $500 `initial_capital` exactly because that script's
position sizing is in fixed absolute dollars). Coulling's book gives no
capital figure at all, so there is nothing else to match.

Sizing is risk-based, not fixed-dollar: `VpaSetup.risk_fraction` (stop
distance as a fraction of entry) is exactly the input the book leaves for a
position sizer to use ("you will have to judge the risk on the trade") —
`position_size_usd()` risks a fixed % of the wallet per trade via that
fraction, capped as a fraction of available capital so a very tight stop
can't blow the size up unreasonably. No leverage: the book never discusses
leveraged trading, so this stays at 1x rather than inventing a multiplier
the source doesn't call for.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from Core.Library import positions as store
from Core.Library.positions import Position

DATA_DIR = Path(__file__).resolve().parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"

WALLET_USDT = 200.0  # this project's usual convention -- the book gives no capital figure to match
RISK_PCT_PER_TRADE = 0.02  # reasoned default (2% of wallet risked per trade), not book-derived
MAX_POSITION_FRACTION = 0.5  # a single position's margin capped at this fraction of available capital
MIN_MARGIN_USDT = 10.0  # below this a paper position is not worth opening on a 200 USDT wallet
MAX_CONCURRENT_POSITIONS = 4  # matches Weekend Hunter's own value -- a reasonable project-wide default
MAX_CONSECUTIVE_LOSSES = 4
DAILY_LOSS_LIMIT_PCT = 0.05
MAX_DRAWDOWN_PCT = 0.30


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
    return store.available_balance_usdt(WALLET_USDT + total_realized_pnl(), POSITIONS_PATH)


def realized_pnl_today() -> float:
    return store.realized_pnl_today(POSITIONS_PATH)


def consecutive_losses() -> int:
    return store.consecutive_losses(POSITIONS_PATH)


def account_emergency_stop() -> bool:
    equity = WALLET_USDT + total_realized_pnl()
    return equity < WALLET_USDT * (1 - MAX_DRAWDOWN_PCT)


def loss_limits_active() -> bool:
    daily_loss_pct = (realized_pnl_today() / WALLET_USDT) * 100
    return daily_loss_pct <= -DAILY_LOSS_LIMIT_PCT * 100 or consecutive_losses() >= MAX_CONSECUTIVE_LOSSES


def position_size_usd(risk_fraction: float) -> float:
    """Risk a fixed % of the wallet per trade, sized against the setup's
    own stop distance -- exactly the use `VpaSetup.risk_fraction` was built
    for. Capped as a fraction of available capital so a very tight stop
    (small risk_fraction) can't imply an unreasonably large position."""
    available = available_capital_usdt()
    if risk_fraction <= 0:
        return 0.0
    risk_amount = WALLET_USDT * RISK_PCT_PER_TRADE
    raw_margin = risk_amount / risk_fraction
    return max(0.0, min(raw_margin, available * MAX_POSITION_FRACTION))


def risk_gate_blocked(symbol: str, margin_usd: float) -> str | None:
    """Every generic risk gate this Core enforces before opening a position
    (the setup/signal quality itself is checked by the caller)."""
    if account_emergency_stop():
        return "account emergency stop tripped (equity below 70% of initial capital)"
    if loss_limits_active():
        return f"loss limits active (daily loss limit or {consecutive_losses()} consecutive losses)"
    if len(open_positions()) >= MAX_CONCURRENT_POSITIONS:
        return f"already at MAX_CONCURRENT_POSITIONS ({MAX_CONCURRENT_POSITIONS})"
    if store.count_open_for_symbol(symbol, POSITIONS_PATH) >= 1:
        return f"already have an open position on {symbol}"
    if margin_usd < MIN_MARGIN_USDT:
        return f"sized position ({margin_usd:.2f} USDT) below MIN_MARGIN_USDT ({MIN_MARGIN_USDT:.0f})"
    return None
