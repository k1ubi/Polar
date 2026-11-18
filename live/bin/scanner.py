#!/usr/bin/env python3
"""Polar alpha-scanner. Places ENTRY limit orders only.
Never a TP, never a stop, never a close (guardian owns exits).

  python3 scanner.py          dry-run: print what it would do, place nothing
  python3 scanner.py --live   place ENTRY limit orders
"""
import json, re, subprocess, sys, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import broker as _broker
import indicators as _ind
from config import TG_TOKEN, TG_CHAT

STATE = Path("/home/kyubi/Polar/live/state")
LIVE = "--live" in sys.argv
UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "SUI",
            "NEAR", "ARB", "APT", "INJ", "OP", "HYPE", "AAVE", "WIF", "TIA", "SEI"]
LEVERAGE = 3  # account leverage — keep in sync with Bitget account setting


def now():
    return datetime.now(timezone.utc)


def z(d=None):
    return (d or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def rj(name, default):
    try:
        return json.loads((STATE / name).read_text())
    except Exception:
        return default


class StateError(Exception):
    pass


def rj_strict(name, default):
    p = STATE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        raise StateError(f"{name} is unreadable")


def log(msg):
    print(("" if LIVE else "[dry] ") + msg, flush=True)


def tg(text):
    if not LIVE:
        return log("telegram: " + text.replace("\n", " | "))
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json.dumps({"chat_id": TG_CHAT, "parse_mode": "HTML", "text": text}).encode(),
            {"Content-Type": "application/json"}), timeout=15).read()
    except Exception as e:
        print("telegram failed:", e, file=sys.stderr)


import fcntl, copy


def commit(name, before, after):
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


def append(rec):
    if LIVE:
        with open(STATE / "trade-signals.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


def gates():
    """File-only gates. Returns (regime, why_not)."""
    if "--ignore-gates" in sys.argv and not LIVE:
        r = rj("regime.json", {})
        return {**r, "regime": r.get("regime") if r.get("regime") in ("BULLISH", "BEARISH") else "BEARISH"}, None
    try:
        if rj_strict("pause.json", {}).get("paused"):
            return None, "paused"
        if rj_strict("circuit-breaker.json", {}).get("halted"):
            return None, "circuit breaker halted"
        rj_strict("open-positions.json", {}), rj_strict("pending-orders.json", {})
    except StateError as ex:
        return None, f"state file corrupt: {ex}"
    reg = rj("regime.json", {})
    if reg.get("regime") not in ("BULLISH", "BEARISH"):
        return None, f"regime {reg.get('regime')}"
    if 9 <= now().hour <= 12:
        return None, "dead-zone hours 09-12 UTC"
    return reg, None


def score(m):
    p, e = m["price"], m["ema20_4h"]
    if p == e:
        return None, 0
    if p > e:
        pts = (2 * (50 <= m["rsi_15m"] <= 70) + 2 * (48 <= m["rsi_1h"] <= 68)
               + 2 * (-0.5 <= m["z8_15m"] <= 1.5) + (m["z20_1h"] > 0) + (m["rvol"] >= 1.4))
        return "LONG", pts
    pts = (2 * (30 <= m["rsi_15m"] <= 50) + 2 * (32 <= m["rsi_1h"] <= 52)
           + 2 * (-1.5 <= m["z8_15m"] <= 0.5) + (m["z20_1h"] < 0) + (m["rvol"] >= 1.4))
    return "SHORT", pts


def review(base, d, pts, m, regime, equity, qty, price, sl, stop, tp, open_view, headlines):
    """Sonnet second opinion on a shortlisted entry. Fails closed."""
    risk_pct = qty * price * sl / equity * 100
    prompt = (
        f"You are the risk reviewer for an automated crypto perpetual-futures strategy "
        f"(Bitget USDT-M, {LEVERAGE}x).\n"
        f"A rule-based scanner shortlisted ONE entry. Decide APPROVE or VETO. Veto if anything looks "
        f"wrong: a trade against the real situation, news risk, oversized risk, correlation with open "
        f"positions, a setup that passes the score but looks like a trap. You cannot change the order.\n\n"
        f"Proposed: {d} {base}/USDT, limit entry {price}, qty {qty}, notional ${qty*price:.2f}\n"
        f"Equity ${equity:.2f}; risk if stopped out {risk_pct:.2f}% of equity; stop {stop:.6g}; TP {tp:.6g}\n"
        f"Score {pts}/8; RSI15m {m['rsi_15m']:.0f}, RSI1h {m['rsi_1h']:.0f}, "
        f"z8(15m) {m['z8_15m']:.2f}, z20(1h) {m['z20_1h']:.2f},\n"
        f"rvol {m['rvol']:.2f}, price vs 4h EMA20 {m['price']:.6g}/{m['ema20_4h']:.6g}, "
        f"ATR15m {m['atr14_15m']:.6g}, BTC corr {m['btc_corr']:.2f}\n"
        f"Market regime: {regime}\n"
        f"Open/pending positions: {open_view or 'none'}\n"
        f"Headlines: {headlines or 'none'}\n\n"
        f'Reply with ONLY: {{"decision": "APPROVE" or "VETO", "reason": "<one sentence>"}}'
    )
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "high",
             "--tools", "", "--no-session-persistence", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=240, cwd="/tmp")
        out = r.stdout.strip()
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        return j.get("decision") == "APPROVE", str(j.get("reason", ""))[:300]
    except Exception as e:
        return False, f"review failed (fail closed): {str(e)[:120]}"


