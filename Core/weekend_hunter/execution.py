"""Weekend Hunter's bridge between a signal and a real Bitget order.

`daemon.py` decides WHAT to trade; this decides whether that may reach the
venue at all, and keeps the local position store honest about what the
venue actually did.

Three modes, via POLAR_LIVE_TRADING in `.env`:

    unset / "false"  -- signal only. Identical to the behaviour before
                        execution existed: nothing is sent, nothing is
                        signed, credentials are not even required.
    "dry-run"        -- everything runs, including credential checks,
                        contract lookups and size/price quantization, and
                        the exact order payload is logged -- but the final
                        place-order call is not made. Use this to confirm
                        sizing against real prices before arming it.
    "true"           -- live. Real orders, real money.

The default is signal-only on purpose. This module is imported by a daemon
that systemd restarts automatically; a config file that goes missing, or a
fresh checkout on another machine, must fail CLOSED (stop trading) rather
than open.

KILL SWITCH
-----------
Create the file `Core/weekend_hunter/data/HALT` and the next poll opens no
new positions, regardless of POLAR_LIVE_TRADING. Existing positions keep
their venue-side stops and targets, and `--flatten` closes them:

    touch Core/weekend_hunter/data/HALT              # stop opening
    python3 -m Core.weekend_hunter.execution --flatten   # close everything now

`--flatten` is also the right first move if anything looks wrong: it asks
the venue to market-close every position it holds for these symbols, and
does not depend on the local store being correct.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Core.Library.broker import BrokerError, Fill
from Core.Library.positions import Position

DATA_DIR = Path(__file__).resolve().parent / "data"
HALT_FILE = DATA_DIR / "HALT"

MODE_OFF = "off"
MODE_DRY_RUN = "dry-run"
MODE_LIVE = "live"


def trading_mode() -> str:
    raw = (os.getenv("POLAR_LIVE_TRADING") or "").strip().lower()
    if raw in ("true", "1", "yes", "live"):
        return MODE_LIVE
    if raw in ("dry-run", "dry_run", "dryrun"):
        return MODE_DRY_RUN
    return MODE_OFF


def halted() -> bool:
    return HALT_FILE.exists()


def halt(reason: str) -> None:
    """Trip the kill switch from inside the daemon. Called when the venue
    and the local store disagree in a way that cannot be resolved safely —
    continuing to size new positions against a store that does not match
    reality is how a bad hour becomes a bad day."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HALT_FILE.write_text(f"{datetime.now(timezone.utc).isoformat()}\n{reason}\n")
    print(f"[weekend_hunter] HALTED: {reason}", flush=True)


_broker = None


def get_broker():
    """Lazily built so signal-only mode never needs credentials, and so an
    unreachable venue surfaces at the call site rather than at import."""
    global _broker
    if _broker is None:
        from Core.Library.bitget_futures import BitgetFutures

        _broker = BitgetFutures()
        # One-way mode matches the strategy's pyramiding=0 rule. Idempotent,
        # but only worth asserting once per process.
        try:
            _broker.set_position_mode_one_way()
        except BrokerError as exc:
            # Bitget rejects the mode switch while any position is open. That
            # is fine if we are ALREADY one-way; it is not fine otherwise,
            # and the caller cannot tell the difference here, so just record
            # it rather than dying at startup.
            print(f"[weekend_hunter] could not assert one-way mode: {exc}", flush=True)
    return _broker


def client_oid(position_id: str) -> str:
    """Deterministic per position, so a retry after an ambiguous network
    failure is rejected by the venue as a duplicate instead of opening a
    second position on the same signal.

    Bitget's clientOid is length-limited and alphanumeric-ish; the position
    id is `SYMBOL:ISO8601`, which contains colons, dashes and dots.
    """
    cleaned = "".join(c if c.isalnum() else "" for c in position_id)
    return f"wh{cleaned}"[:40]


def open_position(position: Position) -> Fill | None:
    """Send the entry. Returns the real fill, or None if nothing was sent.

    Raises BrokerError if the venue refused — the caller must NOT record a
    local position in that case, or the store will claim exposure that does
    not exist and the risk gates will start blocking on phantom trades.
    """
    mode = trading_mode()
    if mode == MODE_OFF:
        return None
    if halted():
        print(f"[weekend_hunter] {position.symbol}: HALT file present — not opening", flush=True)
        return None

    broker = get_broker()
    notional = position.margin_usdt * position.leverage

    if mode == MODE_DRY_RUN:
        price = broker.mark_price(position.symbol)
        size = broker.quantize_size(position.symbol, notional, price)
        print(
            f"[weekend_hunter] DRY-RUN would open {position.symbol} "
            f"{'LONG' if position.direction == 'bullish' else 'SHORT'} "
            f"size={size} @ ~{price} notional={notional:.2f} USDT "
            f"({position.margin_usdt:.2f} margin x {position.leverage}x) "
            f"sl={broker.round_price(position.symbol, position.sl)} "
            f"tp={broker.round_price(position.symbol, position.tp)} "
            f"clientOid={client_oid(position.id)}",
            flush=True,
        )
        return None

    fill = broker.open_position(
        symbol=position.symbol,
        direction=position.direction,
        notional_usdt=notional,
        leverage=position.leverage,
        stop_loss=position.sl,
        take_profit=position.tp,
        client_oid=client_oid(position.id),
    )
    print(
        f"[weekend_hunter] LIVE opened {position.symbol} {fill.side} "
        f"size={fill.size_base} @ {fill.avg_price} order={fill.order_id}",
        flush=True,
    )
    return fill


