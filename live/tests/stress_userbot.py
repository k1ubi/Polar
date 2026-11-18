#!/usr/bin/env python3
"""Sandbox tests for the customer copy engine: fake exchange, temp files, no network, no real customers."""
import asyncio, importlib.util, json, math, sys, tempfile, types
from datetime import datetime, timezone, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location("ub", "/home/kyubi/Polar/live/bot/polar-user-bot.py")
ub = importlib.util.module_from_spec(spec); spec.loader.exec_module(ub)
OK = True


def check(name, cond, detail=""):
    global OK
    OK &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail else ""))


class FakeEx:
    def __init__(self, free=1000.0, lot=1.0, minq=1.0):
        self.free, self.lot, self.minq = free, lot, minq
        self.orders, self.created, self.cancelled, self.n = {}, [], [], 0
        self.positions = []

    def load_markets(self): pass
    def market(self, sym): return {"limits": {"amount": {"min": self.minq}}}
    def amount_to_precision(self, sym, x): return str(math.floor(x / self.lot) * self.lot)
    def price_to_precision(self, sym, x): return str(round(x, 5))
    def fetch_balance(self, p=None): return {"USDT": {"free": self.free}}

    def create_order(self, sym, typ, side, qty, price=None, params=None):
        self.n += 1
        o = {"id": str(self.n), "symbol": sym, "type": typ, "side": side, "amount": qty, "price": price,
             "params": params or {}, "filled": 0.0, "status": "open"}
        self.orders[o["id"]] = o
        self.created.append(o)
        return o

    def fetch_order(self, oid, sym): return dict(self.orders[oid])
    def cancel_order(self, oid, sym):
        self.cancelled.append(oid)
        if self.orders[oid]["status"] == "open":
            self.orders[oid]["status"] = "canceled"


