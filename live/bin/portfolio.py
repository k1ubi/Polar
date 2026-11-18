#!/usr/bin/env python3
"""Polar portfolio manager — equity history, 24h drawdown circuit breaker,
net delta and BTC-correlation advisory. Reads the broker; never closes anything.
Use --dry to print without writing or alerting."""
import json, sys, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import broker as _broker
import indicators as _ind
from config import TG_TOKEN, TG_CHAT

STATE = Path("/home/kyubi/Polar/live/state")
DRY = "--dry" in sys.argv


def rj(name, default):
    try:
        return json.loads((STATE / name).read_text())
    except Exception:
        return default


def wj(name, data):
    if DRY:
        print(f"  [dry] would write {name}: {json.dumps(data)[:200]}")
        return
    tmp = STATE / (name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE / name)


def tg(text):
    if DRY:
        print(f"  [dry] telegram: {text}")
        return
    body = json.dumps({"chat_id": TG_CHAT, "parse_mode": "HTML", "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", body,
        {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("telegram failed:", e, file=sys.stderr)


def now():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat()


def main():
    info = _broker.account_info()
    positions = _broker.account_portfolio()
    net_liq = float(info["netLiquidation"])
    upnl = float(info.get("unrealizedPnL", 0))

    # history (7 days) and rolling-24h peak
    hist_path = STATE / "portfolio-history.jsonl"
    rows = []
    try:
        for line in hist_path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    rows.append({"net_liq": net_liq, "unrealized_pnl": upnl, "recorded_at": iso(now())})
    rows = [r for r in rows if now() - datetime.fromisoformat(r["recorded_at"]) <= timedelta(days=7)]
    if not DRY:
        hist_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    last24 = [r["net_liq"] for r in rows
              if now() - datetime.fromisoformat(r["recorded_at"]) <= timedelta(hours=24)]
    peak = max(last24)
    dd = (peak - net_liq) / peak * 100
    print(f"equity {net_liq} upnl {upnl} peak24h {peak} drawdown {dd:.2f}%")

    # circuit breaker
    cb = rj("circuit-breaker.json", None)
    if cb is None:
        cb = {"halted": False}
        wj("circuit-breaker.json", cb)
    if dd > 10 and not cb.get("halted"):
        wj("circuit-breaker.json", {
            "halted": True, "reason": "drawdown_10pct", "drawdown_pct": round(dd, 2),
            "peak_24h": peak, "net_liq": net_liq, "halted_at": iso(now()),
        })
        tg(f"⛔ CIRCUIT BREAKER | drawdown {dd:.2f}% from 24h peak ${peak} -> ${net_liq} | new entries halted")
    elif cb.get("halted") and dd <= 10:
        halted_at = datetime.fromisoformat(cb["halted_at"]) if cb.get("halted_at") else None
        if halted_at and now() - halted_at >= timedelta(hours=12):
            wj("circuit-breaker.json", {"halted": False, "reset_at": iso(now())})
            tg("✅ Circuit breaker reset, entries re-enabled")

    if not positions:
        return

    # delta
    longs = sum(1 for p in positions if p["side"].lower() == "long")
    shorts = sum(1 for p in positions if p["side"].lower() == "short")
    delta = longs - shorts
    mc = rj("metrics-cache.json", {})
    mc.update({"portfolio_delta": delta, "long_count": longs, "short_count": shorts,
               "net_liq": net_liq, "checked_at": iso(now())})
    wj("metrics-cache.json", mc)
    if abs(delta) >= 3:
        tg(f"⚠️ HIGH DELTA | {longs} LONG / {shorts} SHORT | consider a hedge")

    # BTC correlation advisory
    bases = [p["symbol"] for p in positions]
    ex = _broker._ex()
    corr = _ind.compute_correlations(ex, bases)
    adv = {
        "positions": {
            p["symbol"]: {"btc_correlation": corr.get(p["symbol"]), "side": p["side"].upper()}
            for p in positions
        },
        "updated_at": iso(now()),
    }
    wj("correlation-advisory.json", adv)
    for side in ("LONG", "SHORT"):
        hot = [b for b, v in adv["positions"].items()
               if v["side"] == side and (v["btc_correlation"] or 0) > 0.9]
        if len(hot) >= 2:
            tg(f"⚠️ CORRELATED | {hot[0]} and {hot[1]} both {side}, BTC corr > 0.9 (effectively one BTC bet)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"portfolio error: {e}", file=sys.stderr)
        sys.exit(1)