def sync_trailing_stop(position: Position) -> None:
    """Push a ratcheted stop to the venue. Best-effort by design: the
    position still has its previous venue-side stop, so a failure here
    leaves protection in place at a worse level rather than none at all."""
    if trading_mode() != MODE_LIVE or not position.exchange_order_id:
        return
    try:
        get_broker().update_stop_loss(position.symbol, position.direction, position.sl)
    except BrokerError as exc:
        print(
            f"[weekend_hunter] {position.symbol}: trailing stop NOT updated at venue "
            f"({exc}) — venue stop remains at its previous level",
            flush=True,
        )


def close_position(position: Position) -> Fill | None:
    """Market-close for the strategy's non-TP/SL exits (weekend end, flash
    crash, emergency stop). TP and SL do not come through here — the venue
    fires those itself and `reconcile_closed` discovers them."""
    mode = trading_mode()
    if mode != MODE_LIVE or not position.exchange_order_id:
        return None
    return get_broker().close_position(position.symbol, position.direction)


def reconcile_closed(position: Position) -> dict | None:
    """Has the venue already closed this position behind our back?

    This is the normal path for a stop or target firing: Bitget executes it
    the instant price touches, while the daemon only looks every 300s. On
    every poll, a position the local store thinks is open but the venue
    reports flat gets closed locally using the VENUE's realized PnL.

    Returns a dict of fields to apply to the local position, or None if the
    venue still holds it.
    """
    if trading_mode() != MODE_LIVE or not position.exchange_order_id:
        return None

    broker = get_broker()
    if broker.get_position(position.symbol) is not None:
        return None  # still open at the venue

    # Flat at the venue. Find the matching close in position history, from
    # slightly before this position opened -- clock skew between us and the
    # venue is small but not zero, and a missed match here would leave the
    # local store permanently claiming an open position.
    since = (datetime.fromisoformat(position.opened_at) - timedelta(minutes=5)).isoformat()
    try:
        trades = broker.recent_closed_trades(position.symbol, since)
    except BrokerError as exc:
        print(f"[weekend_hunter] {position.symbol}: could not fetch close history: {exc}", flush=True)
        return None

    for trade in trades:
        if trade.direction != position.direction:
            continue
        # Classify by where it actually exited, since Bitget's history does
        # not label the trigger. Compared against the ORIGINAL stop: `sl`
        # has usually moved by now, and a trailed stop that fires in profit
        # is still an exit via the stop, which is exactly the distinction
        # Core.Library.positions.consecutive_losses() documents.
        is_long = position.direction == "bullish"
        hit_tp = trade.exit_price >= position.tp if is_long else trade.exit_price <= position.tp
        return {
            "status": "tp" if hit_tp else "sl",
            "exit_price": trade.exit_price,
            "closed_at": trade.closed_at,
            # The venue's own realized figure, already net of real fees.
            # daemon.py's modeled fee/slippage deduction is deliberately
            # bypassed for live trades -- applying it here would double-count.
            "pnl_usdt": trade.realized_pnl_usdt,
            "pnl_pct_of_margin": (
                trade.realized_pnl_usdt / position.margin_usdt if position.margin_usdt else 0.0
            ),
        }

    # Venue says flat, history says nothing. Do not guess a PnL into the
    # store -- every risk gate downstream reads these numbers.
    halt(
        f"{position.symbol} is flat at Bitget but no matching closed trade was found "
        f"since {since}. Local store and venue disagree; resolve manually."
    )
    return None


def flatten_all(symbols: list[str]) -> None:
    """Close everything the venue holds for these symbols, now.

    Reads from the VENUE, not the local store, so it still works when the
    two have diverged — which is precisely when you most want it.
    """
    broker = get_broker()
    for symbol in symbols:
        try:
            pos = broker.get_position(symbol)
            if pos is None:
                continue
            fill = broker.close_position(symbol, pos.direction)
            print(f"[weekend_hunter] flattened {symbol} @ {fill.avg_price}", flush=True)
        except BrokerError as exc:
            print(f"[weekend_hunter] FAILED to flatten {symbol}: {exc}", flush=True)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    if "--flatten" in sys.argv:
        from Core.weekend_hunter.daemon import SYMBOLS

        halt("manual --flatten")
        flatten_all(SYMBOLS)
    elif "--resume" in sys.argv:
        if HALT_FILE.exists():
            HALT_FILE.unlink()
            print("[weekend_hunter] HALT cleared — new positions may open again", flush=True)
    elif "--status" in sys.argv:
        print(f"mode={trading_mode()} halted={halted()}")
        # Always attempt the account read, even in signal-only mode. The
        # whole point of --status is to verify credentials BEFORE arming, so
        # gating it on the mode being live defeats the purpose.
        if not os.getenv("BITGET_API_KEY"):
            print("credentials: not configured (BITGET_API_KEY unset) — signal-only")
            return
        try:
            broker = get_broker()
            print(f"credentials: OK — signed request accepted by Bitget")
            print(f"equity={broker.account_equity_usdt():.2f} USDT "
                  f"available={broker.available_usdt():.2f} USDT")
            for symbol in ("BTCUSDT",):
                pos = broker.get_position(symbol)
                if pos is not None:
                    print(f"open at venue: {symbol} {pos.direction} {pos.size_base} @ {pos.entry_price}")
        except BrokerError as exc:
            print(f"credentials: FAILED — {exc}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
