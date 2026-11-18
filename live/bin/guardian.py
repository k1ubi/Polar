#!/usr/bin/env python3
"""Polar guardian. Same rules as position-guardian.md.

  python3 guardian.py          dry-run: print the plan, change NOTHING
  python3 guardian.py --live   act

HARD RULE (Lorenzo, 2026-10-18): this script never closes, partially closes or reduces a
position on its own. Stops, regime flips and 1R partials only create an approval request
(Telegram buttons + close-requests.json). `close_position` appears in exactly one place,
execute_approved(), and only for a symbol present in close-approvals.json, which is written
by Lorenzo's Telegram tap, never by this script. Silence is not approval: a request unanswered
for 30 minutes writes pause.json and keeps asking."""
import json, sys, time, urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import fcntl, copy

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import broker as _broker
import indicators as _ind
from config import TG_TOKEN, TG_CHAT

STATE = Path("/home/kyubi/Polar/live/state")
LIVE = "--live" in sys.argv
REJECT_QUIET = timedelta(hours=6)
RE_ALERT = timedelta(minutes=30)


# ---------- helpers ----------

def now():
    return datetime.now(timezone.utc)


def z(d=None):
    return (d or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def rj(name, default):
    try:
        return json.loads((STATE / name).read_text())
    except Exception:
        return default


def wj(name, data):
    if not LIVE:
        return
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE / name)


def commit(name, before, after):
    """Merge only THIS run's changes onto the live file, under a lock."""
    if not LIVE or before == after:
        return
    with open(STATE / ".state.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = rj(name, {})
        for k in set(before) - set(after):
            cur.pop(k, None)
        for k, v in after.items():
            if before.get(k) != v:
                cur[k] = v
        tmp = STATE / (name + ".tmp")
        tmp.write_text(json.dumps(cur, indent=2))
        tmp.replace(STATE / name)


def log(msg):
    print(("" if LIVE else "[dry] ") + msg, flush=True)


def tg(text, buttons=None):
    if not LIVE:
        log(f"telegram: {text}" + (f"  buttons={buttons}" if buttons else ""))
        return
    payload = {"chat_id": TG_CHAT, "parse_mode": "HTML", "text": text}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": buttons if isinstance(buttons[0], list) else [buttons]
        }
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json.dumps(payload).encode(), {"Content-Type": "application/json"}),
            timeout=15).read()
    except Exception as e:
        print("telegram failed:", e, file=sys.stderr)


def base_of(sym):
    return sym.split("/")[0]


def symbol_of(aid: str) -> str:
    """'bitget-bf693fe4|BTC/USDT:USDT' -> 'BTC/USDT:USDT'"""
    return aid.split("|", 1)[1]


def aid_of(sym):
    return f"{_broker.SRC}|{sym}"


def decimals(x):
    s = str(x)
    return len(s.split(".")[1]) if "." in s else 0


def qty_decimals(x):
    return max(0, -Decimal(str(x)).normalize().as_tuple().exponent)


_EX = None


def _ex():
    global _EX
    if _EX is None:
        _EX = _broker._ex()
    return _EX


def partial_qty(symbol, held):
    """Half of `held` rounded to lot precision; None if either half is below min lot."""
    try:
        ex = _ex()
        half = float(ex.amount_to_precision(symbol, held * 0.5))
        mn = float((ex.markets.get(symbol) or {}).get("limits", {}).get("amount", {}).get("min") or 0)
    except Exception:
        q = Decimal(1).scaleb(-qty_decimals(held))
        half = float(Decimal(str(held * 0.5)).quantize(q, rounding=ROUND_DOWN))
        mn = 0
    if half <= 0 or half < mn or held - half < mn or half >= held:
        return None
    return half


