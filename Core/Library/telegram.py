"""Shared Telegram helpers used by all Cores and bots."""
from __future__ import annotations

import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)


def _scrub(text: str) -> str:
    """Remove any bot token from a string before logging."""
    return _TOKEN_RE.sub("bot<REDACTED>", text)


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
    """Alert the admin that a daemon loop raised an unhandled exception."""
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
