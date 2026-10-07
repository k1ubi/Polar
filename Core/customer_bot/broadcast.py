"""Fan-out trade signals to all registered and enabled customers.

Called synchronously from both daemons (VPA and Weekend Hunter).
Each customer's Bitget TP/SL handles their own exits — Polar only
mirrors the entry.
"""
from __future__ import annotations

import logging
import uuid

import requests

from Core.customer_bot import store as customer_store
from Core.Library.bitget_futures import BitgetFutures
from Core.Library.broker import BrokerError

logger = logging.getLogger(__name__)


def _telegram(bot_token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - notification failure must not abort the broadcast
        logger.warning("[broadcast] telegram to %s failed: %s", chat_id, exc)


def fan_out_opened(signal: dict, customer_bot_token: str, live_mode: bool = False) -> None:
    """Open a mirrored position on each enabled customer's Bitget account.

    `signal` must contain: symbol, direction, stop, target, leverage.
    Customer's own `margin_usdt` (set during registration) is used for sizing.
    Gated on `live_mode`: customers receive real orders only when the calling
    daemon is itself trading live (POLAR_LIVE_TRADING / POLAR_LIVE_TRADING_VPA).
    """
    if not live_mode:
        return
    if not customer_bot_token:
        return
    customers = customer_store.enabled_customers()
    if not customers:
        return

    for cust in customers:
        chat_id = cust["chat_id"]
        margin = float(cust.get("margin_usdt") or 0)
        if margin <= 0:
            logger.info("[broadcast] customer %s has no margin configured — skipping", chat_id)
            continue

        try:
            broker = BitgetFutures(
                api_key=cust["bitget_api_key"],
                api_secret=cust["bitget_secret"],
                passphrase=cust["bitget_passphrase"],
            )
            notional = margin * signal["leverage"]
            fill = broker.open_position(
                symbol=signal["symbol"],
                direction=signal["direction"],
                notional_usdt=notional,
                leverage=signal["leverage"],
                stop_loss=signal["stop"],
                take_profit=signal["target"],
                client_oid=str(uuid.uuid4()),
            )
            side_label = "🟢 LONG" if signal["direction"] == "bullish" else "🔴 SHORT"
            text = (
                f"{side_label} <b>{signal['symbol']}</b>\n"
                f"Entry: <code>{fill.avg_price:.4f}</code>\n"
                f"Stop: <code>{signal['stop']:.4f}</code>  "
                f"Target: <code>{signal['target']:.4f}</code>\n"
                f"Margin: <code>{margin:.2f} USDT</code> × {int(signal['leverage'])}x  "
                f"Exposure: <code>{notional:.2f} USDT</code>\n"
                f"Order: <code>{fill.order_id}</code>"
            )
            _telegram(customer_bot_token, chat_id, text)
            logger.info("[broadcast] opened %s for customer %s  order=%s",
                        signal["symbol"], chat_id, fill.order_id)

        except BrokerError as exc:
            logger.warning("[broadcast] open failed customer=%s symbol=%s: %s",
                           chat_id, signal["symbol"], exc)
            _telegram(customer_bot_token, chat_id,
                      f"⚠️ Could not open <b>{signal['symbol']}</b>: <code>{exc}</code>")
        except Exception as exc:
            logger.error("[broadcast] unexpected error customer=%s: %s", chat_id, exc)


def fan_out_closed(pos: dict, customer_bot_token: str, live_mode: bool = False) -> None:
    """Notify all enabled customers that an admin position closed.

    Informational only — customer exits are managed by Bitget TP/SL.
    Gated on `live_mode` for consistency with fan_out_opened.
    """
    if not live_mode:
        return
    if not customer_bot_token:
        return
    customers = customer_store.enabled_customers()
    if not customers:
        return

    pnl = pos.get("pnl_usdt") or 0
    emoji = "✅" if pnl >= 0 else "❌"
    reason = pos.get("exit_reason") or "?"
    for cust in customers:
        chat_id = cust["chat_id"]
        try:
            _telegram(customer_bot_token, chat_id, (
                f"{emoji} <b>{pos['symbol']}</b> closed on Polar\n"
                f"Reason: <code>{reason}</code>\n"
                f"<i>Your Bitget TP/SL manages your own exit.</i>"
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[broadcast] notify close failed customer=%s: %s", chat_id, exc)
