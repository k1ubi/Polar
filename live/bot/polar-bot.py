#!/usr/bin/env python3
"""Polar Bot (@polar_aibot) — Lorenzo's trading control center"""

import asyncio
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

import os, sys
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from config import TG_TOKEN as BOT_TOKEN, TG_CHAT as _CHAT
OWNER_CHAT_ID = int(_CHAT) if _CHAT else 0
STATE_DIR = Path("/home/kyubi/Polar/live/state")
ISSUES_DIR = Path("/home/kyubi/.openalice/workspaces/workspaces/chat-tidy-silver-cove/.alice/issues")
BINANCE_PRICE = "https://api.binance.com/api/v3/ticker/price?symbol="


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def load_jsonl(path):
    lines = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return lines


async def live_price(sym: str) -> float:
    """Fetch last price from Binance public API. sym = 'BTC/USDT:USDT' -> 'BTCUSDT'"""
    base = sym.split('/')[0]
    binance_sym = base + "USDT"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as s:
            async with s.get(BINANCE_PRICE + binance_sym) as r:
                if r.status == 200:
                    d = await r.json()
                    return float(d["price"])
    except Exception:
        pass
    return 0.0


MAIN_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📊 Positions", callback_data="positions"),
        InlineKeyboardButton("📋 Report",    callback_data="report"),
    ],
    [
        InlineKeyboardButton("👁 Monitor",   callback_data="monitor"),
        InlineKeyboardButton("📈 Metrics",   callback_data="metrics"),
    ],
    [
        InlineKeyboardButton("⚙️ Services",  callback_data="services"),
        InlineKeyboardButton("Decisions ⚖️", callback_data="decisions"),
    ],
])

