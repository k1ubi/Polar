#!/usr/bin/env python3
"""VPA's live loop: scans a market-cap-filtered universe for setups, opens
paper positions, manages/closes them, pushes Telegram alerts.

Own strategy logic (Core.vpa.analysis), own wallet/position store
(positions.py), own risk parameters -- shares only genuinely generic
infrastructure: `Core.Library.candlestick_trend.backtest` for OHLCV fetch
and universe selection, and `Core.Library.telegram.TelegramNotifier` for
delivery.

    python3 -m Core.vpa.daemon

Universe: every USDT spot pair on Bitget with a CoinGecko market cap above
$50M, excluding meme/joke coins and stablecoin/gold-pegged tokens (see
Doc/Library/shared_infrastructure.md) -- Lorenzo's own instruction, not a
book requirement (Coulling's method is instrument-agnostic). Refetching
this is CoinGecko-rate-limited and slow (minutes, not seconds), so it's
cached to disk and only refreshed once a day, not every poll.

Timeframes: Ch11's three-timeframe frame, `analyse_multi_timeframe`'s own
design -- fetch only the FAST chart (15M, early warning) and let it resample
internally to the middle (1H, "my primary trading chart") and slow (4H,
trend bias) charts, rather than fetching three separate series that could
drift out of alignment with each other.

Scanning ~190 pairs takes real CPU time (isolated_pivots/congestion_zones
are per-bar Python loops, not vectorised -- ~4s/symbol measured on this
machine), so this uses a process pool (not threads -- the GIL means threads
barely help here) to keep a full cycle well under POLL_SECONDS.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from Core.Library import telegram as tg
from Core.Library.candlestick_trend.backtest import fetch_ohlcv, top_n_crypto_usdt_pairs
from Core.Library.positions import Position
from Core.vpa import positions as store
from Core.vpa.analysis import MultiTimeframeRead, VpaSetup, analyse_multi_timeframe

FAST_TIMEFRAME = "15min"  # Ch11: the early-warning chart
MIDDLE_RULE = "1h"  # Ch11: "my primary trading chart is the middle time frame"
SLOW_RULE = "4h"  # Ch11: sets the higher-timeframe bias
FETCH_LIMIT = 1000  # Bitget's own per-request max (BITGET_MAX_CANDLES)
POLL_SECONDS = 900  # 15 min -- a few polls per 1H bar close without hammering ~190 pairs every 5 min
PLATFORM = "bitget"
SCAN_WORKERS = min(4, os.cpu_count() or 4)

MIN_MARKET_CAP_USD = 50_000_000  # Lorenzo's own instruction, not a book requirement
COINGECKO_PAGES = 4
MEME_COINGECKO_PAGES = 3
UNIVERSE_REFRESH_HOURS = 24

DATA_DIR = Path(__file__).resolve().parent / "data"
UNIVERSE_CACHE_PATH = DATA_DIR / "universe_cache.json"
DEDUP_STATE_PATH = DATA_DIR / "dedup.json"
PENDING_PATH = DATA_DIR / "pending.json"

# Fee/slippage modeling on paper-tracked exits -- matches this project's
# earlier-established defaults (see Core/weekend_hunter/daemon.py).
TAKER_FEE_PCT = 0.0006
SLIPPAGE_PCT = 0.0003


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def get_universe() -> list[str]:
    """Cached universe, refreshed once a day -- see the module docstring
    for why this can't run every poll."""
    cache = _load_json(UNIVERSE_CACHE_PATH)
    fetched_at = cache.get("fetched_at")
    if fetched_at is not None:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        if age < timedelta(hours=UNIVERSE_REFRESH_HOURS):
            return cache["symbols"]

    print("[vpa] refreshing universe from CoinGecko/Bitget (this takes a few minutes)...", flush=True)
    symbols = top_n_crypto_usdt_pairs(
        n=None, min_market_cap=MIN_MARKET_CAP_USD, coingecko_pages=COINGECKO_PAGES,
        exclude_meme_coins=True, meme_coingecko_pages=MEME_COINGECKO_PAGES,
    )
    _save_json(UNIVERSE_CACHE_PATH, {"fetched_at": datetime.now(timezone.utc).isoformat(), "symbols": symbols})
    print(f"[vpa] universe refreshed: {len(symbols)} pairs", flush=True)
    return symbols