CUST = {"api_key": "k", "secret": "s", "passphrase": "p"}
SIG = {"type": "entry", "symbol": "ADA/USDT:USDT", "side": "LONG", "entry_price": 0.2283, "tp_level": 0.2316,
       "stop_level": 0.2266, "sl_distance_pct": 0.0072, "score": 7, "entered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def use(ex): ub.make_exchange = lambda *a: ex


# --- sizing
ex = FakeEx(free=100.0, lot=1.0, minq=1.0)
q, why = ub.size_copy(ex, "ADA/USDT:USDT", 0.2283, 0.0072, 100.0)
check("sizing: 100 USDT, tight stop is capped at 1.5x balance and rounded down to a whole lot", q == 657.0, f"qty={q} (notional {q*0.2283 if q else 0:.1f})")
q, why = ub.size_copy(FakeEx(free=20.0, lot=0.001, minq=0.001), "BTC/USDT:USDT", 70000, 0.01, 20.0)
check("sizing: 20 USDT on BTC would need a minimum lot that over-risks -> skipped", q is None, why or "")
q, why = ub.size_copy(FakeEx(free=1000.0, lot=0.001, minq=0.001), "BTC/USDT:USDT", 70000, 0.01, 1000.0)
check("sizing: normal account on BTC gets a valid lot", q == 0.021, f"qty={q}")

# --- entry placement: no TP before fill
ex = FakeEx(free=1000.0); use(ex)
text, rec = ub.execute_copy(CUST, SIG)
check("entry: places ONE order (the entry), no reduce-only TP yet", len(ex.created) == 1 and not ex.created[0]["params"].get("reduceOnly"), text)
check("entry: copy is tracked as pending", rec and rec["status"] == "pending")

# --- tending
c = dict(rec)
new, msg = ub.tend_copy(CUST, c)
check("tend: unfilled entry just waits", new and new["status"] == "pending" and msg is None and len(ex.created) == 1)
ex.orders[c["entry_order_id"]]["filled"] = 40.0
new, msg = ub.tend_copy(CUST, dict(c))
tp = ex.created[-1]
check("tend: partial fill -> rest of entry cancelled, TP sized to the fill", c["entry_order_id"] in ex.cancelled and tp["params"].get("reduceOnly") and tp["amount"] == 40.0, f"tp={tp['amount']}")
ex2 = FakeEx(); use(ex2)
_, r2 = ub.execute_copy(CUST, SIG)
ex2.orders[r2["entry_order_id"]].update(status="closed", filled=r2["qty"])
new, msg = ub.tend_copy(CUST, dict(r2))
check("tend: full fill -> TP for the whole quantity, status open", new["status"] == "open" and ex2.created[-1]["amount"] == r2["qty"])
tp_id = new["tp_order_id"]; ex2.orders[tp_id]["status"] = "closed"
done, msg = ub.tend_copy(CUST, new)
check("tend: TP hit -> copy finished", done is None and "take-profit hit" in msg)
ex3 = FakeEx(); use(ex3)
_, r3 = ub.execute_copy(CUST, SIG)
r3["placed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat()
done, msg = ub.tend_copy(CUST, r3)
check("tend: unfilled after 45 min -> cancelled and dropped", done is None and r3["entry_order_id"] in ex3.cancelled, msg or "")

# --- closes touch only the copy's own quantity
ex4 = FakeEx(); use(ex4)
_, r4 = ub.execute_copy(CUST, SIG)
ex4.orders[r4["entry_order_id"]].update(status="closed", filled=r4["qty"])
opened, _ = ub.tend_copy(CUST, dict(r4))
ex4.positions = [{"contracts": 5000}]  # the customer also holds 5000 ADA of their own
closed, msg = ub.mirror_close(CUST, opened, {"type": "close", "outcome": "MANUAL_APPROVED", "symbol": "ADA/USDT:USDT", "side": "LONG"})
mk = [o for o in ex4.created if o["type"] == "market"]
check("close: market order is exactly the copy's quantity, not the customer's whole position",
      closed is None and len(mk) == 1 and mk[0]["amount"] == r4["qty"] and mk[0]["params"].get("reduceOnly"), f"{[o['amount'] for o in mk]}")
check("close: the TP order was cancelled first", opened["tp_order_id"] is None or any(i for i in ex4.cancelled))
ex5 = FakeEx(); use(ex5)
_, r5 = ub.execute_copy(CUST, SIG)
closed, msg = ub.mirror_close(CUST, dict(r5), {"type": "close", "outcome": "MANUAL_APPROVED", "symbol": "ADA/USDT:USDT", "side": "LONG"})
check("close: still-pending entry is just cancelled, nothing sold", closed is None and r5["entry_order_id"] in ex5.cancelled and not [o for o in ex5.created if o["type"] == "market"])
ex6 = FakeEx(); use(ex6)
_, r6 = ub.execute_copy(CUST, SIG)
ex6.orders[r6["entry_order_id"]].update(status="closed", filled=r6["qty"])
o6, _ = ub.tend_copy(CUST, dict(r6))
left, msg = ub.mirror_close(CUST, o6, {"type": "partial_close", "symbol": "ADA/USDT:USDT", "side": "LONG", "fraction": 0.5, "tp_level": 0.24})
mk = [o for o in ex6.created if o["type"] == "market"]
check("partial: half closed at market, remaining gets a new TP", left and mk[0]["amount"] == math.floor(r6["qty"] * 0.5) and ex6.created[-1]["price"] == 0.24 and left["filled"] == r6["qty"] - mk[0]["amount"], f"{mk[0]['amount']} / left {left['filled']}")

# --- engine: pause, idempotency, no replay
d = Path(tempfile.mkdtemp(prefix="polar-ub-"))
ub.STATE_DIR = d; ub.SIGNALS_FILE = d / "trade-signals.jsonl"; ub.LAST_IDX_FILE = d / "li.txt"; ub.CLOSE_IDX_FILE = d / "lc.txt"
ub.CUSTOMERS_FILE = d / "customers.json"; ub.COPIES_FILE = d / "copies.json"
ub.CUSTOMERS_FILE.write_text(json.dumps({"111": CUST}))
sent = []
app = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=lambda **k: asyncio.sleep(0, sent.append(k["text"]))))
old_close = {"type": "close", "outcome": "MANUAL_APPROVED", "symbol": "ADA/USDT:USDT", "side": "LONG"}
ub.SIGNALS_FILE.write_text(json.dumps(old_close) + "\n")
ex7 = FakeEx(); use(ex7)
asyncio.run(ub.process_closes(app))
check("close index starts at the current end: history is never replayed", ub.read_idx(ub.CLOSE_IDX_FILE) == 1 and not ex7.created)
ub.write_idx(ub.LAST_IDX_FILE, 0)
ub.SIGNALS_FILE.write_text(json.dumps(SIG) + "\n")
(d / "pause.json").write_text(json.dumps({"paused": True}))
asyncio.run(ub.process_entries(app))
check("entry while paused: nothing placed, customer told", not ex7.created and any("not copied" in s.lower() for s in sent), str(sent[-1:]))
(d / "pause.json").write_text(json.dumps({"paused": False}))
ub.write_idx(ub.LAST_IDX_FILE, 0)
asyncio.run(ub.process_entries(app))
n = len(ex7.created)
ub.write_idx(ub.LAST_IDX_FILE, 0)
asyncio.run(ub.process_entries(app))
check("entry when allowed: copied once; replaying the same signal does not duplicate", n == 1 and len(ex7.created) == 1, f"orders={len(ex7.created)}")
(d / "circuit-breaker.json").write_text("{corrupt")
check("corrupt state file blocks copying", ub.blocked_reason() is not None)
sys.exit(0 if OK else 1)