CHAT_BAR_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Positions"), KeyboardButton("📋 Report")],
        [KeyboardButton("👁 Monitor"),   KeyboardButton("📈 Metrics")],
        [KeyboardButton("⚙️ Services"),  KeyboardButton("Decisions ⚖️")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

CHAT_BAR_ACTIONS = {
    "📊 Positions": "positions",
    "📋 Report":    "report",
    "👁 Monitor":   "monitor",
    "📈 Metrics":   "metrics",
    "⚙️ Services":  "services",
    "Decisions ⚖️": "decisions",
}


def only_owner(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != OWNER_CHAT_ID:
            return
        return await func(update, ctx)
    return wrapper


@only_owner
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Polar Bot</b> — your trading desk",
        parse_mode="HTML",
        reply_markup=CHAT_BAR_KEYBOARD,
    )


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_CHAT_ID:
        await query.answer()
        return
    await query.answer()
    # Re-use the same logic by routing to the right function
    action = query.data
    if action.split(":")[0] in ("ap", "apy", "rj", "no"):
        await _close_button(query, action)
        return
    if action.split(":")[0] in DECISION_KINDS:
        await _decision_button(query, action)
        return
    if action == "decisions":
        await _send_decisions(query.message)
        return
    # Patch update so handlers can reply via query.message
    fake_update = update
    fake_update._effective_message = query.message
    dispatch = {
        "positions": _positions_text,
        "report":    _report_text,
        "monitor":   _monitor_text,
        "metrics":   _metrics_text,
        "services":  _services_text,
    }
    if action in dispatch:
        text, parse_mode = await dispatch[action]()
        await query.edit_message_text(
            text,
            parse_mode=parse_mode,
            reply_markup=MAIN_KEYBOARD,
        )


# ── Shared text-building helpers (used by both commands and buttons) ─────────

async def _positions_text():
    positions = load_json(STATE_DIR / "open-positions.json", {})
    if not positions:
        return "📭 No open positions.", "HTML"
    lines = ["📊 <b>Open Positions</b>\n"]
    for pid, pos in positions.items():
        sym = pos.get("symbol", pid)
        side = pos.get("side", "?").upper()
        entry = pos.get("entry_price", 0)
        qty = pos.get("qty", 0)
        stop = pos.get("stop_level", 0)
        tp = pos.get("tp_level", 0)
        score = pos.get("score", "?")
        live = await live_price(sym)
        if entry and live:
            pnl_pct = ((live - entry) / entry * 100) if side == "LONG" else ((entry - live) / entry * 100)
            pnl_str = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
        else:
            pnl_str, pnl_emoji = "N/A", "⚪"
        side_emoji = "🔼" if side == "LONG" else "🔽"
        lines.append(
            f"{side_emoji} <b>{sym.split('/')[0]}</b> {side}\n"
            f"  Entry: <code>${entry:.4f}</code>  Live: <code>${live:.4f}</code>\n"
            f"  {pnl_emoji} PnL: {pnl_str}  Qty: {qty}\n"
            f"  SL watch: <code>${stop:.4f}</code>  TP: <code>${tp:.4f}</code>\n"
            f"  Score: {score}/8\n"
        )
    return "\n".join(lines), "HTML"


async def _report_text():
    trades = load_jsonl(STATE_DIR / "trade-signals.jsonl")
    closed = [t for t in trades if t.get("type") == "close"]
    if not closed:
        return "📋 No closed trades yet.", "HTML"
    recent = closed[-30:]
    wins = [t for t in recent if t.get("pnl_pct", 0) > 0]
    total_pnl = sum(t.get("pnl_pct", 0) for t in recent)
    win_rate = len(wins) / len(recent) * 100
    lines = [
        f"📋 <b>Trade Report</b> (last {len(recent)})\n",
        f"Win rate: {win_rate:.0f}%  |  Total PnL: {total_pnl:+.2f}%\n",
    ]
    for t in reversed(recent[-15:]):
        date = t.get("closed_at", "")[:10]
        sym = t.get("symbol", "?").split("/")[0]
        side = t.get("side", "?").upper()
        pnl = t.get("pnl_pct", 0)
        outcome = t.get("outcome", "?")
        emoji = "✅" if pnl > 0 else "❌"
        lines.append(f"{emoji} {date} | {sym} {side} | {pnl:+.2f}% | {outcome}")
    return "\n".join(lines), "HTML"


def _system_status_lines():
    """One-glance safety state: pause, circuit breaker, open close requests."""
    out = []
    pause = load_json(STATE_DIR / "pause.json", {})
    out.append(f"⏸ <b>PAUSED</b> — {pause.get('reason', '?')} (since {str(pause.get('since', '?'))[:16]}Z)"
               if pause.get("paused") else "▶️ Entries allowed")
    cb = load_json(STATE_DIR / "circuit-breaker.json", {})
    if cb.get("halted"):
        out.append("🛑 <b>Circuit breaker HALTED</b> — no new entries")
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    for base, r in reqs.items():
        state = "rejected" if r.get("rejected") else "waiting for you"
        what = f"partial {r['qty']}" if r.get("qty") else "full"
        out.append(f"⚠️ {base} {r.get('side', '')} close request ({r.get('reason', '')}, {what}) — {state}")
    return out


async def _monitor_text():
    pending = load_json(STATE_DIR / "pending-orders.json", {})
    regime_data = load_json(STATE_DIR / "regime.json", {})
    regime_str = regime_data.get("regime", "—")
    regime_conf = regime_data.get("confidence", 0)
    status = "\n".join(_system_status_lines())

    if not pending:
        return (
            f"👁 <b>Monitor</b>  |  Regime: <b>{regime_str}</b> ({regime_conf}/10)\n{status}\n\n"
            "No pending entry orders.", "HTML"
        )

    # Fetch live prices concurrently
    syms = {order.get("symbol", oid): None for oid, order in pending.items()}
    prices = {}
    for sym in syms:
        prices[sym] = await live_price(sym)

    # Build rows with distance to fill
    rows = []
    for oid, order in pending.items():
        sym = order.get("symbol", oid)
        side = order.get("side", "?").upper()
        lmt = order.get("limit_price", 0)
        score = order.get("score", 0)
        entered_at = order.get("entered_at", "")
        live = prices.get(sym, 0)

        if lmt and live:
            dist_pct = abs(live - lmt) / live * 100
            # For LONG: fill when price drops to limit → dist = (live - lmt) / live
            # For SHORT: fill when price rises to limit → dist = (lmt - live) / live
            if side == "LONG":
                signed_dist = (live - lmt) / live * 100
                direction = "↓ needs" if signed_dist > 0 else "↑ past"
            else:
                signed_dist = (lmt - live) / live * 100
                direction = "↑ needs" if signed_dist > 0 else "↓ past"
        else:
            dist_pct, signed_dist, direction = None, None, "?"

        rows.append((dist_pct if dist_pct is not None else 999, oid, order, sym, side, lmt, live, score, entered_at, signed_dist, direction))

    # Sort by proximity (closest fill first)
    rows.sort(key=lambda r: r[0])

    lines = [f"👁 <b>Pending Orders — Near Fill</b>  |  Regime: <b>{regime_str}</b> ({regime_conf}/10)\n{status}\n"]
    for dist_pct, oid, order, sym, side, lmt, live, score, entered_at, signed_dist, direction in rows:
        base = sym.split("/")[0]
        if dist_pct is not None:
            if dist_pct < 0.2:
                bar = "🔥🔥🔥"
            elif dist_pct < 0.5:
                bar = "🔥🔥"
            elif dist_pct < 1.0:
                bar = "🔥"
            elif dist_pct < 2.0:
                bar = "🟡"
            else:
                bar = "🔵"
            dist_str = f"{dist_pct:.2f}% away"
        else:
            bar, dist_str = "⬜", "no price"

        side_emoji = "🟢" if side == "LONG" else "🔴"
        score_str = f"  score {score}/8" if score else ""
        lines.append(
            f"\n{bar} {side_emoji} <b>{base}</b> {side}  <code>${lmt:.4f}</code>\n"
            f"  Live: <code>${live:.4f}</code>  {direction} {dist_str}{score_str}"
        )

    return "\n".join(lines), "HTML"


async def _metrics_text():
    trades = load_jsonl(STATE_DIR / "trade-signals.jsonl")
    closed = [t for t in trades if t.get("type") == "close"]
    if not closed:
        return "📈 No trade data yet.", "HTML"
    pnls = [t.get("pnl_pct", 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    rr = abs(avg_win / avg_loss) if avg_loss else 0
    win_rate = len(wins) / len(pnls) * 100
    now = datetime.now(timezone.utc)
    def period_pnl(days):
        cutoff = (now - timedelta(days=days)).isoformat()[:10]
        subset = [t.get("pnl_pct", 0) for t in closed if t.get("closed_at", "") >= cutoff]
        return sum(subset), len(subset)
    p7, c7 = period_pnl(7)
    p30, c30 = period_pnl(30)
    return (
        f"📈 <b>Performance Metrics</b>\n\n"
        f"<b>All time</b>: {len(closed)} trades\n"
        f"Win rate: {win_rate:.0f}%  ({len(wins)}W / {len(losses)}L)\n"
        f"Avg win: +{avg_win:.2f}%  |  Avg loss: {avg_loss:.2f}%\n"
        f"R:R ratio: {rr:.2f}\n"
        f"Total PnL: {sum(pnls):+.2f}%\n"
        f"Best: +{max(pnls):.2f}%  |  Worst: {min(pnls):.2f}%\n\n"
        f"<b>7 days</b>: {p7:+.2f}% ({c7} trades)\n"
        f"<b>30 days</b>: {p30:+.2f}% ({c30} trades)"
    ), "HTML"


# (unit, label, max minutes since its last run before it counts as stale)
POLAR_JOBS = [("polar-guardian", "guardian", 40), ("polar-scanner", "scanner", 40), ("polar-portfolio", "portfolio", 90),
              ("polar-regime", "regime", 300), ("polar-audit", "audit", 180), ("polar-backup", "backup", 1700),
              ("polar-update-check", "update check", 11000)]


def _job_line(unit, label, max_min):
    try:
        r = subprocess.run(["systemctl", "--user", "show", f"{unit}.service", "-p", "Result,ExecMainExitTimestamp"],
                           capture_output=True, text=True, timeout=4)
        kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        t = subprocess.run(["systemctl", "--user", "is-active", f"{unit}.timer"], capture_output=True, text=True, timeout=3)
        if t.stdout.strip() != "active":
            return f"🔴 {label}: timer not active"
        ts = kv.get("ExecMainExitTimestamp", "").strip()
        if not ts:
            return f"⚪ {label}: not run yet"
        when = subprocess.run(["date", "-d", ts, "+%s"], capture_output=True, text=True, timeout=3).stdout.strip()
        age = (datetime.now(timezone.utc).timestamp() - float(when)) / 60
        ok = kv.get("Result") == "success"
        stale = age > max_min
        icon = "🟢" if ok and not stale else "🔴"
        ago = f"{age:.0f} min ago" if age < 120 else f"{age/60:.0f} h ago"
        return f"{icon} {label}: last run {ago}" + ("" if ok else f" — FAILED ({kv.get('Result')})") + (" — STALE" if stale else "")
    except Exception as e:
        return f"⚪ {label}: {str(e)[:40]}"


async def _services_text():
    lines = ["⚙️ <b>Services Status</b>\n"]
    try:
        r = subprocess.run(["openalice", "status"], capture_output=True, text=True, timeout=5)
        running = "running" in r.stdout.lower()
        version = next((l.split(":", 1)[1].strip() for l in r.stdout.splitlines() if "Version:" in l), "")
        lines.append(f"{'🟢' if running else '🔴'} OpenAlice Runtime {version}")
    except Exception as e:
        lines.append(f"🔴 OpenAlice Runtime — {e}")
    for svc in ["openalice", "polar-bot", "polar-user-bot"]:
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", f"{svc}.service"],
                capture_output=True, text=True, timeout=3,
            )
            active = r.stdout.strip() == "active"
            lines.append(f"{'🟢' if active else '🔴'} {svc}.service")
        except Exception:
            lines.append(f"⚪ {svc}.service")
    lines.append("\n🤖 <b>Trading jobs</b>")
    lines += [_job_line(*j) for j in POLAR_JOBS]
    try:
        active_issues = []
        for f in ISSUES_DIR.glob("*.md"):
            content = f.read_text()
            if ("status: todo" in content or "status: in_progress" in content) and "when:" in content:
                active_issues.append(f.stem)
        lines.append(f"\n📋 OpenAlice scheduled issues: {len(active_issues)}")
        for iss in active_issues:
            lines.append(f"  • {iss} (weekly AI report)" if iss == "performance-reporter" else f"  • {iss}")
    except Exception:
        pass
    return "\n".join(lines), "HTML"


# ── Decisions ⚖️ ─────────────────────────────────────────────────────────────
# Every position the guardian is asking about, with three answers:
#   Close  -> approval (asks to confirm first)      Leave -> "no" for now, asked again after 6 h
#   Leave and don't ask me again -> muted for this position until it is closed (asks to confirm first)
DECISION_KINDS = ("dc", "dcy", "dl", "dm", "dmy", "dback")
REASON_WORDS = {
    "stop_breach": "the price is past the stop level",
    "regime_shift": "the market regime turned against this position",
    "partial_1R": "it reached 1R profit, so take half off",
}


def _mutate(name, fn):
    """Read-modify-write of a state file under the same lock the guardian uses."""
    import fcntl
    path = STATE_DIR / name
    with open(STATE_DIR / ".state.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = json.loads(path.read_text()) if path.exists() else {}
        result = fn(cur)
        _write_json(path, cur)
        return result


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _approve_close(base):
    """Lorenzo's explicit Close. Returns False when the request no longer exists."""
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    if base not in reqs:
        return False
    qty = reqs[base].get("qty")
    _mutate("close-approvals.json", lambda a: a.__setitem__(base, {"approved_at": _now_iso(), "by": "lorenzo", "qty": qty}))
    return True


def _leave(base, mute=False):
    def f(reqs):
        if base not in reqs:
            return False
        reqs[base]["rejected"], reqs[base]["rejected_at"] = True, _now_iso()
        if mute:
            reqs[base]["muted"], reqs[base]["muted_at"] = True, _now_iso()
        return True
    ok = _mutate("close-requests.json", f)
    if ok:
        _clear_pause_if_answered()
    return ok


def _find_position(base):
    for p in load_json(STATE_DIR / "open-positions.json", {}).values():
        if p.get("symbol", "").split("/")[0] == base:
            return p
    return None


def _ago(ts):
    try:
        m = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 60
        return f"{m:.0f} min" if m < 120 else (f"{m/60:.0f} h" if m < 2880 else f"{m/1440:.0f} days")
    except Exception:
        return "?"


def _decision_kb(base, r):
    close = InlineKeyboardButton("🔴 Close", callback_data=f"dc:{base}")
    leave = InlineKeyboardButton("🟢 Leave", callback_data=f"dl:{base}")
    mute = InlineKeyboardButton("🔕 Leave and don't ask me again", callback_data=f"dm:{base}")
    if r.get("muted"):
        return InlineKeyboardMarkup([[close]])
    if r.get("rejected"):
        return InlineKeyboardMarkup([[close], [mute]])
    return InlineKeyboardMarkup([[close, leave], [mute]])


async def _decision_card(base, r):
    p = _find_position(base)
    if not p:
        return None
    side = p.get("side", "?").upper()
    entry = p.get("entry_price", 0)
    live = await live_price(p["symbol"])
    if entry and live:
        pnl = ((live - entry) if side == "LONG" else (entry - live)) / entry * 100
        now_line = f"Now <code>${live:.4f}</code>  {'🟢' if pnl >= 0 else '🔴'} {pnl:+.2f}%"
    else:
        now_line = "Now: no price"
    what = f"PARTIAL: close {r['qty']} of {p.get('qty')}" if r.get("qty") else "FULL close of the whole position"
    if r.get("muted"):
        status = "🔕 Muted — I will not ask about this position again (liquidation warnings still come)."
    elif r.get("rejected"):
        status = f"🟢 You chose Leave {_ago(r.get('rejected_at', ''))} ago. I may ask again after 6 h if it is still true."
    else:
        status = f"⏳ Waiting for your decision (asked {_ago(r.get('requested_at', ''))} ago)."
    text = (f"⚖️ <b>{base} {side}</b> — I am asking to close because {REASON_WORDS.get(r.get('reason'), r.get('reason', '?'))}.\n"
            f"Entry <code>${entry:.4f}</code>  {now_line}\n"
            f"Stop watch <code>${p.get('stop_level', 0):.4f}</code>  TP <code>${p.get('tp_level', 0):.4f}</code>\n"
            f"If you tap Close: <b>{what}</b>.\n{status}")
    return text, _decision_kb(base, r)


async def _send_decisions(message):
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    order = sorted(reqs.items(), key=lambda kv: (bool(kv[1].get("rejected")), kv[1].get("requested_at", "")))
    cards = [c for c in [await _decision_card(b, r) for b, r in order] if c]
    if not cards:
        n = len(load_json(STATE_DIR / "open-positions.json", {}))
        await message.reply_text(f"⚖️ No decisions waiting. {n} open position(s), all inside their rules.")
        return
    waiting = sum(1 for b, r in order if not r.get("rejected") and _find_position(b))
    await message.reply_text(f"⚖️ <b>Decisions</b> — {len(cards)} position(s), {waiting} waiting for you.\n"
                             "Nothing is closed unless you tap Close and confirm.", parse_mode="HTML")
    for text, kb in cards:
        await message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def _decision_button(query, action):
    kind, _, base = action.partition(":")
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    if base not in reqs or not _find_position(base):
        await query.edit_message_text(f"⚖️ {base}: nothing to decide any more (the request is gone). Nothing changed.")
        return
    r = reqs[base]
    old = query.message.text_html or query.message.text or ""
    base_text = old.split("\n\n<b>Confirm")[0].split("\n\n✅")[0].split("\n\n🟢 <b>Left")[0].split("\n\n🔕 <b>Muted")[0]
    if kind == "dc":
        what = f"PARTIAL close of {r['qty']}" if r.get("qty") else "FULL close"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Yes, close {base}", callback_data=f"dcy:{base}"),
                                    InlineKeyboardButton("↩️ Back", callback_data=f"dback:{base}")]])
        await query.edit_message_text(base_text + f"\n\n<b>Confirm {what} of {base} at market?</b>", parse_mode="HTML", reply_markup=kb)
    elif kind == "dcy":
        if _approve_close(base):
            await query.edit_message_text(base_text + f"\n\n✅ <b>Approved.</b> The guardian closes {base} on its next run (it runs every 5 minutes, so expect it within about 6).",
                                          parse_mode="HTML")
    elif kind == "dl":
        _leave(base)
        await query.edit_message_text(base_text + f"\n\n🟢 <b>Left.</b> {base} stays open; I will not ask again for 6 hours.", parse_mode="HTML")
    elif kind == "dm":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, never ask about it", callback_data=f"dmy:{base}"),
                                    InlineKeyboardButton("↩️ Back", callback_data=f"dback:{base}")]])
        await query.edit_message_text(
            base_text + f"\n\n<b>Confirm: stop asking about {base}?</b> You will get NO close requests for it "
            "(stop, regime change, partial) until it closes. Liquidation warnings still come.", parse_mode="HTML", reply_markup=kb)
    elif kind == "dmy":
        _leave(base, mute=True)
        await query.edit_message_text(base_text + f"\n\n🔕 <b>Muted.</b> {base} stays open and I will not ask about it again. "
                                      "You can still close it from Decisions ⚖️.", parse_mode="HTML")
    elif kind == "dback":
        card = await _decision_card(base, load_json(STATE_DIR / "close-requests.json", {}).get(base, r))
        if card:
            await query.edit_message_text(card[0], parse_mode="HTML", reply_markup=card[1])


