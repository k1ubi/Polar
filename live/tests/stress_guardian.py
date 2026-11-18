#!/usr/bin/env python3
"""Sandbox stress test for Polar guardian/scanner. Fake broker + throwaway state dir.
Touches NOTHING real: no openalice calls, no Telegram, no real state files."""
import importlib.util, json, sys, tempfile, types, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

BIN = Path("/home/kyubi/Polar/live/bin")
SRC = "bitget-bf693fe4"


def z(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


class Broker:
    def __init__(self):
        self.positions, self.orders, self.calls, self.closes, self.cancels, self.places = [], [], [], [], [], []
        self.trades = []
        self.empty_portfolio_once = False
        self.quant_fail = False
        self.fail_place_action = None
        self.next_id = 100
        self.staged = []

    def pos(self, sym, side, qty, avg, mark, liq="0.0001"):
        self.positions.append({"source": SRC, "symbol": sym, "aliceId": f"{SRC}|{sym}/USDT:USDT", "side": side,
                               "quantity": str(qty), "avgCost": str(avg), "marketPrice": str(mark),
                               "marketValue": str(qty * mark), "risk": {"leverage": "10", "liquidationPrice": liq}})

    def order(self, sym, action, qty, price, oid=None):
        self.orders.append({"aliceId": f"{SRC}|{sym}/USDT:USDT", "symbol": sym, "action": action,
                            "totalQuantity": str(qty), "lmtPrice": str(price), "orderId": str(oid or self.next_id)})
        self.next_id += 1

    def cli(self, *a, write=False):
        self.calls.append(a)
        g = a[:3]
        if g == ("alice-uta", "account", "portfolio"):
            if self.empty_portfolio_once:
                self.empty_portfolio_once = False
                return []
            return json.loads(json.dumps(self.positions))
        if g == ("alice-uta", "order", "list"):
            return json.loads(json.dumps(self.orders))
        if g == ("alice-uta", "order", "trades"):
            return self.trades
        if g == ("alice-uta", "git", "status"):
            return {"staged": self.staged, "awaitingApproval": None}
        if g == ("alice-uta", "account", "info"):
            return {"netLiquidation": "100", "totalCashValue": "100"}
        if a[0] == "alice" and a[1] == "analysis":
            if self.quant_fail:
                raise RuntimeError("quant: simulated Bitget API timeout")
            bases = re.findall(r'"(\w+)": atr', a[-1])
            return {"value": {b: 0.01 for b in bases}}
        if g == ("alice-uta", "position", "close"):
            aid = a[a.index("--alice-id") + 1]
            q = a[a.index("--qty") + 1] if "--qty" in a else None
            self.closes.append((aid, q))
            for p in list(self.positions):
                if p["aliceId"] == aid:
                    if q:
                        p["quantity"] = str(round(float(p["quantity"]) - float(q), 8))
                    else:
                        self.positions.remove(p)
            return {"ok": True}
        if g == ("alice-uta", "order", "cancel"):
            oid = a[a.index("--order-id") + 1]
            self.cancels.append(oid)
            self.orders = [o for o in self.orders if o["orderId"] != oid]
            return {"ok": True}
        if g == ("alice-uta", "order", "place"):
            action = a[a.index("--action") + 1]
            if self.fail_place_action and action == self.fail_place_action:
                self.fail_place_action = None
                raise RuntimeError("order place: simulated rejection")
            aid = a[a.index("--alice-id") + 1]
            self.places.append((aid, action, a[a.index("--total-quantity") + 1], a[a.index("--lmt-price") + 1]))
            self.order(aid.split("|")[1].split("/")[0], action, a[a.index("--total-quantity") + 1],
                       a[a.index("--lmt-price") + 1])
            return {"ok": True}
        return {}


def load_guardian(state, broker):
    sys.argv = ["guardian.py", "--live"]
    spec = importlib.util.spec_from_file_location("guardian_t", BIN / "guardian.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    g.STATE = Path(state)
    g.cli = broker.cli
    g.tg = lambda text, buttons=None: g.SENT.append(text)
    g.SENT = []
    g.subprocess = types.SimpleNamespace(run=lambda *a, **k: None)
    g.time = types.SimpleNamespace(sleep=lambda s: None)

    class Ex:  # stand-in for ccxt: whole lots for ADA/NEAR, 0.001 steps elsewhere
        def amount_to_precision(self, sym, x):
            import math
            return str(math.floor(x)) if sym.startswith(("ADA", "NEAR")) else str(math.floor(x * 1000) / 1000)
    g.market_limits = lambda sym: (Ex(), 1.0 if sym.startswith(("ADA", "NEAR")) else 0.001)
    g.log = lambda m: g.LOGS.append(m)
    g.LOGS = []
    return g


def wjson(state, name, data):
    (Path(state) / name).write_text(json.dumps(data, indent=2))


def rjson(state, name, default=None):
    try:
        return json.loads((Path(state) / name).read_text())
    except Exception:
        return default


def mk_open(sym="APT", side="LONG", entry=1.0, qty=100.0, sl=0.01, **kw):
    s = side == "SHORT"
    d = {"symbol": f"{sym}/USDT:USDT", "side": side, "entry_price": entry, "qty": qty,
         "stop_level": entry * (1 + sl) if s else entry * (1 - sl),
         "tp_level": entry * (1 - 2 * sl) if s else entry * (1 + 2 * sl), "sl_distance_pct": sl,
         "score": 7, "regime": "BULLISH", "entered_at": z(NOW - timedelta(hours=2)), "tp_order_id": "1",
         "partial_tp_done": False}
    d.update(kw)
    return d


def fresh(regime="BULLISH"):
    st = tempfile.mkdtemp(prefix="polar-stress-")
    wjson(st, "regime.json", {"regime": regime})
    for n in ("open-positions.json", "pending-orders.json", "close-requests.json", "close-approvals.json"):
        wjson(st, n, {})
    return st


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  -- {detail}" if detail else ""))


# ---------------- attacks ----------------
def a1_empty_portfolio_glitch():
    """API returns [] once while a position is open: must NOT forget the position or cancel its TP."""
    st, b = fresh(), Broker()
    b.pos("APT", "long", 100, 1.0, 1.005)
    b.order("APT", "SELL", 100, 1.02, oid="tp1")
    wjson(st, "open-positions.json", {"APT_1": mk_open()})
    b.empty_portfolio_once = True
    g = load_guardian(st, b)
    g.run()
    kept = "APT_1" in rjson(st, "open-positions.json", {})
    check("A1 transient empty portfolio keeps the position tracked", kept and not b.cancels,
          f"tracked={kept} cancels={b.cancels}")
    check("A1 no close ever", not b.closes)


def a2_partial_fill():
    """Entry 100 partly filled (40): TP must cover only what is filled and the unfilled remainder must not
    keep growing the position unprotected."""
    st, b = fresh(), Broker()
    b.pos("SOL", "long", 40, 100.0, 100.0)
    b.order("SOL", "BUY", 100, 100.0, oid="entry1")
    wjson(st, "pending-orders.json", {"SOL_1": {"symbol": "SOL/USDT:USDT", "side": "LONG", "limit_price": 100.0,
                                                "qty": 100, "stop_level": 99, "tp_level": 102,
                                                "sl_distance_pct": 0.01, "score": 7, "regime": "BULLISH",
                                                "order_id": "entry1", "placed_at": z(NOW - timedelta(minutes=5))}})
    g = load_guardian(st, b)
    g.run()
    tp_qty = [p[2] for p in b.places if p[1] == "SELL"]
    resting_entry = [o for o in b.orders if o["action"] == "BUY"]
    check("A2 partial fill: unfilled entry remainder is cancelled", not resting_entry,
          f"resting entry orders={[o['orderId'] for o in resting_entry]} tp_qty={tp_qty}")
    check("A2 partial fill: TP sized to the filled quantity", tp_qty == ["40.0"] or tp_qty == ["40"], f"{tp_qty}")


def a3_double_execution():
    """Approved partial close, follow-up TP placement fails: the retry must not close another half."""
    st, b = fresh(), Broker()
    b.pos("APT", "long", 100, 1.0, 1.012)
    b.order("APT", "SELL", 100, 1.02, oid="tp1")
    wjson(st, "open-positions.json", {"APT_1": mk_open(qty=100.0)})
    req_t = z(NOW - timedelta(minutes=10))
    wjson(st, "close-requests.json", {"APT": {"reason": "partial_1R", "side": "LONG", "price": 1.01,
                                              "requested_at": req_t, "last_alert_at": req_t, "qty": 50.0}})
    wjson(st, "close-approvals.json", {"APT": {"approved_at": z(NOW - timedelta(minutes=5)), "by": "lorenzo", "qty": 50.0}})
    b.fail_place_action = "SELL"
    g = load_guardian(st, b)
    try:
        g.run()
    except Exception:
        pass
    g2 = load_guardian(st, b)
    try:
        g2.run()
    except Exception:
        pass
    check("A3 retry after a failed follow-up does not close a second half", len(b.closes) == 1,
          f"closes={b.closes}")


def a4_approvals_validity():
    for label, appr, req, expect_close in [
        ("approval older than the request", {"approved_at": z(NOW - timedelta(hours=3)), "by": "lorenzo", "qty": None},
         {"reason": "stop_breach", "requested_at": z(NOW - timedelta(minutes=10))}, False),
        ("approval without any request", {"approved_at": z(NOW - timedelta(minutes=1)), "by": "lorenzo", "qty": None},
         None, False),
        ("approval qty differs from request qty", {"approved_at": z(NOW - timedelta(minutes=1)), "by": "lorenzo", "qty": None},
         {"reason": "partial_1R", "requested_at": z(NOW - timedelta(minutes=10)), "qty": 50.0}, False),
        ("valid approval (control)", {"approved_at": z(NOW - timedelta(minutes=1)), "by": "lorenzo", "qty": None},
         {"reason": "stop_breach", "requested_at": z(NOW - timedelta(minutes=10))}, True),
    ]:
        st, b = fresh(), Broker()
        b.pos("APT", "long", 100, 1.0, 0.98)
        b.order("APT", "SELL", 100, 1.02, oid="tp1")
        wjson(st, "open-positions.json", {"APT_1": mk_open()})
        if req:
            req.update({"side": "LONG", "price": 0.98, "last_alert_at": req["requested_at"]})
            wjson(st, "close-requests.json", {"APT": req})
        wjson(st, "close-approvals.json", {"APT": appr})
        g = load_guardian(st, b)
        try:
            g.run()
        except Exception as e:
            pass
        check(f"A4 {label}: close {'executes' if expect_close else 'is refused'}", bool(b.closes) == expect_close,
              f"closes={b.closes}")


def a5_quant_outage_with_approval():
    """ATR lookup fails: an approved close and the stop alerts must still be processed."""
    st, b = fresh(), Broker()
    b.pos("APT", "long", 100, 1.0, 0.98)
    b.pos("ADA", "long", 100, 1.0, 0.97)
    b.order("APT", "SELL", 100, 1.02, oid="tp1")
    b.order("ADA", "SELL", 100, 1.02, oid="tp2")
    wjson(st, "open-positions.json", {"APT_1": mk_open("APT"), "ADA_1": mk_open("ADA")})
    t = z(NOW - timedelta(minutes=10))
    wjson(st, "close-requests.json", {"APT": {"reason": "stop_breach", "side": "LONG", "price": 0.98,
                                              "requested_at": t, "last_alert_at": t}})
    wjson(st, "close-approvals.json", {"APT": {"approved_at": z(NOW - timedelta(minutes=1)), "by": "lorenzo", "qty": None}})
    b.quant_fail = True
    g = load_guardian(st, b)
    try:
        g.run()
    except Exception as e:
        pass
    reqs = rjson(st, "close-requests.json", {})
    check("A5 quant outage: approved close still executes", len(b.closes) == 1, f"closes={b.closes}")
    check("A5 quant outage: ADA stop breach still raises a request", "ADA" in reqs or any("ADA" in s for s in g.SENT),
          f"requests={list(reqs)}")


def a6_tiny_partial():
    """qty=1 at 1R: the 'partial' request must never degrade into a full-close request."""
    st, b = fresh(), Broker()
    b.pos("BTC", "long", 1, 100.0, 101.5)
    b.order("BTC", "SELL", 1, 102.0, oid="tp1")
    wjson(st, "open-positions.json", {"BTC_1": mk_open("BTC", entry=100.0, qty=1.0, sl=0.01)})
    g = load_guardian(st, b)
    g.run()
    r = rjson(st, "close-requests.json", {}).get("BTC")
    bad = bool(r) and r.get("reason") == "partial_1R" and not r.get("qty")
    check("A6 partial request on a 1-lot position is not a disguised full close", not bad, f"request={r}")


def a6b_lot_sizes():
    st, b = fresh(), Broker()
    b.pos("ADA", "long", 823, 1.0, 1.012)
    b.order("ADA", "SELL", 823, 1.02, oid="tp1")
    wjson(st, "open-positions.json", {"ADA_1": mk_open("ADA", qty=823.0)})
    g = load_guardian(st, b)
    g.run()
    r = rjson(st, "close-requests.json", {}).get("ADA", {})
    check("A6b ADA (whole lots): partial request is a valid whole lot", r.get("qty") == 411.0, f"qty={r.get('qty')}")
    st, b = fresh(), Broker()
    b.pos("NEAR", "long", 1, 5.0, 5.1)
    b.order("NEAR", "SELL", 1, 5.2, oid="tp1")
    wjson(st, "open-positions.json", {"NEAR_1": mk_open("NEAR", entry=5.0, qty=1.0)})
    g = load_guardian(st, b)
    g.run()
    r = rjson(st, "close-requests.json", {}).get("NEAR")
    check("A6b 1-lot position: no partial request at all", r is None, f"request={r}")


def a9_liquidation_watch():
    st, b = fresh(), Broker()
    b.pos("APT", "long", 100, 1.0, 1.0, liq="0.8")
    b.order("APT", "SELL", 100, 1.02, oid="tp1")
    wjson(st, "open-positions.json", {"APT_1": mk_open("APT")})
    g = load_guardian(st, b)
    g.run()
    check("A9 liquidation within 30% raises an information alert, closes nothing",
          any("LIQUIDATION WATCH" in t for t in g.SENT) and not b.closes, f"sent={len(g.SENT)}")


def a10_staged_queue():
    st, b = fresh(), Broker()
    b.pos("APT", "long", 100, 1.0, 0.98)
    b.order("APT", "SELL", 100, 1.02, oid="tp1")
    b.staged = [{"x": 1}]
    wjson(st, "open-positions.json", {"APT_1": mk_open()})
    t = z(NOW - timedelta(minutes=10))
    wjson(st, "close-requests.json", {"APT": {"reason": "stop_breach", "side": "LONG", "price": 0.98,
                                              "requested_at": t, "last_alert_at": t}})
    wjson(st, "close-approvals.json", {"APT": {"approved_at": z(NOW - timedelta(minutes=1)), "by": "lorenzo", "qty": None}})
    g = load_guardian(st, b)
    g.run()
    check("A10 staged operations already queued: no second close stacked, approval kept",
          not b.closes and "APT" in rjson(st, "close-approvals.json", {}), f"closes={b.closes}")


def a7_exit_price_from_fill():
    st, b = fresh(), Broker()
    wjson(st, "open-positions.json", {"ARB_1": mk_open("ARB", "SHORT", entry=0.17668, qty=850, sl=0.0077,
                                                       entered_at=z(NOW - timedelta(days=1)))})
    wjson(st, "last-prices.json", {"ARB": 0.17459})
    b.trades = [{"orderId": "9", "timestamp": z(NOW - timedelta(minutes=3)), "side": "BUY", "quantity": "850",
                 "price": "0.17307", "contract": {"aliceId": f"{SRC}|ARB/USDT:USDT"}, "source": "order"}]
    g = load_guardian(st, b)
    g.run()
    sigs = [json.loads(l) for l in (Path(st) / "trade-signals.jsonl").read_text().splitlines()] \
        if (Path(st) / "trade-signals.jsonl").exists() else []
    c = [s for s in sigs if s["type"] == "close"]
    check("A7 closed trade is booked at the real fill price", bool(c) and abs(c[0]["exit_price"] - 0.17307) < 1e-9,
          f"{c[:1]}")


def a8_never_close_unasked():
    """Control sweep: stop breach, regime shift, 1R and gone-trade all run with no approval -> zero closes."""
    st, b = fresh("BEARISH"), Broker()
    b.pos("APT", "long", 100, 1.0, 0.90)
    b.pos("SOL", "long", 100, 1.0, 1.02)
    b.order("APT", "SELL", 100, 1.02, oid="t1")
    b.order("SOL", "SELL", 100, 1.02, oid="t2")
    wjson(st, "open-positions.json", {"APT_1": mk_open("APT"), "SOL_1": mk_open("SOL")})
    g = load_guardian(st, b)
    g.run()
    check("A8 stop/regime/1R with no approval: zero closes, zero cancels", not b.closes and not b.cancels,
          f"closes={b.closes} cancels={b.cancels}")


if __name__ == "__main__":
    for fn in (a1_empty_portfolio_glitch, a2_partial_fill, a3_double_execution, a4_approvals_validity,
               a5_quant_outage_with_approval, a6_tiny_partial, a6b_lot_sizes, a7_exit_price_from_fill, a8_never_close_unasked,
               a9_liquidation_watch, a10_staged_queue):
        try:
            fn()
        except Exception as e:
            check(fn.__name__ + " (harness crashed)", False, repr(e)[:160])
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    sys.exit(1 if bad else 0)
