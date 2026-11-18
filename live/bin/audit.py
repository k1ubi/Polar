#!/usr/bin/env python3
"""Polar audit (read-only). Compares state files with real Bitget positions and orders.
Checks are plain code; if something is off, Sonnet (high effort) explains it → Telegram.
Places, cancels and closes NOTHING."""
import json, subprocess, sys, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import broker as _broker
from config import TG_TOKEN, TG_CHAT

STATE = Path("/home/kyubi/Polar/live/state")
DRY = "--dry" in sys.argv


def rj(n, d):
    try:
        return json.loads((STATE / n).read_text())
    except Exception:
        return d


def main():
    opens = rj("open-positions.json", {})
    pend  = rj("pending-orders.json", {})
    pos    = _broker.account_portfolio()
    orders = _broker.order_list()
    info   = _broker.account_info()
    equity = float(info["netLiquidation"])

    issues = []
    SRC = _broker.SRC

    tracked = {(f"{SRC}|{p['symbol']}", p["side"].lower()) for p in opens.values()}
    for p in pos:
        k = (p["aliceId"], p["side"].lower())
        if k not in tracked:
            issues.append(f"Broker position {p['symbol']} {p['side']} is not in open-positions.json")
        notional = float(p["marketValue"])
        if notional > equity * 2.5:
            issues.append(
                f"{p['symbol']} notional ${notional:.0f} is {notional/equity:.1f}x equity ${equity:.0f}")

    broker_keys = {(p["aliceId"], p["side"].lower()) for p in pos}
    for k, p in opens.items():
        if (f"{SRC}|{p['symbol']}", p["side"].lower()) not in broker_keys:
            issues.append(f"{p['symbol']} is in open-positions.json but not on the broker")

    for p in pos:
        want = "BUY" if p["side"].lower() == "short" else "SELL"
        if not [o for o in orders if o["aliceId"] == p["aliceId"] and o["action"] == want]:
            issues.append(f"{p['symbol']} {p['side']} has no resting take-profit order")

    for o in orders:
        if not [p for p in pos if p["aliceId"] == o["aliceId"]] and \
                not [e for e in pend.values() if str(e.get("order_id")) == str(o["orderId"])]:
            issues.append(
                f"Order {o['orderId']} on {o['symbol']} {o['action']} {o['totalQuantity']}@{o['lmtPrice']} "
                f"has no position and is not a tracked entry "
                f"(a reduce-side order with no position would OPEN one)")

    print("issues:", issues or "none")
    if not issues or DRY:
        return

    prompt = (
        "You audit an automated crypto futures account. These mismatches were found between the bot's "
        "state files and Bitget. In 4 short lines for the owner: what is wrong, how risky it is, what "
        "to do. Never advise closing a position unless you flag it as the owner's decision.\n\n"
        + "\n".join(issues)
    )
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "high",
             "--tools", "", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, timeout=240, cwd="/tmp")
        explain = r.stdout.strip()[:900]
    except Exception:
        explain = ""

    text = "🔎 <b>AUDIT</b>\n" + "\n".join("• " + i for i in issues) + (f"\n\n{explain}" if explain else "")
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json.dumps({"chat_id": TG_CHAT, "parse_mode": "HTML", "text": text}).encode(),
        {"Content-Type": "application/json"}), timeout=15).read()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("audit error:", e, file=sys.stderr)
        sys.exit(1)