@only_owner
async def cmd_decisions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_decisions(update.message)


@only_owner
async def handle_chat_bar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    action = CHAT_BAR_ACTIONS.get(update.message.text)
    if not action:
        return
    if action == "decisions":
        await _send_decisions(update.message)
        return
    dispatch = {
        "positions": _positions_text,
        "report":    _report_text,
        "monitor":   _monitor_text,
        "metrics":   _metrics_text,
        "services":  _services_text,
    }
    text, parse_mode = await dispatch[action]()
    await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=MAIN_KEYBOARD)


@only_owner
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, mode = await _positions_text()
    await update.message.reply_text(text, parse_mode=mode, reply_markup=MAIN_KEYBOARD)


@only_owner
async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, mode = await _report_text()
    await update.message.reply_text(text, parse_mode=mode, reply_markup=MAIN_KEYBOARD)


@only_owner
async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, mode = await _monitor_text()
    await update.message.reply_text(text, parse_mode=mode, reply_markup=MAIN_KEYBOARD)


@only_owner
async def cmd_metrics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, mode = await _metrics_text()
    await update.message.reply_text(text, parse_mode=mode, reply_markup=MAIN_KEYBOARD)


@only_owner
async def cmd_services(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, mode = await _services_text()
    await update.message.reply_text(text, parse_mode=mode, reply_markup=MAIN_KEYBOARD)


def _write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _pending_requests_text():
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    if not reqs:
        return "No pending close requests."
    lines = ["Pending close requests:"]
    for base, r in reqs.items():
        lines.append(f"• {base} {r.get('side','')} — {r.get('reason','')} (since {r.get('requested_at','?')})")
    lines.append("Use /approve <SYMBOL> or /reject <SYMBOL>.")
    return "\n".join(lines)


async def _close_button(query, action):
    """Inline-button flow for guardian close requests: ap (ask confirm) -> apy (approve), rj (reject), no (cancel)."""
    kind, _, base = action.partition(":")
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    old = query.message.text_html or query.message.text or ""
    if base not in reqs:
        await query.edit_message_text(old + f"\n\n— No pending request for {base} any more. Nothing changed.", parse_mode="HTML")
        return
    if kind == "ap":
        r = reqs[base]
        what = "PARTIAL close of " + str(r.get("qty")) if r.get("qty") else "FULL close"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Yes, approve {base}", callback_data=f"apy:{base}"),
            InlineKeyboardButton("↩️ Cancel", callback_data=f"no:{base}"),
        ]])
        await query.edit_message_text(old + f"\n\n<b>Confirm {what} of {base} at market?</b>", parse_mode="HTML", reply_markup=kb)
    elif kind == "apy":
        approvals = load_json(STATE_DIR / "close-approvals.json", {})
        approvals[base] = {
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "by": "lorenzo",
            "qty": reqs[base].get("qty"),
        }
        _write_json(STATE_DIR / "close-approvals.json", approvals)
        await query.edit_message_text(old + f"\n\n✅ <b>Approved.</b> The guardian closes {base} on its next run (it runs every 5 minutes, so expect it within about 6).", parse_mode="HTML")
    elif kind == "rj":
        reqs[base]["rejected"] = True
        reqs[base]["rejected_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(STATE_DIR / "close-requests.json", reqs)
        cleared = _clear_pause_if_answered()
        await query.edit_message_text(old + f"\n\n🛑 <b>Rejected.</b> {base} stays open; no re-ask for 6 hours."
                                      + ("\n▶️ Pause cleared." if cleared else ""), parse_mode="HTML")
    elif kind == "no":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Approve {base}", callback_data=f"ap:{base}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rj:{base}"),
        ]])
        await query.edit_message_text(old.split("\n\nConfirm")[0], parse_mode="HTML", reply_markup=kb)


