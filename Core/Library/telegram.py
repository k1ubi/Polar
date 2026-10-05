"""Generic Telegram delivery: async helpers for daemon loops, plus the
synchronous TelegramNotifier used by Weekend Hunter and other Cores that
poll rather than run an asyncio event loop.

Per-Core message CONTENT lives in that Core's own daemon, not here.
Credentials from `.env`: TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_CHAT_ID.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

import aiohttp
import requests

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)

# ── keyboard / button labels ───────────────────────────────────────────────

BUTTON_POSITIONS = "📊 Positions"
BUTTON_REPORT = "📈 Report"
BUTTON_WEEKEND_HUNTER = "🎯 Weekend Hunter"
BUTTON_VPA = "📊 VPA"
BUTTON_PENDING = "⏳ Pending Trades"
BUTTON_STATISTICS = "📐 Statistics"
BUTTON_STATUS = "⚡️ Status"

PERSISTENT_KEYBOARD = {
    "keyboard": [
        [{"text": BUTTON_POSITIONS}, {"text": BUTTON_REPORT}],
        [{"text": BUTTON_WEEKEND_HUNTER}, {"text": BUTTON_VPA}],
        [{"text": BUTTON_PENDING}, {"text": BUTTON_STATISTICS}],
        [{"text": BUTTON_STATUS}],
    ],
    "resize_keyboard": True,
}

# ── formatting helpers ─────────────────────────────────────────────────────


def fmt_price(price: float) -> str:
    """Sane decimal precision across BTC (~65k), XRP (~1), DOGE (~0.07)."""
    if price >= 100:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    return f"{price:.6f}"


def fmt_time(iso_str: str | None) -> str:
    """MM-DD HH:MM — easier to scan than a full ISO timestamp."""
    if not iso_str:
        return "?"
    try:
        return datetime.fromisoformat(iso_str).strftime("%m-%d %H:%M")
    except ValueError:
        return iso_str


def row_block(rows: list[tuple[str, str]]) -> str:
    """Label/value pairs in a monospace <pre> block."""
    label_width = max(len(label) for label, _ in rows) + 2
    table = "\n".join(f"{label:<{label_width}}{value}" for label, value in rows)
    return f"<pre>{table}</pre>"


def _scrub(text: str) -> str:
    return _TOKEN_RE.sub("bot<REDACTED>", text)


# ── synchronous notifier (Weekend Hunter, polling daemons) ─────────────────


class TelegramNotifier:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        if not self.bot_token or not self.chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_CHAT_ID not set — see .env"
            )
        self._base = f"https://api.telegram.org/bot{self.bot_token}"

    def send(self, message: str, reply_markup: dict | None = None) -> None:
        self.send_tracked(message, reply_markup=reply_markup)

    def send_tracked(self, message: str, reply_markup: dict | None = None) -> int | None:
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        resp = requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")

    def edit_message(self, message_id: int, message: str) -> None:
        resp = requests.post(
            f"{self._base}/editMessageText",
            json={
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()


# ── async helpers (VPA daemon, asyncio loops) ──────────────────────────────


async def send_message(
    session: aiohttp.ClientSession,
    bot_token: str,
    chat_id: int | str,
    text: str,
    parse_mode: str = "HTML",
) -> bool:
    """Send a Telegram message. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            logger.warning("Telegram sendMessage %s: %s", resp.status, _scrub(body))
            return False
    except aiohttp.ClientError as exc:
        logger.warning("Telegram sendMessage failed: %s", _scrub(str(exc)))
        return False


async def notify_loop_failure(
    session: aiohttp.ClientSession,
    bot_token: str,
    chat_id: int | str,
    core_name: str,
    error: Exception,
) -> None:
    text = (
        f"🚨 <b>{core_name}</b> loop failure\n"
        f"<code>{type(error).__name__}: {_scrub(str(error))}</code>"
    )
    await send_message(session, bot_token, chat_id, text)


async def get_updates(
    session: aiohttp.ClientSession,
    bot_token: str,
    offset: int | None = None,
    timeout: int = 30,
) -> list[dict]:
    """Long-poll Telegram for new updates."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=timeout + 5)
        ) as resp:
            if resp.status == 200:
                body = await resp.json()
                return body.get("result", [])
            body = await resp.text()
            logger.warning("getUpdates %s: %s", resp.status, _scrub(body))
            return []
    except aiohttp.ClientError as exc:
        logger.warning("getUpdates failed: %s", _scrub(str(exc)))
        return []
