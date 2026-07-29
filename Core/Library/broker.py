"""Venue-agnostic order execution — the interface a Core talks to when it
places REAL orders, as opposed to `Core.Library.positions`, which only ever
tracked what a Core *told* the user.

Polar's whole shape is "the needle can be swapped without touching the
instrument around it." That applies to the exchange as much as to the
strategy: a Core should ask for "open a 12x long on SOLUSDT with this stop
and this target" and not know or care whether that lands on Bitget, a
different venue, or a dry-run logger. Hence this module holds no HTTP and
no exchange vocabulary — `Core.Library.bitget_futures` supplies those.

Two invariants every implementation must honor, because the strategy layer
assumes them:

1. `open_position()` attaches the stop-loss and take-profit AT THE EXCHANGE,
   in the same call that opens the position. Polar's daemons poll on a
   multi-minute cycle; Weekend Hunter's ATR stops sit ~0.3-0.6% from entry
   on the 15m chart, which price can cross entirely between two polls. A
   stop that only exists in the poll loop is not a stop.
2. Sizes are expressed to the caller in USDT of NOTIONAL exposure. Every
   venue has its own contract denomination (Bitget USDT-M wants base coin,
   quantized to a per-symbol step); converting is the implementation's job,
   not the strategy's.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BrokerError(RuntimeError):
    """Any failure that means the requested order did NOT reach the venue in
    the state the caller asked for.

    Callers treat this as "assume nothing happened, do not retry blindly" —
    the ambiguous cases (request sent, response lost) are resolved by
    reconciling against the venue's own position list rather than by
    guessing here. See `Core.weekend_hunter.execution.reconcile()`.
    """


@dataclass(frozen=True)
class Fill:
    """What actually happened, as reported by the venue — never what was
    requested. `avg_price` is the real fill, which on a market order into a
    thin book can differ materially from the price the signal was computed
    at; the caller is expected to store this rather than its own intended
    entry."""

    order_id: str
    symbol: str
    side: str  # "buy" | "sell"
    size_base: float  # filled quantity in base coin (BTC, SOL, ...)
    avg_price: float
    notional_usdt: float


@dataclass(frozen=True)
class BrokerPosition:
    """A position as the VENUE currently sees it. The source of truth for
    reconciliation — if this disagrees with the local store, the venue is
    right and the local store is stale."""

    symbol: str
    direction: str  # "bullish" (long) | "bearish" (short) -- Polar's vocabulary, not the venue's
    size_base: float
    entry_price: float
    leverage: float
    unrealized_pnl_usdt: float
    liquidation_price: float | None


@dataclass(frozen=True)
class ClosedTrade:
    """A position the venue has already closed (stop/target hit, or closed
    by us). `realized_pnl_usdt` is the venue's own figure and INCLUDES the
    fees it actually charged — Polar's modeled `TAKER_FEE_PCT`/`SLIPPAGE_PCT`
    estimate must not be applied on top of it."""

    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    realized_pnl_usdt: float
    closed_at: str  # ISO 8601, UTC
    reason: str  # venue's close reason, normalized: "tp" | "sl" | "manual" | "liquidation" | "unknown"


class Broker(Protocol):
    """The whole surface a Core needs. Deliberately small: Polar's strategies
    decide WHAT to trade, and this decides only how that reaches a venue."""

    def account_equity_usdt(self) -> float:
        """Total account equity in USDT, including unrealized PnL. Replaces
        the hardcoded paper `WALLET_USDT` once real money is in play — risk
        gates sized against a stale constant stop being risk gates."""
        ...

    def available_usdt(self) -> float:
        """Free margin — equity minus what's already posted as collateral."""
        ...

    def open_position(
        self,
        symbol: str,
        direction: str,
        notional_usdt: float,
        leverage: float,
        stop_loss: float,
        take_profit: float,
        client_oid: str,
    ) -> Fill:
        """Market-in with the stop and target attached at the venue.

        `client_oid` must be derived deterministically from the caller's
        position id, so that a retry after an ambiguous failure is rejected
        as a duplicate by the venue instead of opening a second position.
        """
        ...

    def update_stop_loss(self, symbol: str, direction: str, stop_loss: float) -> None:
        """Move the venue-side stop — used when the strategy's trailing stop
        ratchets. A trailing stop tracked only locally provides no protection
        between polls, which is the entire reason it exists."""
        ...

    def get_position(self, symbol: str) -> BrokerPosition | None:
        """The venue's current view, or None if flat."""
        ...

    def close_position(self, symbol: str, direction: str) -> Fill:
        """Market-out the whole position. Used for the strategy's own
        non-TP/SL exits (weekend-end exit, flash crash, emergency stop)."""
        ...

    def recent_closed_trades(self, symbol: str, since_iso: str) -> list[ClosedTrade]:
        """Positions the venue closed on its own since `since_iso` — how a
        stop or target that fired between polls gets discovered."""
        ...
