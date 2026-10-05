"""Shared risk primitives used by all Cores."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_CONSECUTIVE_LOSSES = 4
LOSS_STREAK_COOLDOWN_HOURS = 12
MIN_RISK_FRACTION = 0.003   # floor: never size below this fraction of equity per trade
RISK_PCT_PER_TRADE = 0.005  # 0.5% unlevered equity risk per trade

# Graduated position-size multiplier as drawdown deepens.
# Each entry is (drawdown_threshold, size_multiplier).
# Evaluated top-to-bottom; first threshold exceeded wins.
DRAWDOWN_LADDER: tuple[tuple[float, float], ...] = (
    (0.30, 0.00),  # ≥30% drawdown → halt (size = 0)
    (0.20, 0.25),  # ≥20% drawdown → 25% of normal size
    (0.15, 0.50),  # ≥15% drawdown → 50%
    (0.10, 0.75),  # ≥10% drawdown → 75%
    (0.00, 1.00),  # baseline → full size
)


def _load_positions(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def consecutive_losses(path_or_closed: Path | list) -> int:
    """Return the number of consecutive losses at the tail of the closed book.

    Accepts either a Path to a positions JSON file or an already-loaded list.
    A 'loss' is any closed position with pnl_usdt < 0.
    """
    if isinstance(path_or_closed, Path):
        positions = _load_positions(path_or_closed)
    else:
        positions = list(path_or_closed)

    closed = sorted(
        [p for p in positions if p.get("status") == "closed" and p.get("closed_at")],
        key=lambda p: p["closed_at"],
    )

    count = 0
    for p in reversed(closed):
        pnl = p.get("pnl_usdt")
        if pnl is not None and pnl < 0:
            count += 1
        else:
            break
    return count


def loss_streak_cooldown_until(path: Path) -> datetime | None:
    """Return the datetime until which trading is paused after a loss streak.

    Returns None if no cooldown is currently active.
    """
    positions = _load_positions(path)
    closed = sorted(
        [p for p in positions if p.get("status") == "closed" and p.get("closed_at")],
        key=lambda p: p["closed_at"],
    )

    if consecutive_losses(closed) < MAX_CONSECUTIVE_LOSSES:
        return None

    last_close_str = closed[-1]["closed_at"]
    last_close = datetime.fromisoformat(last_close_str)
    if last_close.tzinfo is None:
        last_close = last_close.replace(tzinfo=timezone.utc)

    return last_close + timedelta(hours=LOSS_STREAK_COOLDOWN_HOURS)


def is_in_cooldown(path: Path) -> bool:
    """Return True if the Core should pause due to a loss streak."""
    until = loss_streak_cooldown_until(path)
    if until is None:
        return False
    return datetime.now(tz=timezone.utc) < until


def drawdown_multiplier(equity_usdt: float, baseline_usdt: float) -> float:
    """Return the DRAWDOWN_LADDER size multiplier for the current equity level."""
    if baseline_usdt <= 0:
        return 1.0
    drawdown = max(0.0, (baseline_usdt - equity_usdt) / baseline_usdt)
    for threshold, multiplier in DRAWDOWN_LADDER:
        if drawdown >= threshold:
            return multiplier
    return 1.0


def position_size_usd(
    equity_usdt: float,
    stop_distance_pct: float,
    leverage: float = 3.0,
    baseline_usdt: float | None = None,
) -> float:
    """Return the notional USDT position size for a single trade.

    Uses fractional-Kelly / risk-fraction sizing:
      risk_usdt  = equity * RISK_PCT_PER_TRADE * drawdown_multiplier
      size_usdt  = (risk_usdt / stop_distance_pct) * leverage

    stop_distance_pct must be expressed as a fraction (e.g. 0.02 for a 2% stop).
    """
    if stop_distance_pct <= 0:
        return 0.0

    dm = drawdown_multiplier(equity_usdt, baseline_usdt or equity_usdt)
    risk_fraction = max(MIN_RISK_FRACTION, RISK_PCT_PER_TRADE * dm)
    risk_usdt = equity_usdt * risk_fraction
    return (risk_usdt / stop_distance_pct) * leverage
