"""VPA daemon — live execution bridge.

Mirrors Core/weekend_hunter/execution.py.

POLAR_LIVE_TRADING (or POLAR_LIVE_TRADING_VPA for VPA-only override):
    unset / "false"  -- paper mode (default, no credentials needed)
    "dry-run"        -- log order payload but don't send
    "true"           -- live orders, real money

Kill switch: create Core/vpa/data/HALT to block new opens.
Flatten: python3 -m Core.vpa.execution --flatten
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from Core.Library.broker import BrokerError, Fill

DATA_DIR = Path(__file__).resolve().parent / "data"
HALT_FILE = DATA_DIR / "HALT"

MODE_OFF = "off"
MODE_DRY_RUN = "dry-run"
MODE_LIVE = "live"


def trading_mode() -> str:
    # Per-core override takes precedence over the global flag.
    raw = (
        os.getenv("POLAR_LIVE_TRADING_VPA")
        or os.getenv("POLAR_LIVE_TRADING")
        or ""
    ).strip().lower()
    if raw in ("true", "1", "yes", "live"):
        return MODE_LIVE
    if raw in ("dry-run", "dry_run", "dryrun"):
        return MODE_DRY_RUN
    return MODE_OFF


def halted() -> bool:
    return HALT_FILE.exists()


def halt(reason: str) -> None:
    """Trip the kill switch. Called when venue and local store diverge unsafely."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HALT_FILE.write_text(f"{datetime.now(timezone.utc).isoformat()}\n{reason}\n")
    print(f"[vpa] HALTED: {reason}", flush=True)


_broker = None


def get_broker():
    global _broker
    if _broker is None:
        from Core.Library.bitget_futures import BitgetFutures
        _broker = BitgetFutures()
        try:
            _broker.set_position_mode_one_way()
        except BrokerError as exc:
            print(f"[vpa] could not assert one-way mode: {exc}", flush=True)
    return _broker


def open_position_live(pos: dict) -> Fill | None:
    """Place a market order at the venue for the given VPA position dict.

    `pos` is the dict built in _open_pending() — it must contain:
      symbol, direction, entry, stop, target, margin_usdt, leverage, id

    Returns the venue's Fill on success, or None in dry-run / on error.
    The caller is responsible for writing exchange_order_id back to the store.
    """
    mode = trading_mode()
    if mode == MODE_OFF:
        return None

    broker = get_broker()
    notional_usdt = pos["margin_usdt"] * pos["leverage"]

    sl = pos["stop"]
    tp = pos["target"]

    if mode == MODE_DRY_RUN:
        price = broker.mark_price(pos["symbol"])
        size = broker.quantize_size(pos["symbol"], notional_usdt, price)
        print(
            f"[vpa] DRY-RUN open {pos['symbol']} {pos['direction']} "
            f"notional={notional_usdt:.2f} USDT  sl={sl}  tp={tp}  "
            f"~{size} contracts @ {price:.4f}",
            flush=True,
        )
        return None

    # Live
    try:
        fill = broker.open_position(
            symbol=pos["symbol"],
            direction=pos["direction"],
            notional_usdt=notional_usdt,
            leverage=pos["leverage"],
            stop_loss=sl,
            take_profit=tp,
            client_oid=pos["id"],
        )
        print(
            f"[vpa] LIVE opened {pos['symbol']} {fill.side} "
            f"fill={fill.avg_price:.4f}  notional={fill.notional_usdt:.2f} USDT  "
            f"order={fill.order_id}",
            flush=True,
        )
        return fill
    except BrokerError as exc:
        print(f"[vpa] open_position FAILED {pos['symbol']}: {exc}", flush=True)
        return None


def reconcile_closes(open_positions: list[dict]) -> list[dict]:
    """Ask the venue which open positions it has already closed (stop/target hit).

    Returns a list of updated position dicts with venue PnL filled in.
    The caller writes these back to the store.
    """
    if trading_mode() != MODE_LIVE:
        return []
    if not open_positions:
        return []

    broker = get_broker()
    closed: list[dict] = []

    for pos in open_positions:
        if not pos.get("exchange_order_id"):
            continue
        try:
            trades = broker.recent_closed_trades(pos["symbol"], since_iso=pos["opened_at"])
        except BrokerError as exc:
            print(f"[vpa] reconcile {pos['symbol']} error: {exc}", flush=True)
            continue

        if not trades:
            continue

        # Most recent close for this symbol wins.
        trade = sorted(trades, key=lambda t: t.closed_at)[-1]
        updated = dict(pos)
        updated.update(
            status="closed",
            exit_price=trade.exit_price,
            exit_reason=trade.reason,
            closed_at=trade.closed_at,
            pnl_usdt=round(trade.realized_pnl_usdt, 4),
        )
        closed.append(updated)

    return closed


def equity_live() -> float | None:
    """Return live account equity from the venue, or None if unavailable."""
    if trading_mode() != MODE_LIVE:
        return None
    try:
        return get_broker().account_equity_usdt()
    except BrokerError as exc:
        print(f"[vpa] equity_live error: {exc}", flush=True)
        return None


# ── flatten CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--flatten" not in sys.argv:
        print("Usage: python3 -m Core.vpa.execution --flatten")
        sys.exit(1)

    import json
    from Core.vpa import positions as store

    broker = get_broker()
    open_pos = store.get_open()
    if not open_pos:
        print("[vpa] flatten: no open positions in local store.")
        sys.exit(0)

    for pos in open_pos:
        sym = pos["symbol"]
        try:
            venue_pos = broker.get_position(sym)
            if venue_pos is None:
                print(f"[vpa] flatten: {sym} — already flat at venue")
            else:
                fill = broker.close_position(sym, pos["direction"])
                print(f"[vpa] flatten: closed {sym} @ {fill.avg_price:.4f}  pnl≈{fill.notional_usdt:.2f} USDT")
        except BrokerError as exc:
            print(f"[vpa] flatten: {sym} FAILED: {exc}")
