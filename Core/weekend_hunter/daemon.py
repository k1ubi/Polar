#!/usr/bin/env python3
"""Weekend Hunter's live loop: scans for signals, opens paper positions,
manages/closes them, pushes Telegram alerts.

Own strategy logic (strategy.py), own wallet/position store (positions.py),
own risk parameters — shares only genuinely generic infrastructure:
`Core.Library.candlestick_trend.backtest.fetch_ohlcv` for OHLCV (Bitget's
public REST API, no auth needed for market data) and
`Core.Library.telegram.TelegramNotifier` for delivery.

    python3 -m Core.weekend_hunter.daemon

Runs the creator's own recommended configuration: 15M execution, 1H
momentum, 4H trend, across the 10 pairs the script's own
`symbol_risk_profile` table is keyed on (BTC, ETH, LINK, XRP, DOGE, SOL,
AVAX, 1000PEPE, TON, POL). The prior version of this daemon ran only
BTCUSDT/ETHUSDT on the daily chart -- a deliberate deviation the earlier
codebase had verified manually, not the intended design; corrected here
to match the creator's actual recommendation.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from Core.Library import telegram as tg
from Core.Library.broker import BrokerError
from Core.Library.candlestick_trend.backtest import fetch_ohlcv
from Core.Library.indicators import compute_atr
from Core.Library.positions import Position
from Core.weekend_hunter import execution
from Core.weekend_hunter import positions as store
from Core.weekend_hunter import strategy as wh

# The creator's 10 pre-configured pairs -- same list as strategy.py's
# SYMBOL_RISK_PROFILE (that table's keys were always meant to BE the
# trading universe, not just a risk lookup with an unused fallback) --
# minus TONUSDT, which Bitget spot does not list at all (verified live
# against the exchange's symbols endpoint). 9 of the creator's 10 pairs,
# not 10 -- TON would need a different exchange to actually trade.
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "LINKUSDT", "XRPUSDT", "DOGEUSDT",
    "SOLUSDT", "AVAXUSDT", "PEPEUSDT", "POLUSDT",
]
EXEC_TIMEFRAME = "15min"  # creator spec: "15M execution"
HTF_4H = "4h"  # creator spec: "4H trend"
HTF_1H = "1h"  # creator spec: "1H momentum"
POLL_SECONDS = 300  # 15m bars close every 15 minutes -- poll a few times per bar, not once per bar
PLATFORM = "bitget"

DATA_DIR = Path(__file__).resolve().parent / "data"
DEDUP_STATE_PATH = DATA_DIR / "dedup.json"

# Fee/slippage modeling on paper-tracked exits (Bitget taker fee + typical
# slippage) -- values match this project's earlier-established defaults.
TAKER_FEE_PCT = 0.0006
SLIPPAGE_PCT = 0.0003


def _load_dedup_state() -> dict:
    if not DEDUP_STATE_PATH.exists():
        return {}
    return json.loads(DEDUP_STATE_PATH.read_text())


def _save_dedup_state(state: dict) -> None:
    DEDUP_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_STATE_PATH.write_text(json.dumps(state, indent=2))


def format_signal_message(
    symbol: str, signal: wh.WeekendHunterSignal, tp: float, sl: float,
    margin_usd: float, fill=None,
) -> str:
    is_long = signal.direction == "bullish"
    side_emoji = "🟢" if is_long else "🔴"
    side_label = "LONG" if is_long else "SHORT"
    leverage = signal.final_leverage
    notional = margin_usd * leverage
    rows = [
        ("Market", f"{PLATFORM}, {EXEC_TIMEFRAME}"),
        ("Entry", tg.fmt_price(fill.avg_price if fill else signal.entry_price)),
        ("TP / SL", f"{tg.fmt_price(tp)} / {tg.fmt_price(sl)}"),
        # Both figures, always. The margin alone is the number that misled
        # this system for its whole paper life -- what is actually at risk
        # is the notional, and a 12x position reads very differently once
        # the exposure is on the same line as the collateral.
        ("Margin", f"{margin_usd:.2f} USDT @ {leverage}x"),
        ("Exposure", f"{notional:.2f} USDT"),
    ]
    if fill is not None:
        rows.append(("Size", f"{fill.size_base:g}"))
        footer = f"<i>LIVE — order {fill.order_id} filled. Stop and target are held at {PLATFORM}.</i>"
    elif execution.trading_mode() == execution.MODE_DRY_RUN:
        footer = "<i>DRY-RUN — sizing computed against live prices, no order sent.</i>"
    else:
        footer = "<i>Signal only — no order placed. Separate test track, isolated wallet.</i>"
    return (
        f"🎯 <b>[Weekend Hunter]</b> {side_emoji} <b>{symbol}</b> — {side_label}\n\n"
        f"📊 Weekend Hunter (faithful port): {signal.notes}\n\n"
        f"{tg.row_block(rows)}\n\n"
        f"{footer}"
    )


def format_closed_message(position: Position) -> str:
    is_win = (position.pnl_usdt or 0) >= 0
    banner = "🟩🟩🟩 CLOSED IN PROFIT 🟩🟩🟩" if is_win else "🟥🟥🟥 CLOSED AT A LOSS 🟥🟥🟥"
    side_label = "LONG" if position.direction == "bullish" else "SHORT"
    rows = [
        ("Entry", tg.fmt_price(position.entry)),
        ("Exit", tg.fmt_price(position.exit_price)),
        ("PnL", f"{position.pnl_usdt:+.2f} USDT"),
    ]
    return (
        f"🎯 <b>[Weekend Hunter]</b> {banner}\n"
        f"<b>{position.symbol}</b> — {side_label}\n\n"
        f"{tg.row_block(rows)}\n\n"
        f"<i>Opened {tg.fmt_time(position.opened_at)} — closed {tg.fmt_time(position.closed_at)}</i>"
    )


def _finalize(position: Position, notifier: tg.TelegramNotifier) -> None:
    store.update(position)
    if position.message_id is not None:
        try:
            notifier.edit_message(position.message_id, format_closed_message(position))
        except Exception as exc:  # noqa: BLE001
            print(f"[weekend_hunter] failed to edit message for {position.id}: {exc}", flush=True)


def check_open_positions(notifier: tg.TelegramNotifier, broker_equity: float | None = None) -> None:
    now = datetime.now(timezone.utc)
    # Pine: "Weekend End Exit" -- Monday 00:00 UTC, close everything regardless of TP/SL.
    weekend_end_exit = now.weekday() == 0 and now.hour == 0

    for position in store.open_positions():
        # --- Live positions: the VENUE owns the exit ---------------------
        # Bitget holds this position's stop and target and fires them the
        # moment price touches. This loop runs every 300s, so by the time we
        # look, a live exit has usually ALREADY happened -- the job here is
        # to discover it and record the venue's real PnL, not to decide it.
        if position.exchange_order_id:
            try:
                closed = execution.reconcile_closed(position)
            except BrokerError as exc:
                print(f"[weekend_hunter] reconcile failed for {position.id}: {exc}", flush=True)
                continue
            if closed is not None:
                for field, value in closed.items():
                    setattr(position, field, value)
                _finalize(position, notifier)
                print(
                    f"[weekend_hunter] venue closed {position.symbol} as {position.status} "
                    f"@ {position.exit_price} pnl={position.pnl_usdt:+.2f} USDT", flush=True,
                )
                continue

        try:
            df = fetch_ohlcv(position.symbol, EXEC_TIMEFRAME, 60)
        except Exception as exc:  # noqa: BLE001 - one bad price check must not kill the loop
            print(f"[weekend_hunter] price check failed for {position.id}: {exc}", flush=True)
            continue
        if df.empty:
            continue
        bar = df.iloc[-1]
        is_long = position.direction == "bullish"

        if wh.USE_TRAILING:
            new_sl = wh.update_trailing_stop(position.direction, position.entry, float(bar["close"]), position.sl)
            if new_sl != position.sl:
                position.sl = new_sl
                store.update(position)
                # Push the ratchet to the venue too -- a trailing stop that
                # only exists in this loop protects nothing for the 300s
                # until the next pass.
                execution.sync_trailing_stop(position)

        current_profit_pct = ((bar["close"] - position.entry) / position.entry * 100) if is_long else \
                              ((position.entry - bar["close"]) / position.entry * 100)
        liq_distance = wh.liquidation_proximity_pct(position.leverage)
        approaching_liquidation = abs(current_profit_pct) > liq_distance
        flash_crash = len(df) > 4 and abs((df["close"].iloc[-1] - df["close"].iloc[-5]) / df["close"].iloc[-5]) > wh.FLASH_CRASH_THRESHOLD
        discretionary_exit = (
            approaching_liquidation or flash_crash
            or store.account_emergency_stop(broker_equity) or weekend_end_exit
        )

        if position.exchange_order_id:
            # Still open at the venue. TP/SL are Bitget's to fire, so only
            # the strategy's OWN exit rules (weekend end, flash crash,
            # emergency stop, liquidation proximity) act here.
            if not discretionary_exit:
                continue
            try:
                fill = execution.close_position(position)
            except BrokerError as exc:
                print(f"[weekend_hunter] FAILED to close {position.symbol} at venue: {exc}", flush=True)
                execution.halt(f"could not close {position.symbol}: {exc}")
                continue
            # Read the outcome back from the venue rather than modeling it.
            closed = execution.reconcile_closed(position)
            if closed is None:
                print(f"[weekend_hunter] {position.symbol} closed @ {fill.avg_price}, "
                      f"awaiting venue history for final PnL", flush=True)
                continue
            for field, value in closed.items():
                setattr(position, field, value)
            position.status = "time"  # our decision, not a TP/SL trigger
            _finalize(position, notifier)
            continue

        # --- Paper positions: model the exit as before -------------------
        hit_tp = bar["high"] >= position.tp if is_long else bar["low"] <= position.tp
        hit_sl = bar["low"] <= position.sl if is_long else bar["high"] >= position.sl

        status, exit_price = None, None
        if hit_sl:
            status, exit_price = "sl", position.sl
        elif hit_tp:
            status, exit_price = "tp", position.tp
        elif discretionary_exit:
            status, exit_price = ("sl" if current_profit_pct < 0 else "tp"), float(bar["close"])

        if status is None:
            continue

        raw_move = (exit_price / position.entry - 1) * (1 if is_long else -1)
        fees = 2 * (TAKER_FEE_PCT + SLIPPAGE_PCT) * position.leverage
        pnl_pct = position.leverage * raw_move - fees
        position.status = status
        position.exit_price = exit_price
        position.closed_at = datetime.now(timezone.utc).isoformat()
        position.pnl_pct_of_margin = pnl_pct
        position.pnl_usdt = position.margin_usdt * pnl_pct
        _finalize(position, notifier)


def run_once(notifier: tg.TelegramNotifier) -> None:
    # One equity/margin read per pass, shared by the exit checks and every
    # symbol's risk gate below. Live, these replace the paper wallet
    # arithmetic entirely; None means paper and the store falls back to its
    # own equity curve.
    broker_equity = broker_available = None
    if execution.trading_mode() == execution.MODE_LIVE:
        try:
            broker = execution.get_broker()
            broker_equity = broker.account_equity_usdt()
            broker_available = broker.available_usdt()
            store.record_live_baseline(broker_equity)
        except BrokerError as exc:
            # Without a reliable equity figure the risk gates cannot be
            # trusted, so this pass opens nothing. Exits still run below --
            # and the venue's own stops are unaffected regardless.
            print(f"[weekend_hunter] account read failed, opening nothing this pass: {exc}", flush=True)
            check_open_positions(notifier, None)
            return

    check_open_positions(notifier, broker_equity)
    now = datetime.now(timezone.utc)

    for symbol in SYMBOLS:
        try:
            df_exec = fetch_ohlcv(symbol, EXEC_TIMEFRAME, 300)
            df_4h = fetch_ohlcv(symbol, HTF_4H, 100)
            df_1h = fetch_ohlcv(symbol, HTF_1H, 100)
        except Exception as exc:  # noqa: BLE001
            print(f"[weekend_hunter] scan failed for {symbol}: {exc}", flush=True)
            continue
        if len(df_exec) < 60 or len(df_4h) < 55 or len(df_1h) < 35:
            continue

        try:
            signal = wh.generate_signal(df_exec, df_4h, df_1h, symbol, now)
        except Exception as exc:  # noqa: BLE001
            print(f"[weekend_hunter] signal generation failed for {symbol}: {exc}", flush=True)
            continue
        if signal is None:
            continue

        dedup_key = f"{symbol}:{signal.direction}"
        bar_id = str(df_exec.index[-1])
        state = _load_dedup_state()
        if state.get(dedup_key) == bar_id:
            continue  # already evaluated this exact bar
        state[dedup_key] = bar_id
        _save_dedup_state(state)

        atr_value = compute_atr(df_exec, period=wh.ATR_PERIOD).iloc[-1]
        signal_confidence = abs(signal.signal_strength) / 100
        tp, sl = wh.compute_levels(signal.direction, signal.entry_price, atr_value, signal.final_leverage, signal_confidence)
        position_usd = wh.position_size_usd(signal, symbol, store.capital_scale())

        skip_reason = store.risk_gate_blocked(symbol, broker_equity, broker_available)
        if skip_reason is not None:
            print(f"[weekend_hunter] {symbol}: signal found but not opened -- {skip_reason}", flush=True)
            continue

        # Pine sizes in contracts (position_usd / close). This store tracks USDT
        # MARGIN, and the leveraged interpretation is the one the whole system is
        # calibrated to: PnL below is leverage * raw_move, and the daily/weekend/
        # drawdown gates in positions.py are sized against the dollar swings that
        # produces. So position_usd is the margin posted, and real exposure is
        # position_usd * final_leverage.
        opened_at = datetime.now(timezone.utc).isoformat()
        position = Position(
            id=f"{symbol}:{opened_at}",
            symbol=symbol, platform=PLATFORM, timeframe=EXEC_TIMEFRAME, pattern="weekend_hunter_faithful_port",
            direction=signal.direction, confidence=signal_confidence, entry=signal.entry_price,
            tp=tp, sl=sl, leverage=int(round(signal.final_leverage)), margin_usdt=position_usd,
            message_id=None, chat_id=notifier.chat_id,
            opened_at=opened_at,
            original_sl=sl,
        )

        # Send the order BEFORE recording or announcing it. A rejected order
        # that had already been written to the store would leave the risk
        # gates blocking on exposure that does not exist, and would tell you
        # on Telegram that you are in a trade you are not in.
        try:
            fill = execution.open_position(position)
        except BrokerError as exc:
            print(f"[weekend_hunter] {symbol}: order REJECTED, no position opened -- {exc}", flush=True)
            try:
                notifier.send(
                    f"⚠️ <b>[Weekend Hunter]</b> {symbol} — order rejected\n\n"
                    f"<pre>{exc}</pre>\n\nNo position was opened."
                )
            except Exception:  # noqa: BLE001 - a failed alert must not also lose the log line above
                pass
            continue

        if fill is not None:
            # The real fill, not the price the signal was computed at.
            position.exchange_order_id = fill.order_id
            position.exchange_fill_price = fill.avg_price
            position.exchange_size_base = fill.size_base
            position.entry = fill.avg_price

        position.message_id = notifier.send_tracked(
            format_signal_message(symbol, signal, tp, sl, position_usd, fill)
        )
        store.add(position)
        print(f"[weekend_hunter] opened {symbol} {signal.direction} margin={position_usd:.2f} USDT "
              f"leverage={signal.final_leverage}x mode={execution.trading_mode()}", flush=True)


def main() -> None:
    notifier = tg.TelegramNotifier()
    mode = execution.trading_mode()
    wallet_desc = f"paper wallet {store.WALLET_USDT} USDT"
    if mode == execution.MODE_LIVE:
        try:
            equity = execution.get_broker().account_equity_usdt()
            wallet_desc = f"LIVE Bitget account, equity {equity:.2f} USDT"
        except BrokerError as exc:
            # Refuse to start live on an account we cannot read. Starting
            # anyway would fall back to the paper wallet's 500 USDT and size
            # real orders against a number with no connection to reality.
            raise SystemExit(f"[weekend_hunter] POLAR_LIVE_TRADING is on but Bitget is unreachable: {exc}")
    print(f"[weekend_hunter] starting -- {SYMBOLS} on {EXEC_TIMEFRAME}, {wallet_desc}, "
          f"mode={mode}, halted={execution.halted()}, "
          f"ENABLE_WEEKEND_ONLY={wh.ENABLE_WEEKEND_ONLY}", flush=True)
    while True:
        try:
            run_once(notifier)
        except Exception as exc:  # noqa: BLE001 - one bad pass must never kill this long-running process
            print(f"[weekend_hunter] run_once failed: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
