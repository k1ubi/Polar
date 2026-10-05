"""VPA daemon — Coulling book-rule volume-validated breakouts on daily bars.

Strategy (ch. 7 & 11, A Complete Guide To Volume Price Analysis, 2013):
  congestion   prior ZONE daily bars: range ≤ 4×ATR14 and mean volume < 50-bar average
  breakout     wide-spread bar (high-low ≥ 1 ATR) with close beyond zone ≥ 0.25 ATR
  validation   breakout-bar volume ≥ 1.5 × 20-bar average
  entry        next bar's open (entered when daily_scan refreshes the next day)
  stop         opposite side of the congestion zone
  target       2 × zone height from the breakout level
  time stop    MAX_HOLD bars

Pre-registered research result: Research/vpa_book_breakout.py

Run via: python3 -m Core.vpa.daemon
Triggered by polar-vpa.service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from Core.Library import telegram as tg
from Core.Library.paper_fills import pnl_usdt, resolve_exit
from Core.Library.risk import (
    MAX_CONSECUTIVE_LOSSES,
    is_in_cooldown,
    loss_streak_cooldown_until,
    position_size_usd,
)
from Core.Library.settings import get_settings
from Core.vpa import positions as store

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ── strategy constants (from Research/vpa_book_breakout.py) ─────────────────
ZONE = 20           # congestion zone lookback (bars)
MAX_ZONE_ATR = 4.0  # zone range ≤ 4 × ATR14 to be congested
BREAK_ATR = 0.25    # close must clear zone by ≥ 0.25 ATR
WIDE_ATR = 1.0      # breakout bar must have high-low ≥ 1 ATR
VOL_MULT = 1.5      # breakout volume ≥ 1.5 × 20-bar average
TARGET_MULT = 2.0   # target = 2 × zone height from breakout level
MAX_HOLD = 30       # time stop (bars)
LEVERAGE = 3.0

# ── paths ───────────────────────────────────────────────────────────────────
_DATA = Path(__file__).parent / "data"
_CACHE = _DATA / "ohlcv_cache.json"
_BASELINE = _DATA / "live_baseline.json"
_PENDING = _DATA / "pending_signals.json"

# ── timing ───────────────────────────────────────────────────────────────────
POLL_SECONDS = 120   # how often to check for a new day's bars
MAX_OPEN = 4         # max simultaneous open positions (capital management)


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_cache() -> tuple[str, dict[str, list]]:
    """Load ohlcv_cache. Returns (fetched_at ISO string, bars_by_symbol)."""
    try:
        d = json.loads(_CACHE.read_text())
        return d.get("fetched_at", ""), d.get("bars", {})
    except Exception:
        return "", {}


def _load_pending() -> list[dict]:
    try:
        return json.loads(_PENDING.read_text())
    except Exception:
        return []


def _save_pending(signals: list[dict]) -> None:
    _PENDING.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PENDING.with_suffix(".tmp")
    tmp.write_text(json.dumps(signals, indent=2, default=str))
    tmp.replace(_PENDING)


def _load_baseline() -> float:
    try:
        return json.loads(_BASELINE.read_text()).get("initial_equity_usdt", 200.0)
    except Exception:
        return 200.0


def _equity() -> float:
    """Estimate current equity: baseline + sum of closed PnL."""
    baseline = _load_baseline()
    return baseline + store.realized_pnl_usdt()


def _drop_forming_bar(a: np.ndarray) -> np.ndarray:
    """Drop the last bar if it is today's UTC daily bar (still forming).

    daily_scan.py runs at ~02:00 UTC, which is 2 hours into the current UTC day.
    Bitget includes the forming bar in its API response. An incomplete bar has
    wrong volume, ATR, and spread — must not be used for signal detection.
    """
    if len(a) == 0:
        return a
    today_open_ms = int(
        datetime.now(tz=timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )
    if int(a[-1, 0]) >= today_open_ms:
        return a[:-1]
    return a


def _symbol_bars(bars_all: dict, symbol: str) -> np.ndarray | None:
    """Load a symbol's bars as a float array (ts, o, h, l, c, base_vol, quote_vol).

    Drops the forming bar when daily_scan ran before today's bar closed.
    """
    rows = bars_all.get(symbol)
    if not rows:
        return None
    a = _drop_forming_bar(np.array(rows, dtype=float))
    if len(a) < ZONE + 50 + 2:
        return None
    return a


def _atr14(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(
        np.stack([h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1), axis=1
    )
    return pd.Series(tr).rolling(14).mean().to_numpy()


def _scan_setup(symbol: str, a: np.ndarray) -> dict | None:
    """Detect a book-rule breakout at the most recently closed bar (index -1).

    Returns a setup dict or None.
    """
    ts, o, h, l, c, bv = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]

    atr = _atr14(h, l, c)
    # Volume averages shifted by 1 so the current bar is not included
    vavg20 = pd.Series(bv).rolling(20).mean().shift(1).to_numpy()
    vavg50 = pd.Series(bv).rolling(50).mean().shift(1).to_numpy()

    i = len(c) - 1
    a_ = atr[i - 1]  # ATR as of the bar before the signal bar

    if not (a_ > 0) or bv[i] <= 0 or not (vavg50[i - ZONE] > 0):
        return None

    lo = i - ZONE
    ceil = h[lo:i].max()
    floor = l[lo:i].min()

    congested = (ceil - floor) <= MAX_ZONE_ATR * a_ and np.mean(bv[lo:i]) < vavg50[i - 1]
    if not congested:
        return None

    wide = (h[i] - l[i]) >= WIDE_ATR * a_
    validated = bv[i] >= VOL_MULT * vavg20[i]
    if not (wide and validated):
        return None

    if c[i] >= ceil + BREAK_ATR * a_:
        direction = "bullish"
        stop = float(floor)
        target = float(ceil + TARGET_MULT * (ceil - floor))
    elif c[i] <= floor - BREAK_ATR * a_:
        direction = "bearish"
        stop = float(ceil)
        target = float(floor - TARGET_MULT * (ceil - floor))
    else:
        return None

    risk_pct = abs(c[i] - stop) / c[i]
    if risk_pct <= 0:
        return None

    return {
        "id": str(uuid.uuid4()),
        "symbol": symbol,
        "direction": direction,
        "signal_bar_ts": int(ts[i]),
        "signal_close": float(c[i]),
        "stop": stop,
        "target": target,
        "risk_pct": float(risk_pct),
    }


def _bars_since(a: np.ndarray, entry_ts: int) -> list:
    """Return bars after the entry bar (for exit resolution)."""
    ts = a[:, 0].astype(int)
    after = np.searchsorted(ts, entry_ts, side="right")
    rows = a[after:]
    # shape: (ts, open, high, low, close, ...)
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def _time_stop_bar(entry_ts: int, a: np.ndarray) -> int:
    """How many bars have elapsed since entry."""
    ts = a[:, 0].astype(int)
    after = np.searchsorted(ts, entry_ts, side="right")
    return len(a) - after


# ── position management ───────────────────────────────────────────────────────

def _check_exits(bars_all: dict[str, list]) -> list[dict]:
    """Resolve exits for all open positions against the latest bars.

    Returns the list of positions that closed this cycle.
    """
    closed_this_cycle: list[dict] = []
    open_positions = store.get_open()
    if not open_positions:
        return []

    for pos in open_positions:
        symbol = pos["symbol"]
        a = _symbol_bars(bars_all, symbol)
        if a is None:
            continue

        bars_after = _bars_since(a, int(pos["entry_ts"]))
        n_bars = _time_stop_bar(int(pos["entry_ts"]), a)

        exit_result = resolve_exit(
            pos["direction"], pos["stop"], pos["target"], bars_after
        )

        should_close = exit_result is not None or n_bars >= MAX_HOLD
        if not should_close:
            continue

        if exit_result is not None:
            exit_price = exit_result.price
            exit_reason = exit_result.status
        else:
            # Time stop — exit at close of last bar
            exit_price = float(a[-1, 4])
            exit_reason = "time_stop"

        pnl = pnl_usdt(
            pos["direction"],
            pos["entry"],
            exit_price,
            pos["margin_usdt"],
            LEVERAGE,
        )

        updated = dict(pos)
        updated.update(
            status="closed",
            exit_price=exit_price,
            exit_reason=exit_reason,
            closed_at=datetime.now(tz=timezone.utc).isoformat(),
            pnl_usdt=round(pnl, 4),
        )
        store.upsert(updated)
        closed_this_cycle.append(updated)

    return closed_this_cycle


def _open_pending(bars_all: dict[str, list]) -> list[dict]:
    """Convert yesterday's pending signals into open positions using today's bar open.

    Returns newly opened positions.
    """
    pending = _load_pending()
    if not pending:
        return []

    opened = []
    open_symbols = {p["symbol"] for p in store.get_open()}
    equity = _equity()

    remaining: list[dict] = []
    for sig in pending:
        symbol = sig["symbol"]
        a = _symbol_bars(bars_all, symbol)
        if a is None:
            remaining.append(sig)
            continue

        # Find the bar immediately after the signal bar
        ts = a[:, 0].astype(int)
        after = np.searchsorted(ts, int(sig["signal_bar_ts"]), side="right")
        if after >= len(a):
            # Entry bar hasn't appeared yet — keep pending
            remaining.append(sig)
            continue

        if symbol in open_symbols:
            # Already have a position in this symbol — skip
            continue

        entry_price = float(a[after, 1])  # open of the entry bar
        entry_ts = int(a[after, 0])

        size = position_size_usd(equity, sig["risk_pct"], LEVERAGE, baseline_usdt=_load_baseline())
        if size <= 0:
            continue

        pos = {
            "id": sig["id"],
            "symbol": symbol,
            "direction": sig["direction"],
            "entry": entry_price,
            "entry_ts": entry_ts,
            "stop": sig["stop"],
            "target": sig["target"],
            "margin_usdt": round(size / LEVERAGE, 4),
            "leverage": LEVERAGE,
            "status": "open",
            "opened_at": datetime.now(tz=timezone.utc).isoformat(),
            "pnl_usdt": None,
        }
        store.upsert(pos)
        open_symbols.add(symbol)
        opened.append(pos)

    _save_pending(remaining)
    return opened


def _scan_new_signals(bars_all: dict[str, list]) -> list[dict]:
    """Scan all symbols for new breakout setups and add them to pending."""
    open_symbols = {p["symbol"] for p in store.get_open()}
    pending_symbols = {s["symbol"] for s in _load_pending()}

    if is_in_cooldown(store.POSITIONS_PATH):
        logger.info("[vpa] in loss-streak cooldown — skipping new signals")
        return []

    open_count = len(open_symbols)
    if open_count >= MAX_OPEN:
        logger.info("[vpa] at max positions (%d) — skipping new signals", MAX_OPEN)
        return []

    new_signals: list[dict] = []
    for symbol, _ in bars_all.items():
        if symbol in open_symbols or symbol in pending_symbols:
            continue
        a = _symbol_bars(bars_all, symbol)
        if a is None:
            continue
        setup = _scan_setup(symbol, a)
        if setup is not None:
            new_signals.append(setup)
            logger.info(
                "[vpa] signal: %s %s  close=%.4f  stop=%.4f  target=%.4f",
                symbol, setup["direction"], setup["signal_close"], setup["stop"], setup["target"],
            )
            if len(open_symbols) + len(new_signals) >= MAX_OPEN:
                break

    if new_signals:
        existing = _load_pending()
        existing.extend(new_signals)
        _save_pending(existing)

    return new_signals


# ── Telegram notifications ───────────────────────────────────────────────────

async def _notify_opened(session, settings, pos: dict) -> None:
    direction = "🟢 LONG" if pos["direction"] == "bullish" else "🔴 SHORT"
    text = (
        f"{direction} <b>{pos['symbol']}</b>\n"
        f"Entry: <code>{pos['entry']:.4f}</code>  "
        f"Stop: <code>{pos['stop']:.4f}</code>  "
        f"Target: <code>{pos['target']:.4f}</code>\n"
        f"Margin: <code>{pos['margin_usdt']:.2f} USDT</code> × {int(LEVERAGE)}x  "
        f"[PAPER]"
    )
    await tg.send_message(session, settings.telegram_bot_token, settings.telegram_admin_chat_id, text)


async def _notify_closed(session, settings, pos: dict) -> None:
    pnl = pos.get("pnl_usdt", 0) or 0
    emoji = "✅" if pnl >= 0 else "❌"
    text = (
        f"{emoji} <b>{pos['symbol']}</b> {pos['direction'].upper()} closed\n"
        f"Exit: <code>{pos['exit_price']:.4f}</code>  "
        f"Reason: {pos['exit_reason']}\n"
        f"PnL: <code>{pnl:+.2f} USDT</code>"
    )
    await tg.send_message(session, settings.telegram_bot_token, settings.telegram_admin_chat_id, text)


# ── main loop ────────────────────────────────────────────────────────────────

async def run() -> None:
    settings = get_settings()
    last_cache_ts = ""

    async with __import__("aiohttp").ClientSession() as session:
        while True:
            try:
                fetched_at, bars_all = _load_cache()

                open_pos = store.get_open()
                n_open = len(open_pos)
                n_closed = len(store.get_closed())
                equity = _equity()

                if fetched_at != last_cache_ts:
                    # New day's bars available — run full cycle
                    last_cache_ts = fetched_at
                    logger.info(
                        "[vpa] new bars: %s  symbols=%d  open=%d  closed=%d  equity=%.2f USDT",
                        fetched_at[:10], len(bars_all), n_open, n_closed, equity,
                    )

                    # 1. Check exits for open positions
                    closed = _check_exits(bars_all)
                    for pos in closed:
                        logger.info(
                            "[vpa] closed %s %s @ %.4f  reason=%s  pnl=%.2f",
                            pos["symbol"], pos["direction"], pos["exit_price"],
                            pos["exit_reason"], pos.get("pnl_usdt") or 0,
                        )
                        await _notify_closed(session, settings, pos)

                    # 2. Open positions from pending signals
                    opened = _open_pending(bars_all)
                    for pos in opened:
                        logger.info(
                            "[vpa] opened %s %s @ %.4f  stop=%.4f  target=%.4f",
                            pos["symbol"], pos["direction"], pos["entry"],
                            pos["stop"], pos["target"],
                        )
                        await _notify_opened(session, settings, pos)

                    # 3. Scan for new signals
                    new_sigs = _scan_new_signals(bars_all)
                    if new_sigs:
                        logger.info("[vpa] %d new signal(s) queued for tomorrow's open", len(new_sigs))
                    else:
                        logger.info("[vpa] no new signals this bar")
                else:
                    # Same bars as before — heartbeat only
                    logger.info(
                        "[vpa] heartbeat  open=%d  closed=%d  equity=%.2f USDT",
                        n_open, n_closed, equity,
                    )

            except Exception as exc:
                logger.error("[vpa] run failed: %s", exc)
                try:
                    await tg.notify_loop_failure(
                        session, settings.telegram_bot_token,
                        settings.telegram_admin_chat_id, "VPA", exc,
                    )
                except Exception:
                    pass

            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
