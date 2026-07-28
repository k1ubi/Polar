"""Generic Telegram delivery: the notifier, message-formatting primitives,
and the persistent keyboard — shared by every Core that pushes alerts.

Per-Core message CONTENT (what a signal/close message actually says) lives
in that Core's own daemon, not here — this module only provides the
building blocks (`row_block`, `fmt_time`) and the transport (`TelegramNotifier`).

Create a bot via @BotFather and set POLAR_TELEGRAM_BOT_TOKEN /
POLAR_TELEGRAM_CHAT_ID in `.env` before using this.
"""
from __future__ import annotations

import os
from datetime import datetime

import requests

BUTTON_POSITIONS = "📊 Positions"
BUTTON_REPORT = "📈 Report"
BUTTON_WEEKEND_HUNTER = "🎯 Weekend Hunter"
BUTTON_PENDING = "⏳ Pending Trades"
BUTTON_STATISTICS = "📐 Statistics"
BUTTON_STATUS = "⚡️ Status"  # systemd --user status of this deployment's own services

PERSISTENT_KEYBOARD = {
    "keyboard": [
        [{"text": BUTTON_POSITIONS}, {"text": BUTTON_REPORT}],
        [{"text": BUTTON_WEEKEND_HUNTER}, {"text": BUTTON_PENDING}],
        [{"text": BUTTON_STATISTICS}, {"text": BUTTON_STATUS}],
    ],
    "resize_keyboard": True,
}


def fmt_price(price: float) -> str:
    """Sane decimal precision across wildly different price magnitudes in
    one universe (BTC ~65000, XRP ~1.1, DOGE ~0.07)."""
    if price >= 100:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    return f"{price:.6f}"


def fmt_time(iso_str: str | None) -> str:
    """MM-DD HH:MM instead of the raw ISO string (microseconds + tz offset
    make a closed-trade list hard to scan at a glance)."""
    if not iso_str:
        return "?"
    try:
        return datetime.fromisoformat(iso_str).strftime("%m-%d %H:%M")
    except ValueError:
        return iso_str


def row_block(rows: list[tuple[str, str]]) -> str:
    """Renders label/value pairs as aligned rows in a <pre> block —
    Telegram has no table markup, so monospace + padding is the closest
    approximation. Shared by every message so pushed alerts and on-demand
    command replies look consistent."""
    label_width = max(len(label) for label, _ in rows) + 2
    table = "\n".join(f"{label:<{label_width}}{value}" for label, value in rows)
    return f"<pre>{table}</pre>"


class TelegramNotifier:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = bot_token or os.getenv("POLAR_TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("POLAR_TELEGRAM_CHAT_ID")
        if not self.bot_token or not self.chat_id:
            raise ValueError(
                "POLAR_TELEGRAM_BOT_TOKEN / POLAR_TELEGRAM_CHAT_ID not set — see .env"
            )
        self._base = f"https://api.telegram.org/bot{self.bot_token}"

    def send(self, message: str) -> None:
        """Fire and forget. Use send_tracked() when the caller needs the
        message_id back (e.g. to edit it later on TP/SL)."""
        self.send_tracked(message)

    def send_tracked(self, message: str, reply_markup: dict | None = None) -> int | None:
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")

    def edit_message(self, message_id: int, message: str) -> None:
        """Rewrites a previously-sent message in place — used to mark a
        signal green (TP) or red (SL) once it resolves, instead of a
        separate follow-up message."""
        resp = requests.post(
            f"{self._base}/editMessageText",
            json={"chat_id": self.chat_id, "message_id": message_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Registers commands in Telegram's "/" picker. Idempotent — safe
        to call every time (Telegram just overwrites the list). `commands`
        is a list of (command, description) so each Core's bot process
        registers only the commands it actually answers."""
        resp = requests.post(
            f"{self._base}/setMyCommands",
            json={"commands": [{"command": c, "description": d} for c, d in commands]},
            timeout=10,
        )
        resp.raise_for_status()

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        """Long-polling read of incoming messages (button taps / typed
        commands). `offset` should be the last-seen update_id + 1 to avoid
        re-processing the same update."""
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(f"{self._base}/getUpdates", params=params, timeout=timeout + 10)
        resp.raise_for_status()
        return resp.json().get("result", [])
