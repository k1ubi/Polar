#!/usr/bin/env python3
"""Polar regime agent (no model). Same rules as regime-agent.md: BTC 4h/1d trend vs EMA20,
futures basis, options put/call ratio and 25-delta skew -> score -5..+5 -> regime.json.
Reads market data only. Use --dry to print without writing or alerting."""
import json, subprocess, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from config import TG_TOKEN, TG_CHAT

STATE = Path("/home/kyubi/Polar/live/state")
SRC = "bitget-bf693fe4"
DRY = "--dry" in sys.argv


def cli(*args):
    for _ in (1, 2):
        r = subprocess.run(["openalice", "exec", *args], capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            return json.loads(r.stdout)
    raise RuntimeError(f"{' '.join(args[:3])}: {r.stderr.strip()[:200]}")


def sign(x, up, down):
    return 1 if up(x) else (-1 if down(x) else 0)


def parse_exp(s):
    return datetime.strptime(s, "%d%b%y").replace(tzinfo=timezone.utc)


def main():
    q = cli("alice", "analysis", "quant", "--script", f'''
b4h = bars("{SRC}|BTC/USDT", "4h", count=30)
b1d = bars("{SRC}|BTC/USDT", "1d", count=30)
{{"price": b4h.close[-1], "ema4h": ema(b4h.close, 20), "ema1d": ema(b1d.close, 20)}}
''')["value"]
    price, e4, e1 = q["price"], q["ema4h"], q["ema1d"]
    s4 = 1 if price > e4 else -1
    s1 = 1 if price > e1 else -1

    # futures basis: first annualized basis available on the BTC curve
    basis = None
    try:
        board = cli("traderhub", "board", "get", "--board", "term-structure")
        btc = next(c for c in board["curves"] if c["symbol"] == "BTC")
        basis = next((p["annualizedBasis"] for p in btc["points"] if p["annualizedBasis"] is not None), None)
    except Exception as e:
        print("term-structure failed:", e, file=sys.stderr)
    sb = 0 if basis is None else sign(basis, lambda x: x > 5, lambda x: x < -5)

    # options: put/call OI over the next 3 expiries, 25-delta skew on the nearest full-day expiry
    pc = skew = None
    sp = ss = 0
    try:
        chain = cli("traderhub", "crypto", "options", "--symbol", "BTC")
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        exps = sorted({o["expiration"] for o in chain if parse_exp(o["expiration"]) > today}, key=parse_exp)
        near = set(exps[:3])
        put_oi = sum(o["open_interest"] or 0 for o in chain if o["expiration"] in near and o["option_type"] == "put")
        call_oi = sum(o["open_interest"] or 0 for o in chain if o["expiration"] in near and o["option_type"] == "call")
        if call_oi:
            pc = put_oi / call_oi
            sp = sign(pc, lambda x: x < 0.7, lambda x: x > 1.3)
        e0 = [o for o in chain if o["expiration"] == exps[0] and o.get("delta") is not None and o.get("implied_volatility")]
        p25 = min((o for o in e0 if o["option_type"] == "put"), key=lambda o: abs(o["delta"] + 0.25), default=None)
        c25 = min((o for o in e0 if o["option_type"] == "call"), key=lambda o: abs(o["delta"] - 0.25), default=None)
        if p25 and c25:
            skew = (p25["implied_volatility"] - c25["implied_volatility"]) * 100
            ss = sign(skew, lambda x: x < -3, lambda x: x > 3)
    except Exception as e:
        print("options failed (signals scored 0):", e, file=sys.stderr)

    score = s4 + s1 + sb + sp + ss
    regime = "BULLISH" if score >= 3 else "BEARISH" if score <= -3 else "NEUTRAL"
    out = {
        "regime": regime, "score": score, "confidence": round(abs(score) / 5 * 10, 1),
        "basis": {
            "btc_4h": f"{'bullish' if s4 > 0 else 'bearish'} (price ${price:.2f} vs EMA20 ${e4:.2f})",
            "btc_1d": f"{'bullish' if s1 > 0 else 'bearish'} (price ${price:.2f} vs EMA20 ${e1:.2f})",
            "futures_basis_pct": None if basis is None else round(basis, 2),
            "put_call_ratio": None if pc is None else round(pc, 4),
            "options_skew_pct": None if skew is None else round(skew, 2),
        },
        "signals": {"ema_4h": s4, "ema_1d": s1, "futures_basis": sb, "put_call_ratio": sp, "skew_25d": ss},
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        prev = json.loads((STATE / "regime.json").read_text()).get("regime")
    except Exception:
        prev = None
    print(json.dumps(out, indent=2))
    if DRY:
        print(f"[dry] previous regime {prev}")
        return
    tmp = STATE / "regime.json.tmp"
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(STATE / "regime.json")
    if prev and prev != regime:
        b = out["basis"]
        text = (f"🔄 <b>REGIME CHANGE</b>\n{prev} → {regime} (score: {score}/5, confidence: {out['confidence']}/10)\n"
                f"Basis: 4h {'bullish' if s4 > 0 else 'bearish'} · 1d {'bullish' if s1 > 0 else 'bearish'} · "
                f"basis {b['futures_basis_pct']}% · P/C {b['put_call_ratio']}")
        body = json.dumps({"chat_id": TG_CHAT, "parse_mode": "HTML", "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", body, {"Content-Type": "application/json"}), timeout=15).read()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"regime error: {e}", file=sys.stderr)
        sys.exit(1)
