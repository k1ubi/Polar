"""Generic paper-position tracking: a signal's lifecycle from alert to
TP/SL/manual-exit outcome, as a JSON file store.

Not tied to any one Core or wallet — every function takes an optional
`path`, so each Core (Weekend Hunter, and whatever else eventually goes
live) keeps its own file and its own wallet, sharing only this one
well-tested implementation rather than each copy-pasting it. This mirrors
how the prior Polar codebase converged on the same design after several
per-strategy stores had drifted out of sync with each other.

This tracks what a Core *told* the user, for paper/signal purposes — it
does not place or track real exchange orders.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Position:
    id: str  # f"{symbol}:{opened_at}"
    symbol: str
    platform: str
    timeframe: str
    pattern: str
    direction: str  # "bullish" | "bearish"
    confidence: float
    entry: float
    tp: float | None  # None for a Core with no target-price formula (e.g. VPA — see Doc/Core/vpa.md)
    sl: float
    leverage: int
    margin_usdt: float
    message_id: int | None
    chat_id: str | None
    opened_at: str  # ISO 8601
    status: str = "open"  # "open" | "tp" | "sl" | "time" (see Core-specific docs for what each Core actually emits)
    closed_at: str | None = None
    exit_price: float | None = None
    pnl_usdt: float | None = None
    pnl_pct_of_margin: float | None = None
    # `sl` becomes a MOVING stop once a trailing mechanism engages — see
    # e.g. Core/weekend_hunter/strategy.py's update_trailing_stop().
    # original_sl is frozen at open and never changes, so an R-multiple
    # (how far into profit a trade is, in units of its own original risk)
    # can still be computed after sl has moved.
    original_sl: float | None = None
    # ISO 8601 -- for a Core whose exit rule includes a holding-period
    # expectation rather than (or in addition to) TP/SL. VPA's Ch11
    # cause-effect duration (a congestion zone's size sets how long to
    # expect the subsequent move to run) is the first user of this.
    expires_at: str | None = None
    # --- real-order linkage (see Core/Library/broker.py) ---
    # All None on a paper/signal-only position, which is what every
    # position in an existing store is -- these default so old JSON files
    # keep loading unchanged.
    #
    # exchange_order_id being set is the ONE test for "this position is
    # backed by real money": it is written only after a venue confirms a
    # fill, so a signal-only run can never be mistaken for a live one.
    exchange_order_id: str | None = None
    # The REAL average fill, which on a market order into a thin book can
    # differ from `entry` (the price the signal was computed at). PnL and
    # R-multiples on a live position must use this, not `entry`.
    exchange_fill_price: float | None = None
    # Filled quantity in base coin, as accepted by the venue after its own
    # step/precision rounding -- not the size we asked for.
    exchange_size_base: float | None = None
    exit_reason: str | None = None


def _load_raw(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def load(path: Path) -> list[Position]:
    return [Position(**p) for p in _load_raw(path)]


def _save(positions: list[Position], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(p) for p in positions], indent=2))


def add(position: Position, path: Path) -> None:
    positions = load(path)
    positions.append(position)
    _save(positions, path)


def update(position: Position, path: Path) -> None:
    positions = [p for p in load(path) if p.id != position.id]
    positions.append(position)
    _save(positions, path)


def open_positions(path: Path) -> list[Position]:
    return [p for p in load(path) if p.status == "open"]


def closed_positions(path: Path) -> list[Position]:
    return [p for p in load(path) if p.status != "open"]


def available_balance_usdt(wallet_usdt: float, path: Path) -> float:
    """Wallet minus margin already committed to open positions. Sizing a
    new position against the raw wallet figure (ignoring what's already
    committed) is the classic bug here — it lets a bad streak of open
    positions commit many times the actual wallet."""
    committed = sum(p.margin_usdt for p in open_positions(path))
    return max(0.0, wallet_usdt - committed)


def count_open_for_symbol(symbol: str, path: Path) -> int:
    return sum(1 for p in open_positions(path) if p.symbol == symbol)


def realized_pnl_today(path: Path) -> float:
    today = datetime.now(timezone.utc).date()
    total = 0.0
    for p in closed_positions(path):
        if p.closed_at and datetime.fromisoformat(p.closed_at).date() == today:
            total += p.pnl_usdt or 0.0
    return total


def total_realized_pnl(path: Path) -> float:
    return sum(p.pnl_usdt or 0.0 for p in closed_positions(path))


def peak_equity(starting_wallet_usdt: float, path: Path) -> float:
    """High-water mark of the paper wallet's equity curve. Drawdown is
    conventionally measured from the PEAK, not the starting balance —
    if equity grew to 300 and fell back to 220, that's a real 26.7%
    drawdown from peak, which a check against the starting 200 would miss
    entirely (220 > 200 looks fine)."""
    equity = starting_wallet_usdt
    peak = equity
    for p in sorted(closed_positions(path), key=lambda p: p.closed_at or ""):
        equity += p.pnl_usdt or 0.0
        peak = max(peak, equity)
    return peak


def consecutive_losses(path: Path) -> int:
    """How many losses in a row, most recent first, before hitting a win.

    Counts by actual realized PnL SIGN, not the status label — once a
    trailing/breakeven stop exists, an "sl"-status exit can still be a WIN
    (the stop trailed into profit before eventually getting hit), so
    `status == "sl"` means "closed via the stop level," not "closed at a
    loss."""
    count = 0
    for p in sorted(closed_positions(path), key=lambda p: p.closed_at or "", reverse=True):
        if (p.pnl_usdt or 0.0) < 0:
            count += 1
        else:
            break
    return count