def closing_fill(aid, side, since):
    """(avg_price, order_id) of the latest closing fill for this contract after `since`, or None."""
    try:
        sym = symbol_of(aid)
        rows = _broker.fetch_my_trades(sym, 100)
        want = "BUY" if side == "SHORT" else "SELL"
        fills = [r for r in rows
                 if r.get("contract", {}).get("aliceId") == aid
                 and r.get("side") == want
                 and parse(r["timestamp"]) > parse(since)]
        if not fills:
            return None
        oid = max(fills, key=lambda r: r["timestamp"])["orderId"]
        same = [r for r in fills if r["orderId"] == oid]
        q = sum(float(r["quantity"]) for r in same)
        return sum(float(r["price"]) * float(r["quantity"]) for r in same) / q, oid
    except Exception:
        return None


def place_tp(aid, action, price, qty, msg):
    """Place a reduce-only TP limit order. Returns order id or None in dry-run."""
    log(f"PLACE TP {action} LMT {qty} @ {price} on {aid}")
    if LIVE:
        return _broker.place_tp_order(symbol_of(aid), action, price, float(qty), msg)
    return None


def cancel(order_id, symbol, msg):
    log(f"CANCEL order {order_id}")
    if LIVE:
        _broker.cancel_order(str(order_id), symbol, msg)


def cancel_leftovers(aid, base):
    sym = symbol_of(aid)
    orders = _broker.order_list([sym]) if LIVE else []
    for o in orders:
        if o["aliceId"] == aid:
            cancel(o["orderId"], sym, f"Guardian cleanup {base}")