def _scan_symbol(symbol: str) -> tuple[str, MultiTimeframeRead | None, str | None]:
    """Top-level (picklable) worker for the process pool: fetch + analyse
    one symbol. Returns (symbol, result, error) -- never raises, so one bad
    symbol can't take down the whole pool."""
    try:
        df = fetch_ohlcv(symbol, FAST_TIMEFRAME, FETCH_LIMIT)
        if len(df) < 120:  # not enough bars for a meaningful slow-timeframe read
            return symbol, None, "insufficient history"
        mtf = analyse_multi_timeframe(df, middle_rule=MIDDLE_RULE, slow_rule=SLOW_RULE)
        return symbol, mtf, None
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the pool
        return symbol, None, str(exc)


def format_signal_message(symbol: str, setup: VpaSetup, margin_usd: float) -> str:
    is_long = setup.direction == "long"
    side_emoji = "🟢" if is_long else "🔴"
    side_label = "LONG" if is_long else "SHORT"
    hold_hours = setup.expected_bars  # middle timeframe is 1H, so bars == hours
    rows = [
        ("Market", f"{PLATFORM}, {MIDDLE_RULE} (VPA multi-timeframe)"),
        ("Entry", tg.fmt_price(setup.entry_price)),
        ("Stop", f"{tg.fmt_price(setup.stop_price)} ({setup.stop_basis})"),
        ("Class", setup.trade_class.replace("_", "-")),
        ("Conviction", f"{setup.conviction:.0%}"),
        ("Expected hold", f"~{hold_hours}h (Ch11 cause/effect, no fixed target)"),
        ("Position", f"{margin_usd:.2f} USDT (unleveraged)"),
    ]
    rationale = "; ".join(setup.rationale[:4]) or "no rationale recorded"
    return (
        f"📊 <b>[VPA]</b> {side_emoji} <b>{symbol}</b> — {side_label}\n\n"
        f"{tg.row_block(rows)}\n\n"
        f"<i>{rationale}</i>\n\n"
        f"<i>Signal only — no order placed. Separate test track, isolated wallet.</i>"
    )


def format_closed_message(position: Position) -> str:
    is_win = (position.pnl_usdt or 0) >= 0
    banner = "🟩🟩🟩 CLOSED IN PROFIT 🟩🟩🟩" if is_win else "🟥🟥🟥 CLOSED AT A LOSS 🟥🟥🟥"
    side_label = "LONG" if position.direction == "bullish" else "SHORT"
    reason = {"sl": "stop hit", "time": "cause/effect duration expired"}.get(position.status, position.status)
    rows = [
        ("Entry", tg.fmt_price(position.entry)),
        ("Exit", tg.fmt_price(position.exit_price)),
        ("Reason", reason),
        ("PnL", f"{position.pnl_usdt:+.2f} USDT"),
    ]
    return (
        f"📊 <b>[VPA]</b> {banner}\n"
        f"<b>{position.symbol}</b> — {side_label}\n\n"
        f"{tg.row_block(rows)}\n\n"
        f"<i>Opened {tg.fmt_time(position.opened_at)} — closed {tg.fmt_time(position.closed_at)}</i>"
    )


def check_open_positions(notifier: tg.TelegramNotifier) -> None:
    now = datetime.now(timezone.utc)
    for position in store.open_positions():
        try:
            df = fetch_ohlcv(position.symbol, MIDDLE_RULE, 5)
        except Exception as exc:  # noqa: BLE001 - one bad price check must not kill the loop
            print(f"[vpa] price check failed for {position.id}: {exc}", flush=True)
            continue
        if df.empty:
            continue
        bar = df.iloc[-1]
        is_long = position.direction == "bullish"

        hit_sl = bar["low"] <= position.sl if is_long else bar["high"] >= position.sl
        expired = position.expires_at is not None and now >= datetime.fromisoformat(position.expires_at)

        status, exit_price = None, None
        if hit_sl:
            status, exit_price = "sl", position.sl
        elif expired:
            status, exit_price = "time", float(bar["close"])

        if status is None:
            continue

        raw_move = (exit_price / position.entry - 1) * (1 if is_long else -1)
        fees = 2 * (TAKER_FEE_PCT + SLIPPAGE_PCT)  # no leverage -- fees apply once, not leverage-scaled
        pnl_pct = raw_move - fees
        position.status = status
        position.exit_price = exit_price
        position.closed_at = now.isoformat()
        position.pnl_pct_of_margin = pnl_pct
        position.pnl_usdt = position.margin_usdt * pnl_pct
        store.update(position)

        if position.message_id is not None:
            try:
                notifier.edit_message(position.message_id, format_closed_message(position))
            except Exception as exc:  # noqa: BLE001
                print(f"[vpa] failed to edit message for {position.id}: {exc}", flush=True)