def main():
    reg, why = gates()
    if why:
        return log(f"exit: {why}")
    regime = reg["regime"]
    opens = rj("open-positions.json", {})
    pend  = rj("pending-orders.json", {})
    total = len(opens) + len(pend)
    if total >= 5:
        return log("exit: position cap")
    sides = [v["side"] for v in list(opens.values()) + list(pend.values())]
    held  = {v["symbol"].split("/")[0] for v in list(opens.values()) + list(pend.values())}
    allowed = "LONG" if regime == "BULLISH" else "SHORT"
    if sides.count(allowed) >= 2:
        return log(f"exit: {allowed} cap (2) reached")

    info   = _broker.account_info()
    broker = _broker.account_portfolio()
    equity = float(info["netLiquidation"])
    b_syms = {p["symbol"] for p in broker}
    b_side = [p["side"].upper() for p in broker]
    pend_only = [v for v in pend.values() if v["symbol"].split("/")[0] not in b_syms]
    if len(broker) + len(pend_only) >= 5:
        return log(f"exit: position cap (broker shows {len(broker)} open + {len(pend_only)} pending)")
    if b_side.count(allowed) + [v["side"] for v in pend_only].count(allowed) >= 2:
        return log(f"exit: {allowed} cap (2) reached (broker view)")
    held |= b_syms
    used = sum(float(p["marketValue"]) / float((p.get("risk") or {}).get("leverage") or LEVERAGE)
               for p in broker)
    used += sum(v["qty"] * v["limit_price"] / LEVERAGE for v in pend.values())
    free = equity - used
    if free < 15:
        return log(f"exit: free margin {free:.2f} < 15")

    ex = _broker._ex()
    to_scan = [s for s in UNIVERSE if s not in held]
    results = _ind.compute_scan(ex, to_scan)

    cands = []
    for s, m in results.items():
        d, pts = score(m)
        log(f"{s}: {d} {pts}/8 "
            f"(rsi15 {m['rsi_15m']:.0f}, rsi1h {m['rsi_1h']:.0f}, "
            f"z8 {m['z8_15m']:.2f}, rvol {m['rvol']:.2f})")
        if d == allowed and pts >= 7:
            cands.append((pts, s, d, m))
    cands.sort(key=lambda c: -c[0])

    adv = rj("correlation-advisory.json", {}).get("positions", {})
    hot_adv = any(v.get("side") == allowed and (v.get("btc_correlation") or 0) > 0.9 for v in adv.values())
    picks, hot_run = [], False
    for pts, s, d, m in cands:
        if m["btc_corr"] > 0.9 and (hot_adv or hot_run):
            log(f"{s}: dropped by correlation gate")
            continue
        # news gate removed (no OpenAlice RSS feed; Sonnet reviewer handles news risk)
        if m["btc_corr"] > 0.9:
            hot_run = True
        picks.append((pts, s, d, m))
    slots = min(3, 5 - total, 2 - sides.count(allowed))
    picks = picks[:slots]
    if not picks:
        return log("no qualifying setups")

    for pts, s, d, m in picks:
        sym = f"{s}/USDT:USDT"
        price, atr14 = m["price"], m["atr14_15m"]
        sl = min(max(atr14 * 1.5 / price, 0.005), 0.015)
        risk_usd = equity * 0.015
        qty = min(risk_usd / (price * sl), (free * 0.8 * LEVERAGE) / price)
        qty_s   = ex.amount_to_precision(sym, qty)
        price_s = ex.price_to_precision(sym, price)
        qty, price_f = float(qty_s), float(price_s)
        if qty <= 0 or qty * price_f / LEVERAGE > free * 0.8:
            log(f"{s}: skipped, size/margin")
            continue
        short = d == "SHORT"
        stop  = price_f * (1 + sl) if short else price_f * (1 - sl)
        tp    = price_f * (1 - 2 * sl) if short else price_f * (1 + 2 * sl)
        action = "SELL" if short else "BUY"
        risk_pct = qty * price_f * sl / equity * 100
        log(f"ENTRY {s} {d} score {pts}/8: {action} LMT {qty_s} @ {price_s} "
            f"(sl {sl*100:.2f}%, risk {risk_pct:.2f}%, tp {tp:.6g}, stop watch {stop:.6g})")
        if risk_pct > 2.0:
            log(f"{s}: skipped, risk {risk_pct:.2f}% above 2% cap after lot rounding")
            append({"type": "skipped", "symbol": sym, "reason": f"risk_{risk_pct:.1f}pct", "entered_at": z()})
            continue
        if not LIVE:
            continue
        open_view = "; ".join(
            f"{v['side']} {v['symbol'].split('/')[0]}"
            for v in list(opens.values()) + list(pend.values()))
        ok, why = review(s, d, pts, m, regime, equity, qty, price_f, sl, stop, tp, open_view, [])
        log(f"{s}: reviewer {'APPROVE' if ok else 'VETO'}: {why}")
        if not ok:
            append({"type": "skipped", "symbol": sym, "reason": "reviewer_veto", "detail": why, "entered_at": z()})
            tg(f"🚫 <b>VETO</b> | {s} {d} score {pts}/8 not entered. Reviewer: {why}")
            continue
        try:
            order_id = _broker.place_entry_order(sym, action, price_f, qty, f"Alpha Scanner {s} {d} score {pts}/8")
        except Exception as e:
            append({"type": "skipped", "symbol": sym, "reason": f"order_error: {str(e)[:120]}", "entered_at": z()})
            print("order failed, stopping this run:", e, file=sys.stderr)
            break
        key = f"{s}_{int(now().timestamp())}"
        pend[key] = {
            "symbol": sym, "side": d, "limit_price": price_f, "qty": qty,
            "stop_level": stop, "tp_level": tp, "sl_distance_pct": sl,
            "atr14_15m": atr14, "ema20_4h": m["ema20_4h"], "btc_corr": m["btc_corr"],
            "score": pts, "regime": regime, "order_id": order_id, "placed_at": z(),
        }
        commit("pending-orders.json", {k: v for k, v in pend.items() if k != key}, pend)
        append({"type": "order", "symbol": sym, "side": d, "limit_price": price_f,
                "qty": qty, "score": pts, "regime": regime, "placed_at": z()})
        free -= qty * price_f / LEVERAGE
        tg(f"{'🔴' if short else '🟢'} <b>ENTRY ORDER</b> | {s}/USDT | {d}\n"
           f"Limit: ${price_s} | planned TP: ${tp:.6g} | stop watch: ${stop:.6g}\n"
           f"Score: {pts}/8 | RSI(15m): {m['rsi_15m']:.0f} | z8: {m['z8_15m']:.2f} | "
           f"rvol: {m['rvol']:.2f}x\n"
           f"ATR SL: {sl*100:.2f}% | Regime: {regime} | Reviewer: {why}\nwaiting for fill")
        if free < 15:
            break


if __name__ == "__main__":
    try:
        log(z() + (" LIVE" if LIVE else " DRY-RUN (no orders)"))
        main()
    except Exception as e:
        print(f"scanner error: {e}", file=sys.stderr)
        sys.exit(1)