def record_close(p, exit_price, outcome):
    short = p["side"] == "SHORT"
    e = p["entry_price"]
    pnl = ((e - exit_price) if short else (exit_price - e)) / e * 100
    rec = {"type": "close", "symbol": p["symbol"], "side": p["side"], "entry_price": e,
           "exit_price": exit_price, "pnl_pct": round(pnl, 3), "outcome": outcome,
           "regime": p.get("regime"), "score": p.get("score"),
           "entered_at": p.get("entered_at"), "closed_at": z()}
    log(f"RECORD {json.dumps(rec)}")
    if LIVE:
        with open(STATE / "trade-signals.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    return pnl


def append_signal(rec):
    log(f"RECORD {json.dumps(rec)}")
    if LIVE:
        with open(STATE / "trade-signals.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


def update_metrics():
    wins, losses = [], []
    try:
        for line in (STATE / "trade-signals.jsonl").read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("type") == "close":
                    (wins if r["pnl_pct"] > 0 else losses).append(r["pnl_pct"])
    except FileNotFoundError:
        return
    n = len(wins) + len(losses)
    if not n:
        return
    allp = wins + losses
    aw = sum(wins) / len(wins) if wins else 0
    al = sum(losses) / len(losses) if losses else 0
    mc = rj("metrics-cache.json", {})
    mc.update({"total_trades": n, "wins": len(wins), "losses": len(losses),
               "win_rate_pct": round(len(wins) / n * 100, 1), "avg_win_pct": round(aw, 3),
               "avg_loss_pct": round(al, 3), "rr_ratio": round(abs(aw / al), 3) if al else None,
               "total_pnl_pct": round(sum(allp), 3), "best_trade_pct": max(allp),
               "worst_trade_pct": min(allp), "updated_at": z()})
    wj("metrics-cache.json", mc)


# ---------- approval requests ----------

def request_close(base, side, reason, price, entry, upnl, requests, qty=None):
    r = requests.get(base)
    if r and r.get("muted"):
        return
    if r and r.get("rejected"):
        if now() - parse(r["rejected_at"]) < REJECT_QUIET:
            return
        r = None
    if r and r.get("reason") == reason:
        if now() - parse(r.get("last_alert_at", r["requested_at"])) >= RE_ALERT:
            r["last_alert_at"] = z()
            send_request(base, side, reason, price, entry, upnl, r.get("qty"))
        return
    requests[base] = {"reason": reason, "side": side, "price": price, "requested_at": z(),
                      "last_alert_at": z(), **({"qty": qty} if qty else {})}
    send_request(base, side, reason, price, entry, upnl, qty)


def send_request(base, side, reason, price, entry, upnl, qty=None):
    what = f"PARTIAL: closes {qty} only" if qty else "FULL close of the whole position"
    tg(f"⚠️ <b>CLOSE REQUEST</b> | {base} {side} | {reason} | price ${price}, entry ${entry}, "
       f"uPnL {upnl:+.2f}%\nClose = {what}. Nothing has been closed. Tap a button below.",
       [[{"text": "🔴 Close", "callback_data": f"dc:{base}"},
         {"text": "🟢 Leave", "callback_data": f"dl:{base}"}],
        [{"text": "🔕 Leave and don't ask me again", "callback_data": f"dm:{base}"}]])


def approval_problem(base, appr, requests):
    r = requests.get(base)
    if not r:
        return "there is no open request for it"
    try:
        at = parse(appr["approved_at"])
        if at < parse(r["requested_at"]):
            return "it is older than the current request"
        if r.get("rejected") and at <= parse(r["rejected_at"]):
            return "the request was rejected after it"
    except Exception:
        return "its timestamp is unreadable"
    if float(appr.get("qty") or 0) != float(r.get("qty") or 0):
        return "its size differs from the request"
    return None


# ---------- the only place a close can happen ----------

def execute_approved(base, appr, p, opens, key, consume, price):
    aid = aid_of(p["symbol"])
    sym = symbol_of(aid)
    qty = appr.get("qty")
    log(f"EXECUTE APPROVED CLOSE {base} qty={qty or 'full'} "
        f"(approved {appr.get('approved_at')} by {appr.get('by')})")
    if not LIVE:
        return False
    try:
        _broker.close_position(sym, qty=float(qty) if qty else None,
                               comment=f"Guardian close {base} approved by Lorenzo")
    except Exception as ex:
        tg(f"🚨 <b>{base} close FAILED</b>: {ex}. Nothing was closed.")
        return False
    consume()
    try:
        if not qty:
            fill = closing_fill(aid, p["side"], p.get("entered_at") or "1970-01-01T00:00:00Z")
            mp = fill[0] if fill else float(rj("last-prices.json", {}).get(base, p["entry_price"]))
            record_close(p, mp, "MANUAL_APPROVED")
            cancel_leftovers(aid, base)
        else:
            for o in _broker.order_list([sym]):
                if o["aliceId"] == aid:
                    cancel(o["orderId"], sym, f"Guardian cancel TP {base} before partial re-TP")
            fraction = float(qty) / p["qty"]
            remaining = round(p["qty"] - float(qty), 8)
            sl, e = p["sl_distance_pct"], p["entry_price"]
            tp = round(e * (1 - 3 * sl) if p["side"] == "SHORT" else e * (1 + 3 * sl),
                       decimals(p["tp_level"]))
            p.update({"qty": remaining, "partial_tp_done": True})
            place_tp(aid, "BUY" if p["side"] == "SHORT" else "SELL", tp, remaining,
                     f"Guardian TP {base} 3R after partial")
            p["tp_level"] = tp
            append_signal({"type": "partial_close", "symbol": p["symbol"], "side": p["side"],
                           "qty": qty, "fraction": round(fraction, 6), "tp_level": tp,
                           "price": price, "closed_at": z()})
    except Exception as ex:
        tg(f"🚨 <b>{base}: close went through, follow-up failed</b> ({ex}). "
           f"Check the TP order for what is left; the close will NOT be repeated.")
    if not qty:
        opens.pop(key, None)
    tg(f"✅ <b>CLOSED (approved)</b> | {base}" + (f" partial {qty}" if qty else ""))
    return True


# ---------- main pass ----------

def run():
    opens = rj("open-positions.json", {})
    pend  = rj("pending-orders.json", {})
    approvals = rj("close-approvals.json", {})
    requests  = rj("close-requests.json", {})
    snap = {n: copy.deepcopy(v) for n, v in (
        ("open-positions.json", opens), ("pending-orders.json", pend),
        ("close-approvals.json", approvals), ("close-requests.json", requests))}
    regime = rj("regime.json", {}).get("regime", "UNKNOWN")
    if not opens and not pend and not approvals:
        return log("nothing to manage")

    # pause check
    pause = rj("pause.json", {})
    unanswered = sorted(b for b, r in requests.items()
                        if not r.get("rejected") and b not in approvals)
    if pause.get("paused") and str(pause.get("reason", "")).startswith("unanswered close request") \
            and unanswered:
        want = "unanswered close requests: " + ", ".join(unanswered)
        if pause.get("reason") != want:
            pause = {**pause, "reason": want}
            wj("pause.json", pause)
    for b, r in requests.items():
        if not r.get("rejected") and b not in approvals \
                and now() - parse(r["requested_at"]) > RE_ALERT \
                and not pause.get("paused"):
            wj("pause.json", {"paused": True,
                               "reason": f"unanswered close request {b}", "since": z()})
            tg(f"⏸ <b>PAUSED</b> | no answer on the {b} close request after 30 min. "
               f"No new entries. Use the buttons above, then /resume.")
            pause = {"paused": True}

    def broker_view():
        positions = _broker.account_portfolio()
        orders    = _broker.order_list()
        return positions, orders

    positions, orders = broker_view()
    have    = {(x["aliceId"], x["side"].lower()) for x in positions}
    vanished = any((aid_of(p["symbol"]), p["side"].lower()) not in have for p in opens.values())
    unseen   = any((aid_of(e["symbol"]), e["side"].lower()) not in have
                   and not any(str(o["orderId"]) == str(e.get("order_id")) for o in orders)
                   for e in pend.values())
    if vanished or unseen:
        time.sleep(4)
        positions, orders = broker_view()
    on_broker = {(p["aliceId"], p["side"].lower()): p for p in positions}
    changed = False

    # pending entries
    for key, e in list(pend.items()):
        base = base_of(e["symbol"])
        aid  = aid_of(e["symbol"])
        pos  = on_broker.get((aid, e["side"].lower()))
        resting = [o for o in orders if o["aliceId"] == aid
                   and str(o["orderId"]) == str(e.get("order_id"))]
        if pos:
            if resting:
                try:
                    cancel(e["order_id"], symbol_of(aid),
                           f"Guardian cancel unfilled rest of entry {base}")
                    if LIVE:
                        fresh = _broker.account_portfolio()
                        pos = next((x for x in fresh
                                    if x["aliceId"] == aid
                                    and x["side"].lower() == e["side"].lower()), pos)
                except Exception as ex:
                    tg(f"🚨 <b>{base}</b> entry only partly filled and the rest could not be "
                       f"cancelled: {ex}")
            entry = float(pos["avgCost"])
            qty   = float(pos["quantity"])
            sl, short = e["sl_distance_pct"], e["side"] == "SHORT"
            stop = entry * (1 + sl) if short else entry * (1 - sl)
            dp   = max(decimals(e["limit_price"]), 2)
            tp   = round(entry * (1 - 2 * sl) if short else entry * (1 + 2 * sl), dp)
            tp_id = None
            try:
                tp_id = place_tp(aid, "BUY" if short else "SELL", tp, pos["quantity"],
                                 f"Guardian TP {base} {e['side']} after fill")
            except Exception as ex:
                tg(f"🚨 <b>TP NOT PLACED</b> | {base} {e['side']} filled @ {entry} "
                   f"but the TP order failed: {ex}")
            opens[f"{base}_{int(now().timestamp())}"] = {
                "symbol": e["symbol"], "side": e["side"], "entry_price": entry, "qty": qty,
                "stop_level": stop, "tp_level": tp, "sl_distance_pct": sl,
                "atr14_15m": e.get("atr14_15m"), "ema20_4h": e.get("ema20_4h"),
                "score": e.get("score"), "regime": e.get("regime"),
                "entered_at": z(), "tp_order_id": tp_id, "partial_tp_done": False,
            }
            pend.pop(key)
            append_signal({"type": "entry", "symbol": e["symbol"], "side": e["side"],
                           "entry_price": entry, "qty": qty, "score": e.get("score"),
                           "regime": e.get("regime"), "entered_at": z(),
                           "stop_level": stop, "tp_level": tp, "sl_distance_pct": sl})
            tg(f"✅ <b>FILLED</b> | {base} {e['side']} @ ${entry} | TP ${tp} | stop watch ${stop:.6g}")
            changed = True
        elif not resting:
            log(f"entry gone {base}: remove from pending")
            append_signal({"type": "skipped", "symbol": e["symbol"], "reason": "entry_not_open"})
            pend.pop(key)
            changed = True
        elif now() - parse(e["placed_at"]) > timedelta(hours=24):
            # 4h/daily strategy: entries are limit orders near EMA — give them 24h to fill
            cancel(e["order_id"], symbol_of(aid), f"Guardian cancel stale entry {base}")
            append_signal({"type": "skipped", "symbol": e["symbol"], "reason": "entry_expired"})
            pend.pop(key)
            changed = True

    # ATR map for trailing stops
    atr_map = {}
    if opens:
        bases = sorted({base_of(p["symbol"]) for p in opens.values()})
        try:
            atr_map = _ind.compute_atr_map(_ex(), bases)
        except Exception as ex:
            log(f"WARNING no ATR this run (trailing skipped): {ex}")

    def manage(key, p):
        base, side, aid = base_of(p["symbol"]), p["side"], aid_of(p["symbol"])
        sym   = symbol_of(aid)
        short = side == "SHORT"
        bpos  = on_broker.get((aid, side.lower()))
        if not bpos:
            fill = closing_fill(aid, side, p.get("entered_at") or "1970-01-01T00:00:00Z")
            if fill:
                exit_price, oid = fill
                tp_hit = str(oid) == str(p.get("tp_order_id")) or \
                    abs(exit_price - p["tp_level"]) / p["tp_level"] <= 0.002
                outcome = "TP_HIT" if tp_hit else "CLOSED_EXTERNAL"
            else:
                mp = float(rj("last-prices.json", {}).get(base, p["entry_price"]))
                tp_hit = mp <= p["tp_level"] * 1.001 if short else mp >= p["tp_level"] * 0.999
                outcome = "TP_HIT" if tp_hit else "CLOSED_UNKNOWN"
                exit_price = p["tp_level"] if tp_hit else mp
            pnl = record_close(p, exit_price, outcome)
            try:
                cancel_leftovers(aid, base)
            except Exception as ex:
                tg(f"🚨 <b>{base}</b> is closed but leftover orders could not be cancelled: {ex}")
            tg(f"{'✅ TP HIT' if tp_hit else 'ℹ️ CLOSED'} | {base} {side} | "
               f"pnl {pnl:+.2f}% | exit {exit_price:.6g} ({outcome})")
            opens.pop(key)
            requests.pop(base, None)
            approvals.pop(base, None)
            return True
        changed = False
        price = float(bpos["marketPrice"])
        lp = rj("last-prices.json", {})
        lp[base] = price
        wj("last-prices.json", lp)
        entry, sl, stop = p["entry_price"], p["sl_distance_pct"], p["stop_level"]
        upnl = ((entry - price) if short else (price - entry)) / entry * 100
        log(f"{base} {side} price {price} entry {entry} stop {stop} uPnL {upnl:+.2f}% regime {regime}")

        liq = float((bpos.get("risk") or {}).get("liquidationPrice") or 0)
        if liq:
            dist = ((liq - price) if short else (price - liq)) / price
            if dist < 0.30 and now() - parse(p.get("liq_alert_at") or "1970-01-01T00:00:00Z") > timedelta(hours=2):
                p["liq_alert_at"] = z()
                tg(f"🔥 <b>LIQUIDATION WATCH</b> | {base} {side} | price ${price}, "
                   f"liquidation ${liq:.6g} ({dist*100:.0f}% away). Information only.")
                changed = True

        if base in approvals:
            why = approval_problem(base, approvals[base], requests)
            if why:
                tg(f"⚠️ Your approval for <b>{base}</b> was IGNORED: {why}. Nothing was closed. "
                   f"Tap Approve again on the current request if you still want it.")
                approvals.pop(base)
                changed = True
            else:
                def consume():
                    approvals.pop(base, None)
                    requests.pop(base, None)
                    commit("close-approvals.json", snap["close-approvals.json"], approvals)
                    commit("close-requests.json", snap["close-requests.json"], requests)
                    snap["close-approvals.json"] = copy.deepcopy(approvals)
                    snap["close-requests.json"]  = copy.deepcopy(requests)
                if execute_approved(base, approvals[base], p, opens, key, consume, price):
                    return True
                return changed

        tp_orders = [o for o in orders if o["aliceId"] == aid
                     and o["action"] == ("BUY" if short else "SELL")]
        if not tp_orders:
            log(f"WARNING {base}: no TP order resting")
        breach = price >= stop if short else price <= stop
        shift  = (not short and regime == "BEARISH") or (short and regime == "BULLISH")
        if breach:
            request_close(base, side, "stop_breach", price, entry, upnl, requests)
            return True
        if shift:
            request_close(base, side, "regime_shift", price, entry, upnl, requests)
            return True

        favour = ((entry - price) if short else (price - entry)) / entry
        if not p.get("breakeven_done") and favour >= sl:
            p["stop_level"], p["breakeven_done"] = entry, True
            tg(f"🛡 <b>BREAKEVEN</b> | {base} | stop moved to entry ${entry}")
            changed = True
        a = atr_map.get(base)
        if p.get("breakeven_done") and a:
            new = price + a if short else price - a
            cur = p["stop_level"]
            if (short and new < cur) or (not short and new > cur):
                p["stop_level"] = round(new, 8)
                last = p.get("last_trail_notice_stop", cur)
                if abs(last - p["stop_level"]) >= 0.25 * a:
                    tg(f"📈 <b>TRAIL</b> | {base} | stop ${cur:.6g} -> ${p['stop_level']:.6g}")
                    p["last_trail_notice_stop"] = p["stop_level"]
                changed = True
        if favour >= sl and not p.get("partial_tp_done") and base not in requests:
            half = partial_qty(p["symbol"], float(bpos["quantity"]))
            if half:
                request_close(base, side, "partial_1R", price, entry, upnl, requests, qty=half)
            else:
                log(f"{base}: too small to split at 1R, no partial request")
                p["partial_tp_done"] = True
            changed = True
        return changed

    errors = []
    for key, p in list(opens.items()):
        try:
            if manage(key, p):
                changed = True
        except Exception as ex:
            errors.append(f"{base_of(p['symbol'])}: {ex}")
            print(f"guardian: {base_of(p['symbol'])} failed this run: {ex}", file=sys.stderr)
    held_bases = {base_of(p["symbol"]) for p in opens.values()}
    for b in [b for b in approvals if b not in held_bases]:
        log(f"dropping stale approval for {b}: no open position")
        approvals.pop(b)
        changed = True

    commit("open-positions.json",   snap["open-positions.json"],   opens)
    commit("pending-orders.json",   snap["pending-orders.json"],   pend)
    commit("close-requests.json",   snap["close-requests.json"],   requests)
    commit("close-approvals.json",  snap["close-approvals.json"],  approvals)
    if changed:
        update_metrics()
    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    try:
        log(z() + (" LIVE" if LIVE else " DRY-RUN (no orders, no writes)"))
        run()
    except Exception as e:
        print(f"guardian error: {e}", file=sys.stderr)
        sys.exit(1)
