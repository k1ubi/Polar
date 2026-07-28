#!/usr/bin/env python3
"""Telegram command listener — answers /positions, /report, /weekendscalper,
/pending, /statistics (and the matching persistent keyboard buttons).

Separate process from daemon.py: the daemon only ever PUSHES alerts, it
never listens; this script is the other direction (Telegram -> Polar),
long-polling Telegram's getUpdates. Run both together for the full
experience.

    python3 -m Core.weekend_hunter.telegram_bot

Read-only against Core.weekend_hunter.positions' store — never places an
order, never touches the strategy.

NOTE on /positions and /report vs /weekendscalper: Weekend Hunter is
currently the only live Core in this project, so all three read the exact
same store today (fed by Core.weekend_hunter.positions). /positions and
/report are the general-purpose commands, meant to become a combined view
once a second Core goes live and pushes into its own store; /weekendscalper
is the strategy-specific view and will keep showing Weekend Hunter only,
even then. No design decision is being deferred by this overlap — there is
just nothing else to combine yet.

No /status command here (unlike the prior codebase's version) — that
queried systemd services on a specific Pi deployment, which doesn't apply
wherever this bot actually runs. Add a Core-appropriate equivalent once
this has a fixed deployment target.
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from Core.Library import telegram as tg
from Core.Library.candlestick_trend.backtest import fetch_ohlcv
from Core.weekend_hunter import positions as store

OFFSET_PATH = Path(__file__).resolve().parent / "data" / "telegram_bot_offset.json"
MAX_POSITIONS_SHOWN = 15  # Telegram messages cap at 4096 chars


def _load_offset() -> int | None:
    if not OFFSET_PATH.exists():
        return None
    return json.loads(OFFSET_PATH.read_text()).get("offset")


def _save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps({"offset": offset}))


def _unrealized_row(position) -> tuple[str, str] | None:
    try:
        df = fetch_ohlcv(position.symbol, position.timeframe, 1)
        if df.empty:
            return None
        current = float(df["close"].iloc[-1])
        is_long = position.direction == "bullish"
        raw_move = (current / position.entry - 1) * (1 if is_long else -1)
        pnl_usdt = position.margin_usdt * position.leverage * raw_move
        pnl_pct = raw_move * position.leverage * 100
        trend = "📈" if pnl_usdt >= 0 else "📉"
        return ("Unrealized", f"{trend} {pnl_usdt:+.2f} USDT ({pnl_pct:+.1f}%)")
    except Exception:  # noqa: BLE001 - unrealized PnL is a bonus, not essential
        return None


def _position_block(p) -> str:
    side = "LONG" if p.direction == "bullish" else "SHORT"
    rows = [
        ("Pattern", p.pattern),
        ("Confidence", f"{p.confidence:.0%}"),
        ("Market", f"{p.platform}, {p.timeframe}"),
        ("Entry", tg.fmt_price(p.entry)),
        ("TP / SL", f"{tg.fmt_price(p.tp)} / {tg.fmt_price(p.sl)}"),
        ("Margin", f"{p.margin_usdt:.2f} USDT ({p.leverage}x)"),
    ]
    unrealized = _unrealized_row(p)
    if unrealized:
        rows.append(unrealized)
    rows.append(("Opened", tg.fmt_time(p.opened_at)))
    return f"{'🟢' if p.direction == 'bullish' else '🔴'} <b>{p.symbol}</b> {side}\n{tg.row_block(rows)}"


def _closed_block(p) -> str:
    mark = "🟢" if (p.pnl_usdt or 0) >= 0 else "🔴"
    rows = [
        ("Pattern", p.pattern),
        ("Market", f"{p.platform}, {p.timeframe}"),
        ("Entry", tg.fmt_price(p.entry)),
        ("Exit", tg.fmt_price(p.exit_price) if p.exit_price is not None else "?"),
        ("PnL", f"{p.pnl_usdt:+.2f} USDT ({(p.pnl_pct_of_margin or 0):+.1%} of margin)"),
        ("Status", p.status.upper()),
        ("Closed", tg.fmt_time(p.closed_at)),
    ]
    return f"{mark} <b>{p.symbol}</b>\n{tg.row_block(rows)}"


def handle_positions() -> str:
    open_positions = store.open_positions()
    if not open_positions:
        return "No open positions right now."

    shown = open_positions[:MAX_POSITIONS_SHOWN]
    total_unrealized = 0.0
    for p in shown:
        row = _unrealized_row(p)
        if row:
            total_unrealized += float(row[1].split()[1])

    lines = [f"<b>{len(open_positions)} open position(s)</b>"]
    if len(open_positions) > MAX_POSITIONS_SHOWN:
        lines.append(f"(showing the first {MAX_POSITIONS_SHOWN}, use /report for the full summary)")
    lines.append(f"Unrealized PnL: {total_unrealized:+.2f} USDT")
    lines.append("")
    lines.extend(_position_block(p) for p in shown)
    return "\n\n".join(lines)


def handle_report() -> str:
    closed = store.closed_positions()
    wins = [p for p in closed if (p.pnl_usdt or 0.0) >= 0]
    losses = [p for p in closed if (p.pnl_usdt or 0.0) < 0]
    realized_pnl = sum(p.pnl_usdt or 0.0 for p in closed)
    balance = store.WALLET_USDT + realized_pnl
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0

    lines = [
        "<b>Polar report</b>",
        "",
        f"Closed trades: {len(closed)} ({len(wins)}W / {len(losses)}L, {win_rate:.1f}% win rate)",
        f"Open positions: {len(store.open_positions())}",
        f"Realized PnL: {realized_pnl:+.2f} USDT",
        f"Starting wallet: {store.WALLET_USDT:.2f} USDT",
        f"Current balance: <b>{balance:.2f} USDT</b>",
    ]
    if closed:
        lines += ["", "<i>Last 5 closed:</i>"]
        recent = sorted(closed, key=lambda p: p.closed_at or "", reverse=True)[:5]
        lines.append("\n\n".join(_closed_block(p) for p in recent))
    return "\n".join(lines)


def handle_weekend_scalper() -> str:
    """Dedicated Weekend Hunter view. See module docstring — identical data
    to /positions and /report for now, since it's the only live Core."""
    open_pos = store.open_positions()
    closed = store.closed_positions()
    wins = [p for p in closed if (p.pnl_usdt or 0.0) >= 0]
    losses = [p for p in closed if (p.pnl_usdt or 0.0) < 0]
    realized_pnl = store.total_realized_pnl()
    balance = store.WALLET_USDT + realized_pnl
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0

    lines = [
        "🎯 <b>Weekend Scalper report</b> (Weekend Hunter faithful port)",
        "",
        f"Closed trades: {len(closed)} ({len(wins)}W / {len(losses)}L, {win_rate:.1f}% win rate)",
        f"Open positions: {len(open_pos)}",
        f"Realized PnL: {realized_pnl:+.2f} USDT",
        f"Starting wallet: {store.WALLET_USDT:.2f} USDT",
        f"Current balance: <b>{balance:.2f} USDT</b>",
    ]
    if open_pos:
        lines += ["", "<i>Open:</i>"]
        lines.append("\n\n".join(_position_block(p) for p in open_pos))
    if closed:
        lines += ["", "<i>Last 5 closed:</i>"]
        recent = sorted(closed, key=lambda p: p.closed_at or "", reverse=True)[:5]
        lines.append("\n\n".join(_closed_block(p) for p in recent))
    return "\n".join(lines)


