#!/usr/bin/env python3
"""One-off: replace estimated exits on CLOSED_UNKNOWN trades with the real Bitget fill.
  python3 backfill_exits.py          show what would change
  python3 backfill_exits.py --apply  rewrite trade-signals.jsonl (keeps a .bak) and refresh metrics
Only `close` records with outcome CLOSED_UNKNOWN are touched; line count and order never change."""
import json, subprocess, sys, os
from datetime import datetime
from pathlib import Path

STATE = Path("/home/kyubi/Polar/live/state")
SRC = "bitget-bf693fe4"
F = STATE / "trade-signals.jsonl"
APPLY = "--apply" in sys.argv


def ts(x):
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


rows = json.loads(subprocess.run(["openalice", "exec", "alice-uta", "order", "trades", "--source", SRC, "--limit", "200"],
                                 capture_output=True, text=True, timeout=120).stdout)
raw = F.read_text()
lines = raw.splitlines()
recs = [json.loads(l) if l.strip() else None for l in lines]
changed = 0
for i, r in enumerate(recs):
    if not r or r.get("type") != "close" or r.get("outcome") != "CLOSED_UNKNOWN":
        continue
    aid = f"{SRC}|{r['symbol']}"
    want = "BUY" if r["side"] == "SHORT" else "SELL"
    fills = [t for t in rows if t["contract"]["aliceId"] == aid and t["side"] == want and t.get("source") != "reconcile"
             and ts(r["entered_at"]) < ts(t["timestamp"]) <= ts(r["closed_at"])]
    if not fills:
        print(f"line {i+1} {r['symbol']}: no closing fill found, left as is")
        continue
    oid = max(fills, key=lambda t: t["timestamp"])["orderId"]
    same = [t for t in fills if t["orderId"] == oid]
    q = sum(float(t["quantity"]) for t in same)
    px = sum(float(t["price"]) * float(t["quantity"]) for t in same) / q
    e = r["entry_price"]
    pnl = ((e - px) if r["side"] == "SHORT" else (px - e)) / e * 100
    tp = next((x.get("tp_level") for x in recs if x and x.get("type") == "entry" and x["symbol"] == r["symbol"]
               and x.get("entered_at") and abs((ts(x["entered_at"]) - ts(r["entered_at"])).total_seconds()) < 1200
               and x.get("tp_level")), None)
    # nothing closes without Lorenzo's tap, so a profitable close that wasn't approved is the resting TP
    outcome = "TP_HIT" if (tp and abs(px - tp) / tp <= 0.003) or pnl > 0 else "CLOSED_EXTERNAL"
    print(f"line {i+1} {r['symbol']} {r['side']}: exit {r['exit_price']} -> {px:.6g}, pnl {r['pnl_pct']} -> {pnl:.3f}, "
          f"{r['outcome']} -> {outcome} (fill {same[0]['timestamp']})")
    r.update({"exit_price": round(px, 8), "pnl_pct": round(pnl, 3), "outcome": outcome, "backfilled": True})
    lines[i] = json.dumps(r)
    changed += 1
if APPLY and changed:
    if F.read_text() != raw:
        sys.exit("trade-signals.jsonl changed while working, run again")
    F.with_suffix(".jsonl.bak").write_text(raw)
    tmp = F.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, F)
    sys.argv = ["guardian.py", "--live"]
    sys.path.insert(0, "/home/kyubi/Polar/live/bin")
    import guardian
    guardian.update_metrics()
    print(f"applied {changed} record(s); backup at {F.with_suffix('.jsonl.bak')}")
elif not APPLY:
    print(f"\n{changed} record(s) would change (dry run)")
