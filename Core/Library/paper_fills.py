"""Candle-based exit resolution for paper positions.

Paper Cores must resolve stops and targets from each bar's high/low, never from a
polled last price: a poll misses any wick that crosses a stop and comes back between
polls, which a real venue stop would have filled. That bug overstated Weekend Hunter's
paper P&L by ~23 USDT over 20 trades (2026-10-03..04).

Conventions (all deliberately pessimistic):
  * stop is checked before target when one bar touches both;
  * a bar that OPENS through the stop fills at the open (gap slippage);
  * a target fills at the target price, never better.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

FEE_ROUND_TRIP = 0.0018


@dataclass(frozen=True)
class Exit:
    status: str  # "sl" | "tp"
    price: float
    ts: int


def resolve_exit(
    direction: str,
    stop: float,
    target: float | None,
    bars: Iterable[Sequence[float]],
) -> Exit | None:
    """bars: (ts, open, high, low, close) for every bar after entry, oldest first."""
    long = direction == "bullish"
    for ts, o, h, l, _c in bars:
        if long:
            if o <= stop:
                return Exit("sl", o, int(ts))
            if l <= stop:
                return Exit("sl", stop, int(ts))
            if target is not None and h >= target:
                return Exit("tp", target, int(ts))
        else:
            if o >= stop:
                return Exit("sl", o, int(ts))
            if h >= stop:
                return Exit("sl", stop, int(ts))
            if target is not None and l <= target:
                return Exit("tp", target, int(ts))
    return None


def pnl_usdt(
    direction: str,
    entry: float,
    exit_price: float,
    margin_usdt: float,
    leverage: float,
    fee_round_trip: float = FEE_ROUND_TRIP,
) -> float:
    notional = margin_usdt * leverage
    move = (exit_price / entry - 1) * (1 if direction == "bullish" else -1)
    return notional * (move - fee_round_trip)