def _latest_setup(mtf: MultiTimeframeRead) -> VpaSetup | None:
    """A setup is only actionable NOW if it entered on the most recently
    closed middle-timeframe bar -- anything earlier already had its entry
    bar's open pass by and isn't tradeable at today's price."""
    if not mtf.setups:
        return None
    last = mtf.setups[-1]
    return last if last.bar_index == len(mtf.middle) - 1 else None


def _update_pending(pending: dict, symbol: str, mtf: MultiTimeframeRead) -> None:
    """A 'pending' read: the final middle-timeframe bar has a qualifying
    bias/conviction but, being the last bar, has no next bar yet to confirm
    it (Ch11 Principle No 2 -- never act on the signal bar itself). This is
    exactly the concept Core/weekend_hunter/telegram_bot.py's /pending
    command was built to show once VPA went live."""
    if not mtf.middle:
        pending.pop(symbol, None)
        return
    read = mtf.middle[-1]
    if read.bias == "neutral" or read.conviction < 0.35:
        pending.pop(symbol, None)
        return
    pending[symbol] = {
        "bias": read.bias,
        "conviction": read.conviction,
        "timeframe": MIDDLE_RULE,
        "timestamp": read.timestamp.isoformat(),
        "rationale": [f"{s.name}: {s.detail}" for s in read.signals if s.bias != "neutral"][:4],
    }


def run_once(notifier: tg.TelegramNotifier) -> None:
    check_open_positions(notifier)
    symbols = get_universe()
    dedup_state = _load_json(DEDUP_STATE_PATH)
    pending = _load_json(PENDING_PATH)

    with ProcessPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for symbol, mtf, error in pool.map(_scan_symbol, symbols):
            if error is not None:
                print(f"[vpa] scan failed for {symbol}: {error}", flush=True)
                continue

            _update_pending(pending, symbol, mtf)

            setup = _latest_setup(mtf)
            if setup is None:
                continue

            dedup_key = f"{symbol}:{setup.direction}"
            if dedup_state.get(dedup_key) == setup.timestamp.isoformat():
                continue  # already evaluated this exact bar
            dedup_state[dedup_key] = setup.timestamp.isoformat()

            margin_usd = store.position_size_usd(setup.risk_fraction)
            skip_reason = store.risk_gate_blocked(symbol, margin_usd)
            if skip_reason is not None:
                print(f"[vpa] {symbol}: setup found but not opened -- {skip_reason}", flush=True)
                continue

            message = format_signal_message(symbol, setup, margin_usd)
            message_id = notifier.send_tracked(message)

            expires_at = (setup.timestamp + timedelta(hours=setup.expected_bars)).isoformat()
            store.add(Position(
                id=f"{symbol}:{datetime.now(timezone.utc).isoformat()}",
                symbol=symbol, platform=PLATFORM, timeframe=MIDDLE_RULE, pattern="vpa_multi_timeframe",
                direction="bullish" if setup.direction == "long" else "bearish",
                confidence=setup.conviction, entry=setup.entry_price,
                tp=None, sl=setup.stop_price, leverage=1, margin_usdt=margin_usd,
                message_id=message_id, chat_id=notifier.chat_id,
                opened_at=datetime.now(timezone.utc).isoformat(),
                original_sl=setup.stop_price, expires_at=expires_at,
            ))
            print(f"[vpa] opened {symbol} {setup.direction} margin={margin_usd:.2f} USDT "
                  f"conviction={setup.conviction:.0%}", flush=True)

    _save_json(DEDUP_STATE_PATH, dedup_state)
    _save_json(PENDING_PATH, pending)


def main() -> None:
    notifier = tg.TelegramNotifier()
    universe = get_universe()
    print(f"[vpa] starting -- {len(universe)} pairs (mcap > ${MIN_MARKET_CAP_USD:,.0f}, meme-excluded), "
          f"wallet {store.WALLET_USDT} USDT, {SCAN_WORKERS} scan workers", flush=True)
    while True:
        try:
            run_once(notifier)
        except Exception as exc:  # noqa: BLE001 - one bad pass must never kill this long-running process
            print(f"[vpa] run_once failed: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
