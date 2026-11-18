#!/usr/bin/env python3
"""Polar User Bot (@polar_user_bot) — customer copy trading.

Mirrors the main strategy onto registered customers' Bitget accounts. Rules:
  * entries are copied only after the main account's entry has FILLED (the guardian logs the `entry`
    signal on fill), and never while the system is paused or the circuit breaker is halted;
  * the take-profit is placed only after the customer's own entry has filled (Bitget rejects a
    reduce-only order with no position), sized to what actually filled;
  * a customer's trade is closed ONLY when Lorenzo approved the matching close on the main account
    (`close` with outcome MANUAL_APPROVED, or `partial_close`), and only for the quantity this bot
    opened for them (tracked in customers/copies.json), never a position the customer holds by themselves;
  * tiny accounts are never rounded up into oversized risk: below the minimum lot the trade is skipped."""

import asyncio
import json
import os
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from config import _load_env as _cfg_load; _cfg_load()
import os

import ccxt
import telegram
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("TG_USER_BOT_TOKEN", "")
CUSTOMERS_FILE = Path("/home/kyubi/Polar/live/customers/customers.json")
COPIES_FILE = Path("/home/kyubi/Polar/live/customers/copies.json")
STATE_DIR = Path("/home/kyubi/Polar/live/state")
SIGNALS_FILE = STATE_DIR / "trade-signals.jsonl"
LAST_IDX_FILE = STATE_DIR / "last-processed-signal.txt"      # entries already handled
CLOSE_IDX_FILE = STATE_DIR / "last-processed-close.txt"      # approved closes / partials already handled

RISK_PCT = 0.015            # risk per trade, as a share of the customer's free balance
MAX_NOTIONAL_X = 1.5        # never open more than 1.5x the customer's balance in one position
MAX_MINLOT_RISK = 0.03      # if the minimum lot would risk more than 3% of balance, skip the trade
ENTRY_TTL = timedelta(minutes=45)
SIGNAL_MAX_AGE = timedelta(minutes=60)

AWAITING_AGREE, AWAITING_KEY, AWAITING_SECRET, AWAITING_PASS = range(4)

TERMS = (
    "⚠️ <b>Please read before you register</b>\n\n"
    "• This bot copies trades from an automated strategy on leveraged crypto futures. You can lose "
    "part or ALL of your balance, and losses can happen quickly.\n"
    "• Nothing here is financial advice and past results do not predict future ones.\n"
    "• Stops are NOT placed on the exchange: only the take-profit is. A losing trade stays open until "
    "the strategy's owner approves closing it.\n"
    "• You are responsible for your own account, keys and funds. Only use money you can afford to lose.\n"
    "• Use a dedicated API key with <b>trade permission only — no withdrawals</b> — and an IP whitelist.\n\n"
    "To continue, reply exactly: <b>I AGREE</b>   (or /cancel)"
)


# ── storage ──────────────────────────────────────────────────────────────────

def load_customers() -> dict:
    try:
        with open(CUSTOMERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_customers(data: dict):
    CUSTOMERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOMERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CUSTOMERS_FILE)


def load_copies() -> dict:
    try:
        return json.loads(COPIES_FILE.read_text())
    except Exception:
        return {}


def save_copies(data: dict):
    tmp = COPIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(COPIES_FILE)


def read_idx(path: Path):
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def write_idx(path: Path, n: int):
    path.write_text(str(n))


def read_signals():
    """(entries, closes): `entry` signals, and approved-close / partial-close signals, in file order."""
    entries, closes = [], []
    try:
        with open(SIGNALS_FILE) as f:
            for line in f:
                try:
                    sig = json.loads(line)
                except Exception:
                    continue
                t = sig.get("type")
                if t == "entry":
                    entries.append(sig)
                elif t == "partial_close" or (t == "close" and sig.get("outcome") == "MANUAL_APPROVED"):
                    closes.append(sig)
    except FileNotFoundError:
        pass
    return entries, closes


def blocked_reason():
    """Why NO new trade may be copied right now, or None. Unreadable state counts as blocked."""
    for name, key, why in (("pause.json", "paused", "the system is paused"),
                           ("circuit-breaker.json", "halted", "the circuit breaker is active")):
        p = STATE_DIR / name
        if not p.exists():
            continue
        try:
            if json.loads(p.read_text()).get(key):
                return why
        except Exception:
            return "the system state is unreadable"
    return None