def handle_pending() -> str:
    """Weekend Hunter enters directly on signal — there is no break-and-
    retest gate (unlike the prior codebase's pattern-confluence engine), so
    there is structurally nothing to be "pending." Kept as a command (the
    prior bot had one) so the slot is ready if a future Core adds a staged
    entry, rather than silently dropped."""
    return (
        "No pending trades — Weekend Hunter enters directly on signal, "
        "there's no break-and-retest staging step for it to wait on."
    )


def handle_statistics() -> str:
    """Win rate broken down by confidence bucket. 10-point-wide buckets
    (50-59%, 60-69%, ...) rather than finer ones — sample sizes here are
    still small, and narrower buckets would mostly produce n=1 buckets that
    look like "0% or 100% win rate" without saying so honestly."""
    closed = store.closed_positions()
    if not closed:
        return "No closed trades yet -- nothing to break down by confidence."

    buckets: dict[int, list] = {}
    for p in closed:
        bucket_start = min(90, int(p.confidence * 100) // 10 * 10)
        buckets.setdefault(bucket_start, []).append(p)

    lines = ["<b>Win rate by confidence</b>", f"({len(closed)} closed trades total)", ""]
    for bucket_start in sorted(buckets):
        trades = buckets[bucket_start]
        wins = sum(1 for p in trades if (p.pnl_usdt or 0.0) >= 0)
        n = len(trades)
        win_rate = wins / n * 100
        label = f"{bucket_start}-{bucket_start + 9}%" if bucket_start < 90 else "90-100%"
        rows = [
            ("Trades", str(n)),
            ("Win rate", f"{win_rate:.0f}% ({wins}W/{n - wins}L)"),
        ]
        lines.append(f"<b>Confidence {label}</b>\n{tg.row_block(rows)}")
    lines.append("")
    lines.append("<i>Small buckets are noise, not a real result — treat them as \"not enough data yet.\"</i>")
    return "\n\n".join(lines)


COMMANDS = [
    ("positions", "Show each open signal"),
    ("report", "Win/loss trades, PnL, and current balance"),
    ("weekendscalper", "Weekend Hunter test track (own wallet)"),
    ("pending", "Signals waiting on entry confirmation"),
    ("statistics", "Win rate broken down by confidence bucket"),
]


def main() -> None:
    notifier = tg.TelegramNotifier()
    notifier.set_commands(COMMANDS)
    print(f"[polar] bot listening (chat_id={notifier.chat_id})...", flush=True)

    offset = _load_offset()
    while True:
        try:
            updates = notifier.get_updates(offset=offset, timeout=30)
        except Exception as exc:  # noqa: BLE001 - a transient network error shouldn't kill the listener
            print(f"[polar] getUpdates failed: {exc}", flush=True)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            _save_offset(offset)
            message = update.get("message")
            if not message or "text" not in message:
                continue
            text = message["text"].strip()
            print(f"[polar] received: {text!r}", flush=True)

            # Every command handler is isolated so one failure just gets
            # logged, never takes the whole listener down.
            try:
                if text in ("/start",):
                    notifier.send_tracked(
                        "Polar bot connected. Use the buttons below or /positions, /report.",
                        reply_markup=tg.PERSISTENT_KEYBOARD,
                    )
                elif text in ("/positions", tg.BUTTON_POSITIONS):
                    notifier.send(handle_positions())
                elif text in ("/report", tg.BUTTON_REPORT):
                    notifier.send(handle_report())
                elif text in ("/weekendscalper", tg.BUTTON_WEEKEND_SCALPER):
                    notifier.send(handle_weekend_scalper())
                elif text in ("/pending", tg.BUTTON_PENDING):
                    notifier.send(handle_pending())
                elif text in ("/statistics", tg.BUTTON_STATISTICS):
                    notifier.send(handle_statistics())
            except Exception as exc:  # noqa: BLE001 - must never crash the listener
                print(f"[polar] failed to handle {text!r}: {exc}", flush=True)


if __name__ == "__main__":
    main()