def _clear_pause_if_answered():
    """Rejecting the only unanswered request ends the pause that the silence caused."""
    pause = load_json(STATE_DIR / "pause.json", {})
    if not pause.get("paused") or not str(pause.get("reason", "")).startswith("unanswered close request"):
        return False
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    if any(not r.get("rejected") for r in reqs.values()):
        return False
    _write_json(STATE_DIR / "pause.json", {"paused": False, "resumed_at": datetime.now(timezone.utc).isoformat(),
                                           "by": "auto: all close requests answered"})
    return True


@only_owner
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(_pending_requests_text())
        return
    base = ctx.args[0].upper()
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    if base not in reqs:
        await update.message.reply_text(f"No pending close request for {base}. Nothing approved.\n\n" + _pending_requests_text())
        return
    approvals = load_json(STATE_DIR / "close-approvals.json", {})
    approvals[base] = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "by": "lorenzo",
        "qty": reqs[base].get("qty"),
    }
    _write_json(STATE_DIR / "close-approvals.json", approvals)
    await update.message.reply_text(f"✅ Approved close of {base}. The guardian will execute it on its next run (it runs every 5 minutes, so expect it within about 6).")


@only_owner
async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(_pending_requests_text())
        return
    base = ctx.args[0].upper()
    reqs = load_json(STATE_DIR / "close-requests.json", {})
    if base not in reqs:
        await update.message.reply_text(f"No pending close request for {base}.")
        return
    reqs[base]["rejected"] = True
    reqs[base]["rejected_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(STATE_DIR / "close-requests.json", reqs)
    cleared = _clear_pause_if_answered()
    await update.message.reply_text(f"🛑 Rejected. {base} stays open; the guardian will not re-ask for 6 hours."
                                    + ("\n▶️ Pause cleared." if cleared else ""))


@only_owner
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _write_json(STATE_DIR / "pause.json", {"paused": False, "resumed_at": datetime.now(timezone.utc).isoformat()})
    await update.message.reply_text("▶️ Pause cleared. Scanner may place new entries again.\n\n" + _pending_requests_text())


async def _run(app: Application):
    async with app:
        await app.start()
        offset = 0
        print("Polar Bot polling...", flush=True)
        while True:
            try:
                updates = await app.bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
                for upd in updates:
                    offset = upd.update_id + 1
                    asyncio.create_task(app.process_update(upd))
            except telegram.error.Conflict:
                print("Conflict: waiting 45s for other instance to die...", flush=True)
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
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("report",    cmd_report))
    app.add_handler(CommandHandler("monitor",   cmd_monitor))
    app.add_handler(CommandHandler("metrics",   cmd_metrics))
    app.add_handler(CommandHandler("services",  cmd_services))
    app.add_handler(CommandHandler("approve",   cmd_approve))
    app.add_handler(CommandHandler("reject",    cmd_reject))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(CommandHandler("decisions", cmd_decisions))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_chat_bar,
    ))
    print("Polar Bot starting...", flush=True)
    asyncio.run(_run(app))


if __name__ == "__main__":
    main()
