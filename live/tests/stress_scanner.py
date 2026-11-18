import importlib.util, json, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

def load(state, broker_positions):
    sys.argv = ["scanner.py"]
    spec = importlib.util.spec_from_file_location("scanner_t", "/home/kyubi/Polar/live/bin/scanner.py")
    s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
    s.STATE = Path(state); s.LOGS = []
    s.log = lambda m: s.LOGS.append(m)
    s.now = lambda: datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)
    def cli(*a):
        if a[:3] == ("alice-uta", "account", "info"): return {"netLiquidation": "100"}
        if a[:3] == ("alice-uta", "account", "portfolio"): return broker_positions
        raise RuntimeError("unexpected cli " + str(a))
    s.cli = cli
    s.scan = lambda syms: {}
    return s

def bp(sym, side):
    return {"symbol": sym, "side": side, "marketValue": "50", "risk": {"leverage": "10"}}

def fresh(regime="BULLISH"):
    d = tempfile.mkdtemp(prefix="polar-scan-")
    for n, v in (("regime.json", {"regime": regime}), ("open-positions.json", {}), ("pending-orders.json", {}),
                 ("pause.json", {"paused": False}), ("circuit-breaker.json", {"halted": False})):
        (Path(d) / n).write_text(json.dumps(v))
    return d

ok = True
def check(name, cond, detail):
    global ok; ok &= cond; print(("PASS " if cond else "FAIL ") + name + "  -- " + detail)

d = fresh(); (Path(d) / "open-positions.json").write_text("{ half written")
s = load(d, []); s.main(); check("S1 corrupt open-positions.json -> scanner refuses to trade", any("corrupt" in l for l in s.LOGS), str(s.LOGS))
d = fresh(); (Path(d) / "pause.json").write_text("garbage")
s = load(d, []); s.main(); check("S1 corrupt pause.json -> treated as halted", any("corrupt" in l for l in s.LOGS), str(s.LOGS))
d = fresh(); (Path(d) / "circuit-breaker.json").write_text("")
s = load(d, []); s.main(); check("S1 corrupt circuit-breaker.json -> treated as halted", any("corrupt" in l for l in s.LOGS), str(s.LOGS))
d = fresh(); s = load(d, [bp("APT", "long"), bp("ADA", "long")]); s.main()
check("S2 state empty but broker holds 2 longs -> LONG cap holds", any("cap (2)" in l for l in s.LOGS), str(s.LOGS))
d = fresh(); s = load(d, [bp(x, "long") for x in "ABCDE"][:5]); s.main()
check("S2 broker holds 5 positions -> position cap holds", any("position cap" in l for l in s.LOGS), str(s.LOGS))
d = fresh(); s = load(d, []); s.main()
check("S2 control: clean state and broker -> scans", any("no qualifying" in l for l in s.LOGS), str(s.LOGS))
sys.exit(0 if ok else 1)
