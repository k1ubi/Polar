#!/usr/bin/env python3
"""Telegram command listener — answers /positions, /report, /weekendhunter,
/vpa, /pending, /statistics (and the matching persistent keyboard buttons).

Separate process from daemon.py: the daemon only ever PUSHES alerts, it
never listens; this script is the other direction (Telegram -> Polar),
long-polling Telegram's getUpdates. Run both together for the full
experience.

    python3 -m Core.weekend_hunter.telegram_bot

Read-only against both Cores' stores (Core.weekend_hunter.positions and
Core.vpa.positions) — never places an order, never touches either
strategy. Lives under Core/weekend_hunter/ for historical reasons (it was
built before VPA had a live daemon) rather than a neutral location — moving
it is a bigger, undocumented restructuring this change doesn't need to make.

NOTE on /positions and /report vs /weekendhunter and /vpa: /positions and
/report are the general-purpose, COMBINED commands (both Cores' open
positions and closed-trade stats together, since only one Telegram bot
process exists for both); /weekendhunter and /vpa are the strategy-specific
views, each showing only its own Core's store. This is exactly what this
module's own prior docstring anticipated before VPA went live: "meant to
become a combined view once a second Core goes live and pushes into its
own store."

/status queries `systemctl --user` for this deployment's own services
(see the unit files under ~/.config/systemd/user/), not a generic Pi-wide
service list.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from Core.Library import telegram as tg
from Core.Library.candlestick_trend.backtest import fetch_ohlcv
from Core.vpa import positions as vpa_store
from Core.weekend_hunter import positions as store

# This deployment's own systemd --user services (see the unit files under
# ~/.config/systemd/user/) -- queried directly via `systemctl --user show`
# rather than reading anything Polar itself wrote, so a service that's
# hung or crash-looping still shows up accurately (a daemon can't
# self-report that it's stuck).
SERVICES = ["polar-weekend-hunter", "polar-weekend-hunter-bot", "polar-vpa"]

VPA_PENDING_PATH = Path(__file__).resolve().parent.parent / "vpa" / "data" / "pending.json"

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


def _tp_sl_row(p) -> tuple[str, str]:
    """VPA positions carry no fixed target (see Doc/Core/vpa.md — the book
    gives no target-price formula, only a market-derived stop and a
    cause/effect holding-period expectation), so `tp` is None there."""
    if p.tp is None:
        expiry = f", exit by {tg.fmt_time(p.expires_at)}" if p.expires_at else ""
        return ("Stop", f"{tg.fmt_price(p.sl)} (no fixed target{expiry})")
    return ("TP / SL", f"{tg.fmt_price(p.tp)} / {tg.fmt_price(p.sl)}")


def _position_block(p) -> str:
    side = "LONG" if p.direction == "bullish" else "SHORT"
    rows = [
        ("Pattern", p.pattern),
        ("Confidence", f"{p.confidence:.0%}"),
        ("Market", f"{p.platform}, {p.timeframe}"),
        ("Entry", tg.fmt_price(p.entry)),
        _tp_sl_row(p),
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
    open_positions = store.open_positions() + vpa_store.open_positions()
    if not open_positions:
        return "No open positions right now."

    open_positions.sort(key=lambda p: p.opened_at, reverse=True)
    shown = open_positions[:MAX_POSITIONS_SHOWN]
    total_unrealized = 0.0
    for p in shown:
        row = _unrealized_row(p)
        if row:
            total_unrealized += float(row[1].split()[1])

    lines = [f"<b>{len(open_positions)} open position(s)</b> (Weekend Hunter + VPA)"]
    if len(open_positions) > MAX_POSITIONS_SHOWN:
        lines.append(f"(showing the first {MAX_POSITIONS_SHOWN}, use /report for the full summary)")
    lines.append(f"Unrealized PnL: {total_unrealized:+.2f} USDT")
    lines.append("")
    lines.extend(_position_block(p) for p in shown)
    return "\n\n".join(lines)


def handle_report() -> str:
    """Combined win/loss/PnL across both Cores. Each Core keeps its own
    wallet (Weekend Hunter's $500 matches its Pine source; VPA's $200 is
    this project's usual convention — see each Core's own positions.py), so
    balances are shown per-Core rather than summed into one number that
    would silently conflate two different starting capitals."""
    wh_closed = store.closed_positions()
    vpa_closed = vpa_store.closed_positions()
    closed = wh_closed + vpa_closed
    wins = [p for p in closed if (p.pnl_usdt or 0.0) >= 0]
    losses = [p for p in closed if (p.pnl_usdt or 0.0) < 0]
    realized_pnl = sum(p.pnl_usdt or 0.0 for p in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0

    lines = [
        "<b>Polar report</b>",
        "",
        f"Closed trades: {len(closed)} ({len(wins)}W / {len(losses)}L, {win_rate:.1f}% win rate)",
        f"Open positions: {len(store.open_positions()) + len(vpa_store.open_positions())}",
        f"Realized PnL (combined): {realized_pnl:+.2f} USDT",
        "",
        f"Weekend Hunter balance: <b>{store.WALLET_USDT + store.total_realized_pnl():.2f} USDT</b> "
        f"(started {store.WALLET_USDT:.0f})",
        f"VPA balance: <b>{vpa_store.WALLET_USDT + vpa_store.total_realized_pnl():.2f} USDT</b> "
        f"(started {vpa_store.WALLET_USDT:.0f})",
    ]
    if closed:
        lines += ["", "<i>Last 5 closed:</i>"]
        recent = sorted(closed, key=lambda p: p.closed_at or "", reverse=True)[:5]
        lines.append("\n\n".join(_closed_block(p) for p in recent))
    return "\n".join(lines)


def handle_weekend_hunter() -> str:
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
        "🎯 <b>Weekend Hunter report</b> (Weekend Hunter faithful port)",
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


def handle_vpa() -> str:
    """Dedicated VPA view — analogous to /weekendhunter. VPA's own wallet,
    own positions, no fixed take-profit (see `_tp_sl_row` — the book gives
    no target-price formula, only a market-derived stop and a cause/effect
    holding-period expectation)."""
    open_pos = vpa_store.open_positions()
    closed = vpa_store.closed_positions()
    wins = [p for p in closed if (p.pnl_usdt or 0.0) >= 0]
    losses = [p for p in closed if (p.pnl_usdt or 0.0) < 0]
    realized_pnl = vpa_store.total_realized_pnl()
    balance = vpa_store.WALLET_USDT + realized_pnl
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0

    lines = [
        "📊 <b>VPA report</b> (Coulling's Volume Price Analysis, multi-timeframe)",
        "",
        f"Closed trades: {len(closed)} ({len(wins)}W / {len(losses)}L, {win_rate:.1f}% win rate)",
        f"Open positions: {len(open_pos)}",
        f"Realized PnL: {realized_pnl:+.2f} USDT",
        f"Starting wallet: {vpa_store.WALLET_USDT:.2f} USDT",
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
    """Pending trades are VPA's, not Weekend Hunter's -- Weekend Hunter
    enters directly on signal, nothing ever stages there. A pending entry
    here is the final middle-timeframe (1H) bar's read: it has a
    qualifying bias/conviction but, being the last bar, has no next bar yet
    to confirm it (Ch11 Principle No 2 -- never act on the signal bar
    itself). Written by Core/vpa/daemon.py's `_update_pending` every scan
    cycle; this command just reads that file."""
    if not VPA_PENDING_PATH.exists():
        return "No pending trades right now."
    pending = json.loads(VPA_PENDING_PATH.read_text())
    if not pending:
        return "No pending trades right now."

    lines = [f"⏳ <b>{len(pending)} pending VPA read(s)</b>", "<i>Awaiting next-bar confirmation</i>", ""]
    for symbol, info in pending.items():
        emoji = "🟢" if info["bias"] == "bullish" else "🔴"
        rows = [
            ("Bias", info["bias"].upper()),
            ("Conviction", f"{info['conviction']:.0%}"),
            ("Timeframe", info["timeframe"]),
            ("As of", tg.fmt_time(info["timestamp"])),
        ]
        block = f"{emoji} <b>{symbol}</b>\n{tg.row_block(rows)}"
        if info.get("rationale"):
            block += f"\n<i>{'; '.join(info['rationale'])}</i>"
        lines.append(block)
    return "\n\n".join(lines)


def handle_statistics() -> str:
    """Win rate broken down by confidence bucket. 10-point-wide buckets
    (50-59%, 60-69%, ...) rather than finer ones — sample sizes here are
    still small, and narrower buckets would mostly produce n=1 buckets that
    look like "0% or 100% win rate" without saying so honestly."""
    closed = store.closed_positions() + vpa_store.closed_positions()
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


def handle_status() -> str:
    """systemd --user status of this deployment's own services. Queries
    `systemctl --user` directly (not anything Polar itself wrote) so a
    hung or crash-looping daemon still shows up accurately."""
    if shutil.which("systemctl") is None:
        return "⚡️ <b>Status</b>\n\n<i>systemctl not available on this machine.</i>"

    lines = ["⚡️ <b>Service status</b>", ""]
    for svc in SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", svc,
                 "-p", "ActiveState,SubState,NRestarts,ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            props = dict(line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line)
            active, sub = props.get("ActiveState", "?"), props.get("SubState", "?")
            restarts = props.get("NRestarts", "?")
            since = props.get("ActiveEnterTimestamp", "?") or "?"
            emoji = "🟢" if active == "active" and sub == "running" else "🔴"
            restart_note = "" if restarts in ("0", "?") else f" ⚠️ {restarts} restarts"
            rows = [
                ("State", f"{active}/{sub}"),
                ("Restarts", f"{restarts}{restart_note}"),
                ("Since", since),
            ]
            lines.append(f"{emoji} <b>{svc}</b>\n{tg.row_block(rows)}")
        except Exception as exc:  # noqa: BLE001 - one bad service query must not blank the whole reply
            lines.append(f"⚠️ <b>{svc}</b>\n{tg.row_block([('Error', str(exc))])}")
    return "\n\n".join(lines)


COMMANDS = [
    ("positions", "Show each open signal (both Cores)"),
    ("report", "Win/loss trades, PnL, and balances (both Cores)"),
    ("weekendhunter", "Weekend Hunter test track (own wallet)"),
    ("vpa", "VPA test track (own wallet)"),
    ("pending", "Signals waiting on entry confirmation"),
    ("statistics", "Win rate broken down by confidence bucket"),
    ("status", "systemd status of this deployment's own services"),
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
                # reply_markup is attached to EVERY response, not just
                # /start -- a keyboard only sent once can silently vanish
                # from the client (a service restart doesn't replay /start,
                # and Telegram clients let a user collapse a persistent
                # keyboard via a small UI toggle). Reasserting it on every
                # reply means it can never get permanently lost.
                if text in ("/start",):
                    notifier.send_tracked(
                        "Polar bot connected. Use the buttons below or /positions, /report.",
                        reply_markup=tg.PERSISTENT_KEYBOARD,
                    )
                elif text in ("/positions", tg.BUTTON_POSITIONS):
                    notifier.send(handle_positions(), reply_markup=tg.PERSISTENT_KEYBOARD)
                elif text in ("/report", tg.BUTTON_REPORT):
                    notifier.send(handle_report(), reply_markup=tg.PERSISTENT_KEYBOARD)
                elif text in ("/weekendhunter", tg.BUTTON_WEEKEND_HUNTER):
                    notifier.send(handle_weekend_hunter(), reply_markup=tg.PERSISTENT_KEYBOARD)
                elif text in ("/vpa", tg.BUTTON_VPA):
                    notifier.send(handle_vpa(), reply_markup=tg.PERSISTENT_KEYBOARD)
                elif text in ("/pending", tg.BUTTON_PENDING):
                    notifier.send(handle_pending(), reply_markup=tg.PERSISTENT_KEYBOARD)
                elif text in ("/statistics", tg.BUTTON_STATISTICS):
                    notifier.send(handle_statistics(), reply_markup=tg.PERSISTENT_KEYBOARD)
                elif text in ("/status", tg.BUTTON_STATUS):
                    notifier.send(handle_status(), reply_markup=tg.PERSISTENT_KEYBOARD)
            except Exception as exc:  # noqa: BLE001 - must never crash the listener
                print(f"[polar] failed to handle {text!r}: {exc}", flush=True)


if __name__ == "__main__":
    main()
