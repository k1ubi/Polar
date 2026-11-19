#!/usr/bin/env python3
"""Polar regime agent. Uses ccxt for BTC market data (no OpenAlice dependency).

Scores BTC market structure on 5 axes → regime.json.
  python3 regime.py        dry-run: print without writing or alerting
  python3 regime.py --live write regime.json and alert on change
"""
import json, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import broker as _broker
import indicators as _ind
from config import TG_TOKEN, TG_CHAT

STATE = Path("/home/kyubi/Polar/live/state")
DRY   = "--live" not in sys.argv


def now():
    return datetime.now(timezone.utc)


def z():
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(x, up_thresh, down_thresh):
    return 1 if x > up_thresh else (-1 if x < down_thresh else 0)


def tg(text):
    if DRY:
        return
    body = json.dumps({"chat_id": TG_CHAT, "parse_mode": "HTML", "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            body, {"Content-Type": "application/json"}), timeout=15).read()
    except Exception as e:
        print("telegram failed:", e, file=sys.stderr)


def write_heartbeat():
    if DRY:
        return
    import os
    hb = {"ts": z(), "pid": os.getpid(), "script": "regime"}
    tmp = STATE / "heartbeat-regime.json.tmp"
    tmp.write_text(json.dumps(hb))
    tmp.replace(STATE / "heartbeat-regime.json")


def main():
    write_heartbeat()
    ex = _broker._ex()

    # ── Signal 1 & 2: BTC 4h and 1d vs EMA-20 ──────────────────────────────
    b4h = _ind.fetch_bars(ex, "BTC/USDT:USDT", "4h", 30)
    b1d = _ind.fetch_bars(ex, "BTC/USDT:USDT", "1d", 30)
    price  = float(b4h["close"][-1])
    e4h    = _ind.ema(b4h["close"], 20)
    e1d    = _ind.ema(b1d["close"], 20)
    s4     = 1 if price > e4h else -1
    s1     = 1 if price > e1d else -1

    # ── Signal 3: BTC funding rate as sentiment proxy ───────────────────────
    # Persistent positive funding (longs pay shorts) → crowded → contango → bullish
    # Persistent negative funding → shorts paying → backwardation → bearish
    sb = 0
    fr_val = None
    try:
        fr_data = ex.fetch_funding_rate("BTC/USDT:USDT")
        fr_val  = float(fr_data.get("fundingRate", 0))
        # > 0.03%/8h (crowded longs) = bullish; < -0.01%/8h (crowded shorts) = bearish
        sb = sign(fr_val, 0.0003, -0.0001)
    except Exception as e:
        print(f"funding rate failed (scored 0): {e}", file=sys.stderr)

    # ── Signal 4: BTC realized vol trend ────────────────────────────────────
    # Rising vol in an uptrend is healthy; rising vol in a downtrend = fear
    sv = 0
    vol_7d = vol_30d = None
    try:
        log_ret = _ind.np.diff(_ind.np.log(b1d["close"].astype(float)))
        vol_7d  = float(log_ret[-7:].std())  * (365 ** 0.5)
        vol_30d = float(log_ret[-30:].std()) * (365 ** 0.5) if len(log_ret) >= 30 else vol_7d
        # Recent vol contracting relative to 30d = calmer market = bullish
        sv = 1 if vol_7d < vol_30d * 0.9 else (-1 if vol_7d > vol_30d * 1.3 else 0)
    except Exception as e:
        print(f"vol trend failed (scored 0): {e}", file=sys.stderr)

    # ── Signal 5: BTC 4h RSI momentum ──────────────────────────────────────
    sr = 0
    rsi_val = None
    try:
        rsi_val = _ind.rsi(b4h["close"], 14)
        sr = sign(rsi_val, 55, 45)  # above 55 = bullish momentum, below 45 = bearish
    except Exception as e:
        print(f"RSI signal failed (scored 0): {e}", file=sys.stderr)

    score  = s4 + s1 + sb + sv + sr
    regime = "BULLISH" if score >= 3 else "BEARISH" if score <= -3 else "NEUTRAL"
    conf   = round(abs(score) / 5 * 10, 1)

    out = {
        "regime":     regime,
        "score":      score,
        "confidence": conf,
        "factors": {
            "btc_above_4h_ema20": s4 > 0,
            "btc_above_1d_ema20": s1 > 0,
            "funding_bullish":    sb > 0,
            "vol_contracting":    sv > 0,
            "rsi_bullish":        sr > 0,
        },
        "signals": {
            "ema_4h": s4, "ema_1d": s1, "funding": sb, "vol_trend": sv, "rsi_momentum": sr,
        },
        "data": {
            "price":        round(price, 2),
            "ema_4h":       round(e4h, 2),
            "ema_1d":       round(e1d, 2),
            "funding_rate": None if fr_val is None else round(fr_val * 100, 4),
            "rsi_4h":       None if rsi_val is None else round(rsi_val, 1),
            "vol_7d_ann":   None if vol_7d is None else round(vol_7d * 100, 1),
            "vol_30d_ann":  None if vol_30d is None else round(vol_30d * 100, 1),
        },
        "updated_at": z(),
    }

    try:
        prev = json.loads((STATE / "regime.json").read_text()).get("regime")
    except Exception:
        prev = None

    print(json.dumps(out, indent=2))
    if DRY:
        print(f"[dry] previous regime: {prev}")
        return

    tmp = STATE / "regime.json.tmp"
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(STATE / "regime.json")

    if prev and prev != regime:
        d = out["data"]
        text = (
            f"🔄 <b>REGIME CHANGE</b>  {prev} → {regime}\n"
            f"Score {score}/5  confidence {conf}/10\n"
            f"BTC ${d['price']:,.2f} | 4h EMA ${d['ema_4h']:,.2f} | 1d EMA ${d['ema_1d']:,.2f}\n"
            f"RSI(4h) {d['rsi_4h']} | funding {d['funding_rate']}%/8h | "
            f"vol 7d {d['vol_7d_ann']}% vs 30d {d['vol_30d_ann']}%"
        )
        tg(text)


if __name__ == "__main__":
    try:
        print(z(), "REGIME", "DRY-RUN" if DRY else "LIVE")
        main()
    except Exception as e:
        print(f"regime error: {e}", file=sys.stderr)
        sys.exit(1)
