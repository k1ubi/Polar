"""One minimal REAL order, to prove the authenticated write path works
before the strategy ever depends on it.

Everything in `Core/Library/bitget_futures.py` was verified against Bitget's
published schema, and the public endpoints were exercised live — but until
this runs, no signed POST has ever been sent, no fill has been read back,
and no exchange-side stop has been confirmed to exist. That gap is the
whole reason this file exists.

    python3 -m Core.Library.smoke_test_bitget            # show the plan, place nothing
    python3 -m Core.Library.smoke_test_bitget --confirm  # actually run it

Cost: the venue minimum, ~6.50 USDT of notional at 5x — about 1.30 USDT of
margin and under a cent in round-trip fees. It touches neither the strategy
nor the position store; it drives the broker directly.

WHAT IT PROVES
    1. signing + auth + passphrase + IP binding      (signed GET)
    2. contract specs and size quantization
    3. one-way position mode
    4. signed POST with a body -- place-order         (the untested path)
    5. fill read-back (real avg price, real size)
    6. THE STOP AND TARGET ACTUALLY EXIST AT BITGET   (the safety-critical one)
    7. close-position
    8. reconciliation reporting the venue's real PnL

Stage 6 is the reason this is worth doing at all. `open_position` attaches
protection via `presetStopLossPrice`; if Bitget silently ignored that field,
every live position would run naked and nothing else in this codebase would
detect it. Here we read it back.

The position is closed in a `finally` block. If any stage fails partway, the
test still flattens before exiting.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from Core.Library.bitget_futures import BitgetFutures  # noqa: E402
from Core.Library.broker import BrokerError  # noqa: E402

SYMBOL = "BTCUSDT"
LEVERAGE = 5.0  # the strategy's own MIN_LEVERAGE
# Deliberately far out. These must NOT fire during the seconds this runs --
# the point is to confirm they were REGISTERED, not to watch them trigger.
STOP_PCT = 0.10
TARGET_PCT = 0.10

PASS, FAIL, INFO = "  \033[32m[PASS]\033[0m", "  \033[31m[FAIL]\033[0m", "  \033[36m[ .. ]\033[0m"


def main() -> int:
    confirm = "--confirm" in sys.argv
    client = BitgetFutures()

    print(f"\n\033[1mBitget live-path smoke test — {SYMBOL} @ {LEVERAGE}x\033[0m\n")

    # --- 1. signed GET: auth, passphrase, IP binding --------------------
    print("1. Credentials and signing")
    try:
        equity = client.account_equity_usdt()
        available = client.available_usdt()
        print(f"{PASS} signed request accepted — equity {equity:.2f} USDT, free {available:.2f} USDT")
    except BrokerError as exc:
        print(f"{FAIL} {exc}")
        print("\n  Check BITGET_API_KEY / BITGET_API_SECRET / BITGET_API_PASSPHRASE in .env,")
        print("  that the key has Trade permission on futures, and that this host's IP is allowed.")
        return 1

    # --- 2. contract specs and sizing -----------------------------------
    print("\n2. Contract specs and sizing")
    contract = client.contract(SYMBOL)
    price = client.mark_price(SYMBOL)
    min_size = float(contract["minTradeNum"])
    # Ask for exactly the venue minimum. The 1.001 nudge keeps float error
    # from flooring a value of exactly 1.0 steps down to 0.
    notional = min_size * price * 1.001
    size = client.quantize_size(SYMBOL, notional, price)
    margin = float(size) * price / LEVERAGE
    print(f"{PASS} min size {min_size} @ {price} -> size={size}, "
          f"notional {float(size) * price:.2f} USDT, margin {margin:.2f} USDT")

    if available < margin * 2:
        print(f"{FAIL} free margin {available:.2f} USDT is too low for even this test")
        return 1

    is_long = True
    stop_loss = price * (1 - STOP_PCT)
    take_profit = price * (1 + TARGET_PCT)

    if not confirm:
        print(f"\n\033[33mDry preview — nothing was sent.\033[0m")
        print(f"  Would open  LONG {size} {SYMBOL} @ ~{price} ({LEVERAGE}x)")
        print(f"  Stop {client.round_price(SYMBOL, stop_loss)} / target {client.round_price(SYMBOL, take_profit)}")
        print(f"  Margin at risk: {margin:.2f} USDT   Round-trip fees: ~{float(size) * price * 0.0012:.4f} USDT")
        print(f"  Then: verify stop exists at venue, close, and reconcile.\n")
        print("  Re-run with --confirm to execute.\n")
        return 0

    # --- 3. one-way position mode ---------------------------------------
    print("\n3. Position mode")
    try:
        client.set_position_mode_one_way()
        print(f"{PASS} one-way mode set")
    except BrokerError as exc:
        # Bitget refuses the switch while positions are open. Not fatal if
        # the account is already one-way, but the caller must know.
        print(f"{INFO} could not set one-way mode: {exc}")
        print(f"{INFO} continuing — verify manually that the account is one-way, not hedge")

    opened = False
    try:
        # --- 4/5. signed POST + fill read-back ---------------------------
        print("\n4. Placing the order (signed POST — the previously untested path)")
        client_oid = f"whsmoke{int(time.time())}"
        started = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        fill = client.open_position(
            symbol=SYMBOL, direction="bullish", notional_usdt=notional,
            leverage=LEVERAGE, stop_loss=stop_loss, take_profit=take_profit,
            client_oid=client_oid,
        )
        opened = True
        print(f"{PASS} order {fill.order_id} accepted")
        print(f"{PASS} fill read back — {fill.size_base} @ {fill.avg_price} "
              f"({fill.notional_usdt:.2f} USDT notional)")
        slip = abs(fill.avg_price - price) / price * 100
        print(f"{INFO} slippage vs mark at request time: {slip:.4f}%")

        # --- 6. THE ONE THAT MATTERS ---------------------------------------
        print("\n5. Verifying the stop and target exist AT BITGET")
        time.sleep(1.5)  # plan orders register a moment after the fill
        plans = client.pending_tpsl(SYMBOL)
        triggers = sorted(float(p.get("triggerPrice") or 0) for p in plans)
        if not plans:
            print(f"{FAIL} NO stop or target found at the venue.")
            print(f"{FAIL} presetStopLossPrice was accepted but did not register.")
            print(f"{FAIL} DO NOT ARM LIVE TRADING — positions would run unprotected.")
        else:
            print(f"{PASS} {len(plans)} plan order(s) live at triggers: "
                  f"{', '.join(f'{t:g}' for t in triggers)}")
            expected_sl = float(client.round_price(SYMBOL, stop_loss))
            if any(abs(t - expected_sl) / expected_sl < 0.01 for t in triggers):
                print(f"{PASS} stop-loss registered near {expected_sl:g} as requested")
            else:
                print(f"{FAIL} no plan order matches the requested stop {expected_sl:g}")

        # --- position read-back -----------------------------------------
        print("\n6. Position as the venue sees it")
        pos = client.get_position(SYMBOL)
        if pos is None:
            print(f"{FAIL} venue reports flat immediately after a confirmed fill")
        else:
            print(f"{PASS} {pos.direction} {pos.size_base} @ {pos.entry_price}, "
                  f"{pos.leverage:g}x, liq {pos.liquidation_price}")
            if abs(pos.leverage - LEVERAGE) > 0.01:
                print(f"{FAIL} leverage is {pos.leverage:g}x, expected {LEVERAGE:g}x — "
                      f"set_leverage did not take effect")

    finally:
        # --- 7. close, no matter what happened above --------------------
        if opened:
            print("\n7. Closing")
            try:
                close_fill = client.close_position(SYMBOL, "bullish")
                print(f"{PASS} closed @ {close_fill.avg_price}")
            except BrokerError as exc:
                print(f"{FAIL} COULD NOT CLOSE: {exc}")
                print(f"{FAIL} close {SYMBOL} manually in the Bitget app NOW.")
                return 1

            time.sleep(2)
            if client.get_position(SYMBOL) is not None:
                print(f"{FAIL} venue still reports a position — close it manually.")
                return 1
            print(f"{PASS} venue confirms flat")

            # --- 8. reconciliation --------------------------------------
            print("\n8. Reconciliation (how a real stop/target gets discovered)")
            for attempt in range(5):
                trades = client.recent_closed_trades(SYMBOL, started)
                if trades:
                    t = trades[0]
                    print(f"{PASS} venue reports the closed trade: "
                          f"entry {t.entry_price} -> exit {t.exit_price}")
                    print(f"{PASS} realized PnL {t.realized_pnl_usdt:+.4f} USDT (net of real fees)")
                    print(f"{PASS} closed_at parsed as {t.closed_at}")
                    break
                time.sleep(2)
            else:
                print(f"{FAIL} no closed trade found — reconcile_closed() would HALT the daemon here.")
                print(f"{INFO} history can lag; re-check with --confirm later before arming.")

    print(f"\n\033[1mDone.\033[0m Total cost: ~{float(size) * price * 0.0012:.4f} USDT in fees.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
