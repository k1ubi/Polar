"""Customer Telegram bot — registration, status, pause/resume.

Customers register their Bitget API credentials here, set their margin per
trade, and receive mirrored position notifications.

    python3 -m Core.customer_bot.bot

Runs as a separate process (polar-customer-bot.service). The admin bot
(Core/weekend_hunter/telegram_bot.py) is the admin's read/query interface;
this bot is the customer-facing onboarding and notification channel.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from Core.customer_bot import store as customer_store
from Core.Library.bitget_futures import BitgetFutures
from Core.Library.broker import BrokerError

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_DATA = Path(__file__).resolve().parent / "data"
OFFSET_PATH = _DATA / "bot_offset.json"
ADMIN_CHAT_ID = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID") or 0)

# In-memory multi-step registration state.
# {chat_id: {"step": str, "partial": dict}}
# Steps: waiting_api_key → waiting_secret → waiting_passphrase → waiting_margin
_reg_state: dict[int, dict] = {}


class CustomerBot:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_CUSTOMER_BOT_TOKEN", "")
        if not self.token:
            raise ValueError("TELEGRAM_CUSTOMER_BOT_TOKEN not set in .env")
        self._base = f"https://api.telegram.org/bot{self.token}"

    def send(self, chat_id: int, text: str) -> None:
        requests.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(
            f"{self._base}/getUpdates", params=params, timeout=timeout + 5
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    def set_commands(self) -> None:
        commands = [
            {"command": "start",    "description": "Welcome — what Polar does"},
            {"command": "register", "description": "Connect your Bitget API key"},
            {"command": "status",   "description": "Your account balance and status"},
            {"command": "pause",    "description": "Pause mirrored trades temporarily"},
            {"command": "resume",   "description": "Resume mirrored trades"},
            {"command": "revoke",   "description": "Delete your credentials from Polar"},
            {"command": "cancel",   "description": "Cancel the current registration flow"},
        ]
        requests.post(f"{self._base}/setMyCommands",
                      json={"commands": commands}, timeout=10)


def _load_offset() -> int | None:
    try:
        return json.loads(OFFSET_PATH.read_text()).get("offset")
    except Exception:
        return None


def _save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps({"offset": offset}))


# ── command handlers ─────────────────────────────────────────────────────────

def _handle_start(bot: CustomerBot, chat_id: int, name: str) -> None:
    cust = customer_store.get(chat_id)
    if cust:
        status = "🟢 active" if cust.get("enabled") else "⏸ paused"
        bot.send(chat_id,
            f"Welcome back, <b>{name}</b>!\n\n"
            f"Your account is {status}.\n"
            f"Margin per trade: <code>{cust.get('margin_usdt', 0):.2f} USDT</code>\n\n"
            f"Commands: /status /pause /resume /revoke")
    else:
        bot.send(chat_id,
            f"👋 <b>Welcome to Polar, {name}!</b>\n\n"
            "Polar is an automated crypto trading system. When it detects a signal, "
            "it mirrors the trade to your own Bitget futures account — you keep full "
            "custody of your funds.\n\n"
            "Use /register to connect your Bitget API key and start.")


def _handle_register(bot: CustomerBot, chat_id: int) -> None:
    if customer_store.get(chat_id):
        bot.send(chat_id,
            "You're already registered.\n"
            "Use /revoke first if you want to update your credentials.")
        return
    _reg_state[chat_id] = {"step": "waiting_api_key", "partial": {}}
    bot.send(chat_id,
        "<b>Step 1 / 4 — Bitget API key</b>\n\n"
        "Create a <b>Futures API key</b> with <b>Trade</b> permission at "
        "Bitget → Settings → API Management.\n"
        "Bind it to your IP. Do <b>not</b> grant Withdraw.\n\n"
        "Send your API key now:")


def _handle_status(bot: CustomerBot, chat_id: int, name: str) -> None:
    cust = customer_store.get(chat_id)
    if not cust:
        bot.send(chat_id, "You're not registered yet. Use /register to get started.")
        return
    try:
        broker = BitgetFutures(
            api_key=cust["bitget_api_key"],
            api_secret=cust["bitget_secret"],
            passphrase=cust["bitget_passphrase"],
        )
        equity = broker.account_equity_usdt()
        status = "🟢 Active — receiving trades" if cust.get("enabled") else "⏸ Paused"
        bot.send(chat_id,
            f"<b>{name}</b>\n\n"
            f"Status: {status}\n"
            f"Bitget equity: <code>{equity:.2f} USDT</code>\n"
            f"Margin per trade: <code>{cust.get('margin_usdt', 0):.2f} USDT</code>\n"
            f"Member since: {cust.get('registered_at', '?')[:10]}")
    except BrokerError as exc:
        bot.send(chat_id, f"⚠️ Could not reach Bitget: <code>{exc}</code>")


def _handle_pause(bot: CustomerBot, chat_id: int) -> None:
    cust = customer_store.get(chat_id)
    if not cust:
        bot.send(chat_id, "You're not registered. Use /register first.")
        return
    cust["enabled"] = False
    customer_store.upsert(cust)
    bot.send(chat_id, "⏸ Paused. New signals will not be mirrored until you /resume.")


def _handle_resume(bot: CustomerBot, chat_id: int) -> None:
    cust = customer_store.get(chat_id)
    if not cust:
        bot.send(chat_id, "You're not registered. Use /register first.")
        return
    cust["enabled"] = True
    customer_store.upsert(cust)
    bot.send(chat_id, "▶️ Resumed. You'll receive the next signals from Polar.")


def _handle_cancel(bot: CustomerBot, chat_id: int) -> None:
    if _reg_state.pop(chat_id, None):
        bot.send(chat_id, "Registration cancelled. Use /register to start again.")
    else:
        bot.send(chat_id, "Nothing to cancel.")


def _handle_revoke(bot: CustomerBot, chat_id: int) -> None:
    if not customer_store.get(chat_id):
        bot.send(chat_id, "Nothing to revoke — you're not registered.")
        return
    customer_store.remove(chat_id)
    _reg_state.pop(chat_id, None)
    bot.send(chat_id,
        "🗑 Your credentials have been deleted from Polar.\n"
        "Use /register to reconnect at any time.")


def _handle_customers(bot: CustomerBot, chat_id: int) -> None:
    """Admin-only command — silently ignored from non-admin chat IDs."""
    if chat_id != ADMIN_CHAT_ID:
        return
    all_c = customer_store.all_customers()
    if not all_c:
        bot.send(chat_id, "No customers registered yet.")
        return
    lines = [f"<b>{len(all_c)} registered customer(s)</b>", ""]
    for c in all_c:
        dot = "🟢" if c.get("enabled") else "⏸"
        lines.append(
            f"{dot} <code>{c['chat_id']}</code>  "
            f"{c.get('margin_usdt', 0):.2f} USDT/trade  "
            f"since {c.get('registered_at', '?')[:10]}"
        )
    bot.send(chat_id, "\n".join(lines))


# ── registration state machine ────────────────────────────────────────────────

def _handle_text(bot: CustomerBot, chat_id: int, text: str) -> None:
    """Drive the multi-step registration flow for plain-text messages."""
    state = _reg_state.get(chat_id)
    if not state:
        return  # no active flow — ignore

    step = state["step"]

    if step == "waiting_api_key":
        state["partial"]["api_key"] = text.strip()
        state["step"] = "waiting_secret"
        bot.send(chat_id, "<b>Step 2 / 4 — API secret</b>\n\nSend your API secret:")

    elif step == "waiting_secret":
        state["partial"]["secret"] = text.strip()
        state["step"] = "waiting_passphrase"
        bot.send(chat_id,
            "<b>Step 3 / 4 — API passphrase</b>\n\n"
            "Send the passphrase you chose when creating the key:")

    elif step == "waiting_passphrase":
        state["partial"]["passphrase"] = text.strip()
        state["step"] = "waiting_margin"
        bot.send(chat_id,
            "<b>Step 4 / 4 — Margin per trade (USDT)</b>\n\n"
            "How much USDT margin to post on each mirrored position?\n"
            "Example: <code>20</code> → 20 USDT per trade at 3× leverage "
            "= 60 USDT exposure per signal.")

    elif step == "waiting_margin":
        try:
            margin = float(text.strip())
            if margin <= 0:
                raise ValueError
        except ValueError:
            bot.send(chat_id, "Please send a positive number, e.g. <code>20</code>.")
            return

        partial = state["partial"]
        bot.send(chat_id, "⏳ Verifying your Bitget credentials…")

        try:
            broker = BitgetFutures(
                api_key=partial["api_key"],
                api_secret=partial["secret"],
                passphrase=partial["passphrase"],
            )
            equity = broker.account_equity_usdt()
        except BrokerError as exc:
            bot.send(chat_id,
                f"❌ Credentials rejected by Bitget: <code>{exc}</code>\n\n"
                "Check your API key, secret, and passphrase, then /register again.")
            _reg_state.pop(chat_id, None)
            return

        customer = {
            "chat_id": chat_id,
            "bitget_api_key": partial["api_key"],
            "bitget_secret": partial["secret"],
            "bitget_passphrase": partial["passphrase"],
            "margin_usdt": margin,
            "enabled": True,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        customer_store.upsert(customer)
        _reg_state.pop(chat_id, None)

        bot.send(chat_id,
            f"✅ <b>Registration complete!</b>\n\n"
            f"Bitget equity: <code>{equity:.2f} USDT</code>\n"
            f"Margin per trade: <code>{margin:.2f} USDT</code>\n\n"
            "You'll receive a Telegram message here each time Polar opens or closes "
            "a position. Your Bitget TP/SL manages your own exits automatically.\n\n"
            "Use /pause to pause, /revoke to remove credentials.")


# ── main loop ────────────────────────────────────────────────────────────────

def main() -> None:
    bot = CustomerBot()
    bot.set_commands()
    print(f"[customer_bot] listening on token {bot.token[:12]}…", flush=True)

    offset = _load_offset()
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=30)
        except Exception as exc:  # noqa: BLE001 - transient network errors must not crash the loop
            logger.warning("[customer_bot] getUpdates failed: %s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            _save_offset(offset)

            message = update.get("message")
            if not message or "text" not in message:
                continue

            chat_id = message["chat"]["id"]
            text = message["text"].strip()
            name = message["from"].get("first_name", str(chat_id))

            logger.info("[customer_bot] %r from %s", text[:40], chat_id)

            try:
                if text.startswith("/start"):
                    _handle_start(bot, chat_id, name)
                elif text.startswith("/register"):
                    _handle_register(bot, chat_id)
                elif text.startswith("/status"):
                    _handle_status(bot, chat_id, name)
                elif text.startswith("/pause"):
                    _handle_pause(bot, chat_id)
                elif text.startswith("/resume"):
                    _handle_resume(bot, chat_id)
                elif text.startswith("/cancel"):
                    _handle_cancel(bot, chat_id)
                elif text.startswith("/revoke"):
                    _handle_revoke(bot, chat_id)
                elif text.startswith("/customers"):
                    _handle_customers(bot, chat_id)
                else:
                    _handle_text(bot, chat_id, text)
            except Exception as exc:  # noqa: BLE001 - one bad handler must not kill the listener
                logger.error("[customer_bot] handler failed for %r from %s: %s",
                             text[:40], chat_id, exc)


if __name__ == "__main__":
    main()