def make_exchange(api_key: str, secret: str, passphrase: str) -> ccxt.bitget:
    return ccxt.bitget({
        "apiKey": api_key,
        "secret": secret,
        "password": passphrase,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })


# ── conversation: register ───────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to <b>Polar User Bot</b>\n\n"
        "It mirrors the trades of the Polar strategy onto your Bitget account, sized to your balance "
        f"(about {RISK_PCT*100:.1f}% of your balance at risk per trade, position size capped at "
        f"{MAX_NOTIONAL_X}x your balance).\n\n"
        "/register — read the risks and connect your Bitget account\n"
        "/status — your mirrored trades\n"
        "/unregister — disconnect (your open positions are left untouched)",
        parse_mode="HTML",
    )


async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id in load_customers():
        await update.message.reply_text("You're already registered. Use /unregister first to change credentials.")
        return ConversationHandler.END
    await update.message.reply_text(TERMS, parse_mode="HTML")
    return AWAITING_AGREE


async def got_agree(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().upper() != "I AGREE":
        await update.message.reply_text("Registration cancelled. Send /register to start again.")
        return ConversationHandler.END
    ctx.user_data["agreed_at"] = datetime.now(timezone.utc).isoformat()
    await update.message.reply_text("🔑 Send your Bitget API key (I will delete your message right after):")
    return AWAITING_KEY


async def _scrub(update: Update):
    """Remove the message that carried a credential from the chat."""
    try:
        await update.message.delete()
    except Exception:
        pass


async def got_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["api_key"] = update.message.text.strip()
    await _scrub(update)
    await update.effective_chat.send_message("🔒 Send your API secret:")
    return AWAITING_SECRET


async def got_secret(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["secret"] = update.message.text.strip()
    await _scrub(update)
    await update.effective_chat.send_message("🔐 Send your API passphrase:")
    return AWAITING_PASS


async def got_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    passphrase = update.message.text.strip()
    await _scrub(update)
    api_key = ctx.user_data.pop("api_key", "")
    secret = ctx.user_data.pop("secret", "")
    chat_id = str(update.effective_chat.id)
    username = update.effective_user.username or chat_id

    try:
        ex = make_exchange(api_key, secret, passphrase)
        bal = await asyncio.to_thread(ex.fetch_balance, {"type": "swap"})
        usdt_free = float(bal.get("USDT", {}).get("free", 0))
    except Exception as e:
        await update.effective_chat.send_message(
            f"❌ Could not connect to Bitget:\n{str(e)[:120]}\n\nCheck credentials and /register again."
        )
        return ConversationHandler.END

    customers = load_customers()
    customers[chat_id] = {
        "chat_id": chat_id,
        "username": username,
        "api_key": api_key,
        "secret": secret,
        "passphrase": passphrase,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "accepted_terms_at": ctx.user_data.pop("agreed_at", None),
    }
    save_customers(customers)
    await update.effective_chat.send_message(
        f"✅ Registered! Balance: <b>${usdt_free:.2f} USDT</b>\n"
        "Your key messages were deleted from this chat. You'll now receive mirrored trades automatically.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled. Nothing was saved.")
    return ConversationHandler.END


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    customers = load_customers()
    if chat_id not in customers:
        await update.message.reply_text("Not registered. Use /register.")
        return

    c = customers[chat_id]
    try:
        ex = make_exchange(c["api_key"], c["secret"], c["passphrase"])
        positions = await asyncio.to_thread(ex.fetch_positions)
        open_pos = [p for p in positions if float(p.get("contracts", 0) or 0) != 0]
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        return

    lines = []
    mine = load_copies().get(chat_id, {})
    if mine:
        lines.append("🔁 <b>Trades mirrored for you</b>")
        for cp in mine.values():
            lines.append(f"  {cp['symbol'].split('/')[0]} {cp['side']} — {cp['status']}")
        lines.append("")
    if open_pos:
        lines.append("📊 <b>Your Bitget positions</b>")
        for pos in open_pos:
            sym = pos.get("symbol", "?")
            side = (pos.get("side") or "?").upper()
            entry = pos.get("entryPrice") or 0
            pnl = pos.get("unrealizedPnl") or 0
            emoji = "🔼" if side == "LONG" else "🔽"
            lines.append(f"{emoji} {sym}  {side}  Entry: ${entry:.4f}  PnL: ${pnl:.2f}")
    if not lines:
        await update.message.reply_text("📭 No open positions.")
        return
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_unregister(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    customers = load_customers()
    if chat_id not in customers:
        await update.message.reply_text("Not registered.")
        return
    del customers[chat_id]
    save_customers(customers)
    copies = load_copies()
    if copies.pop(chat_id, None) is not None:
        save_copies(copies)
    await update.message.reply_text(
        "✅ Unregistered and your keys were deleted. Your open positions and any resting orders are "
        "untouched — manage them directly on Bitget."
    )


# ── copy engine (blocking ccxt calls run in worker threads) ──────────────────

def size_copy(ex, sym: str, entry: float, sl_pct: float, free: float):
    """(qty, None) or (None, reason). Risk ~1.5% of balance, position capped at MAX_NOTIONAL_X x balance,
    rounded DOWN to the exchange lot; below the minimum lot the trade is skipped unless the minimum lot
    itself stays within MAX_MINLOT_RISK of the balance."""
    risk_usd = free * RISK_PCT
    qty = min(risk_usd / (entry * sl_pct), free * MAX_NOTIONAL_X / entry)
    qty = float(ex.amount_to_precision(sym, qty))
    min_qty = float(((ex.market(sym).get("limits") or {}).get("amount") or {}).get("min") or 0)
    if qty < min_qty:
        if min_qty * entry * sl_pct > free * MAX_MINLOT_RISK or min_qty * entry > free * MAX_NOTIONAL_X:
            return None, f"balance too small for the minimum lot ({min_qty} {sym.split('/')[0]})"
        qty = min_qty
    if qty <= 0:
        return None, "size rounds to zero"
    return qty, None


def _params(side: str, reduce=False):
    p = {"tdMode": "cross", "posSide": "long" if side == "LONG" else "short"}
    if reduce:
        p["reduceOnly"] = True
    return p


def execute_copy(cust: dict, sig: dict):
    """Returns (text for the customer, copy record or None)."""
    try:
        ex = make_exchange(cust["api_key"], cust["secret"], cust["passphrase"])
        ex.load_markets()
        bal = ex.fetch_balance({"type": "swap"})
        free = float(bal.get("USDT", {}).get("free", 0))
        if free < 10:
            return f"skipped — low balance (${free:.2f})", None
        sym, side = sig["symbol"], sig["side"]
        entry = float(ex.price_to_precision(sym, float(sig["entry_price"])))
        qty, why = size_copy(ex, sym, entry, float(sig.get("sl_distance_pct") or 0.008), free)
        if qty is None:
            return f"skipped — {why}", None
        o = ex.create_order(sym, "limit", "buy" if side == "LONG" else "sell", qty, entry, params=_params(side))
        rec = {"symbol": sym, "side": side, "qty": qty, "entry_price": entry, "tp_level": sig.get("tp_level"),
               "signal_key": f"{sym}|{sig.get('entered_at')}", "entry_order_id": str(o["id"]), "tp_order_id": None,
               "filled": 0.0, "status": "pending", "placed_at": datetime.now(timezone.utc).isoformat(), "errors": 0}
        return f"entry order placed, qty={qty} (take-profit follows once it fills)", rec
    except Exception as e:
        return f"error: {str(e)[:80]}", None


def _cancel(ex, oid, sym):
    try:
        ex.cancel_order(oid, sym)
    except Exception:
        pass  # already filled/cancelled


def tend_copy(cust: dict, c: dict):
    """One step for a tracked copy. Returns (updated record or None when finished, message or None)."""
    ex = make_exchange(cust["api_key"], cust["secret"], cust["passphrase"])
    sym, side = c["symbol"], c["side"]
    base = sym.split("/")[0]
    if c["status"] == "pending":
        o = ex.fetch_order(c["entry_order_id"], sym)
        filled = float(o.get("filled") or 0)
        st = o.get("status")
        age = datetime.now(timezone.utc) - datetime.fromisoformat(c["placed_at"])
        if st == "open":
            if filled > 0 or age > ENTRY_TTL:
                _cancel(ex, c["entry_order_id"], sym)  # stop the rest of the entry growing past its TP
                o = ex.fetch_order(c["entry_order_id"], sym)
                filled = float(o.get("filled") or filled)
            else:
                return c, None
        if filled <= 0:
            return None, f"{base} {side}: entry did not fill and was cancelled."
        c["filled"] = filled
        if c.get("tp_level"):
            tp = ex.create_order(sym, "limit", "sell" if side == "LONG" else "buy", filled,
                                 float(ex.price_to_precision(sym, float(c["tp_level"]))), params=_params(side, True))
            c["tp_order_id"] = str(tp["id"])
        c["status"] = "open"
        return c, f"{base} {side} filled ({filled}); take-profit placed at {c.get('tp_level')}."
    if c.get("tp_order_id"):
        o = ex.fetch_order(c["tp_order_id"], sym)
        if o.get("status") == "closed":
            return None, f"✅ {base} {side}: take-profit hit."
        if o.get("status") in ("canceled", "expired"):
            return None, f"ℹ️ {base} {side}: your take-profit order was cancelled, so this trade is no longer tracked."
    return c, None


def mirror_close(cust: dict, c: dict, sig: dict):
    """Follow an APPROVED close on the main account. Only this copy's own quantity is touched."""
    ex = make_exchange(cust["api_key"], cust["secret"], cust["passphrase"])
    ex.load_markets()
    sym, side = c["symbol"], c["side"]
    base = sym.split("/")[0]
    partial = sig.get("type") == "partial_close"
    if c["status"] == "pending":
        if partial:
            return c, None
        _cancel(ex, c["entry_order_id"], sym)
        filled = float(ex.fetch_order(c["entry_order_id"], sym).get("filled") or 0)
        if filled <= 0:
            return None, f"{base} {side}: the strategy closed this trade before your entry filled; entry cancelled."
        c["filled"], c["status"] = filled, "open"
    held = float(c.get("filled") or 0)
    qty = float(ex.amount_to_precision(sym, held * float(sig["fraction"]))) if partial else held
    min_qty = float(((ex.market(sym).get("limits") or {}).get("amount") or {}).get("min") or 0)
    if partial and (qty < min_qty or held - qty < min_qty):
        return c, f"{base} {side}: partial close skipped (position too small to split)."
    if c.get("tp_order_id"):
        _cancel(ex, c["tp_order_id"], sym)
        c["tp_order_id"] = None
    ex.create_order(sym, "market", "sell" if side == "LONG" else "buy", qty, params=_params(side, True))
    if not partial:
        return None, f"🔒 {base} {side}: closed at market ({qty}) — the strategy's owner approved closing this trade."
    c["filled"] = round(held - qty, 8)
    new_tp = sig.get("tp_level")
    if new_tp:
        c["tp_level"] = new_tp
        tp = ex.create_order(sym, "limit", "sell" if side == "LONG" else "buy", c["filled"],
                             float(ex.price_to_precision(sym, float(new_tp))), params=_params(side, True))
        c["tp_order_id"] = str(tp["id"])
    return c, f"🔒 {base} {side}: partial close ({qty}) done; take-profit moved to {new_tp}."


async def notify(app, chat_id, text):
    try:
        await app.bot.send_message(chat_id=int(chat_id), text=text, parse_mode="HTML")
    except Exception:
        pass


async def process_entries(app):
    entries, _ = read_signals()
    last = read_idx(LAST_IDX_FILE)
    if last is None:
        last = len(entries)
        write_idx(LAST_IDX_FILE, last)
    for i in range(last, len(entries)):
        sig = entries[i]
        base, side = sig.get("symbol", "?").split("/")[0], sig.get("side", "?")
        entry, tp, stop = (float(sig.get(k) or 0) for k in ("entry_price", "tp_level", "stop_level"))
        why = blocked_reason()
        try:
            if not why and datetime.now(timezone.utc) - datetime.fromisoformat(sig["entered_at"].replace("Z", "+00:00")) > SIGNAL_MAX_AGE:
                why = "the signal is too old"
        except Exception:
            why = "the signal has no valid time"
        customers = load_customers()
        copies = load_copies()
        for chat_id, cust in customers.items():
            key = f"{sig.get('symbol')}|{sig.get('entered_at')}"
            if any(c["signal_key"] == key for c in copies.get(chat_id, {}).values()):
                continue  # already copied (restart safety)
            if why:
                await notify(app, chat_id, f"⏸ <b>Not copied</b>: {base} {side} — {why}.")
                continue
            text, rec = await asyncio.to_thread(execute_copy, cust, sig)
            if rec:
                copies.setdefault(chat_id, {})[rec["entry_order_id"]] = rec
                save_copies(copies)
            await notify(app, chat_id,
                         f"📡 <b>Trade copied</b>\n{base} {side}\nEntry: ${entry:.4f}  TP: ${tp:.4f}\n"
                         f"Stop (watch only): ${stop:.4f}  Score: {sig.get('score', '?')}/8\nStatus: {text}")
        write_idx(LAST_IDX_FILE, i + 1)


async def process_closes(app):
    _, closes = read_signals()
    last = read_idx(CLOSE_IDX_FILE)
    if last is None:  # first start: never replay history
        write_idx(CLOSE_IDX_FILE, len(closes))
        return
    for i in range(last, len(closes)):
        sig = closes[i]
        copies = load_copies()
        customers = load_customers()
        for chat_id, mine in list(copies.items()):
            cust = customers.get(chat_id)
            for cid, c in list(mine.items()):
                if not cust or c["symbol"] != sig.get("symbol") or c["side"] != sig.get("side"):
                    continue
                try:
                    new, msg = await asyncio.to_thread(mirror_close, cust, c, sig)
                except Exception as e:
                    await notify(app, chat_id, f"🚨 {c['symbol'].split('/')[0]}: could not mirror the close "
                                               f"({str(e)[:80]}). Please check your position on Bitget.")
                    continue
                if new is None:
                    mine.pop(cid)
                else:
                    mine[cid] = new
                if msg:
                    await notify(app, chat_id, msg)
        save_copies({k: v for k, v in copies.items() if v})
        write_idx(CLOSE_IDX_FILE, i + 1)


async def tend_all(app):
    copies = load_copies()
    customers = load_customers()
    for chat_id, mine in list(copies.items()):
        cust = customers.get(chat_id)
        if not cust:
            copies.pop(chat_id)
            continue
        for cid, c in list(mine.items()):
            try:
                new, msg = await asyncio.to_thread(tend_copy, cust, c)
                c["errors"] = 0
            except Exception as e:
                c["errors"] = c.get("errors", 0) + 1
                if c["errors"] >= 5:
                    mine.pop(cid)
                    await notify(app, chat_id, f"🚨 {c['symbol'].split('/')[0]}: I can no longer track this trade "
                                               f"({str(e)[:80]}). Please check it on Bitget.")
                continue
            if new is None:
                mine.pop(cid)
            else:
                mine[cid] = new
            if msg:
                await notify(app, chat_id, msg)
    save_copies({k: v for k, v in copies.items() if v})


async def copy_loop(app: Application):
    while True:
        for step in (process_entries, process_closes, tend_all):
            try:
                await step(app)
            except Exception:
                traceback.print_exc()
        await asyncio.sleep(30)


async def _run(app: Application):
    asyncio.create_task(copy_loop(app))
    async with app:
        await app.start()
        offset = 0
        print("Polar User Bot polling...", flush=True)
        while True:
            try:
                updates = await app.bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
                for upd in updates:
                    offset = upd.update_id + 1
                    asyncio.create_task(app.process_update(upd))
            except telegram.error.Conflict:
                print("Conflict: waiting 45s...", flush=True)
                await asyncio.sleep(45)
            except (telegram.error.NetworkError, telegram.error.TimedOut):
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error: {e}", flush=True)
                await asyncio.sleep(10)
        await app.stop()


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    reg = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            AWAITING_AGREE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, got_agree)],
            AWAITING_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_key)],
            AWAITING_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_secret)],
            AWAITING_PASS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(reg)
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("unregister", cmd_unregister))

    print("Polar User Bot starting...", flush=True)
    asyncio.run(_run(app))


if __name__ == "__main__":
    main()
