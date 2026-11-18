#!/usr/bin/env python3
"""Sandbox test for the Decisions menu: fake Telegram, temp state dir, nothing real."""
import asyncio, importlib.util, json, sys, tempfile, types
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, "/home/kyubi/Polar/live/tests")
spec = importlib.util.spec_from_file_location("pb", "/home/kyubi/Polar/live/bot/polar-bot.py")
pb = importlib.util.module_from_spec(spec); spec.loader.exec_module(pb)
OK = True


def check(name, cond, detail=""):
    global OK
    OK &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {detail}" if detail else ""))


d = Path(tempfile.mkdtemp(prefix="polar-dec-"))
pb.STATE_DIR = d


async def fake_price(sym): return 0.70
pb.live_price = fake_price
iso = lambda m: (datetime.now(timezone.utc) - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")


def reset():
    (d / "open-positions.json").write_text(json.dumps({
        "APT_1": {"symbol": "APT/USDT:USDT", "side": "LONG", "entry_price": 0.837, "qty": 193.276, "stop_level": 0.83, "tp_level": 0.85},
        "ADA_1": {"symbol": "ADA/USDT:USDT", "side": "LONG", "entry_price": 0.2283, "qty": 823.0, "stop_level": 0.2266, "tp_level": 0.2316},
        "SOL_1": {"symbol": "SOL/USDT:USDT", "side": "SHORT", "entry_price": 100, "qty": 1, "stop_level": 101, "tp_level": 98}}))
    (d / "close-requests.json").write_text(json.dumps({
        "APT": {"reason": "stop_breach", "side": "LONG", "price": 0.7, "requested_at": iso(600), "last_alert_at": iso(10)},
        "ADA": {"reason": "regime_shift", "side": "LONG", "price": 0.2, "requested_at": iso(300), "last_alert_at": iso(10), "qty": 411.0}}))
    (d / "close-approvals.json").write_text("{}")
    (d / "pause.json").write_text(json.dumps({"paused": True, "reason": "unanswered close requests: ADA, APT"}))


class Msg:
    def __init__(self, text=""): self.text, self.text_html, self.out = text, text, []
    async def reply_text(self, text, **k): self.out.append((text, k.get("reply_markup")))


class Q:
    def __init__(self, data, text):
        self.data, self.message, self.edits = data, Msg(text), []
    async def edit_message_text(self, text, **k):
        self.edits.append((text, k.get("reply_markup"))); self.message.text = self.message.text_html = text


def labels(kb): return [b.text for row in kb.inline_keyboard for b in row] if kb else []
def cbs(kb): return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


reset()
m = Msg(); asyncio.run(pb._send_decisions(m))
check("list: header + one card per position asked about (SOL has no request -> not listed)", len(m.out) == 3 and "APT" in m.out[1][0] and "ADA" in m.out[2][0], f"{len(m.out)} messages")
check("list: APT card has exactly the three answers", labels(m.out[1][1]) == ["🔴 Close", "🟢 Leave", "🔕 Leave and don't ask me again"], str(labels(m.out[1][1])))
check("list: partial request says PARTIAL and the size", "PARTIAL: close 411.0 of 823.0" in m.out[2][0])
check("list: the main keyboard has the Decisions button", "Decisions ⚖️" in str(pb.CHAT_BAR_KEYBOARD.keyboard) and pb.CHAT_BAR_ACTIONS["Decisions ⚖️"] == "decisions")

# Close: confirm first, nothing approved until the second tap
q = Q("dc:APT", m.out[1][0]); asyncio.run(pb._decision_button(q, q.data))
check("close: first tap only asks to confirm, no approval written", json.loads((d / "close-approvals.json").read_text()) == {} and "Confirm FULL close" in q.edits[0][0])
q2 = Q("dback:APT", q.message.text); asyncio.run(pb._decision_button(q2, q2.data))
check("close: Back restores the three buttons, still no approval", len(labels(q2.edits[0][1])) == 3 and json.loads((d / "close-approvals.json").read_text()) == {})
q3 = Q("dcy:APT", q.message.text); asyncio.run(pb._decision_button(q3, q3.data))
a = json.loads((d / "close-approvals.json").read_text())
check("close: second tap writes Lorenzo's approval for APT only", list(a) == ["APT"] and a["APT"]["by"] == "lorenzo" and a["APT"]["qty"] is None, str(a))

# Leave
q = Q("dl:ADA", m.out[2][0]); asyncio.run(pb._decision_button(q, q.data))
r = json.loads((d / "close-requests.json").read_text())
check("leave: ADA marked rejected, not muted, request kept", r["ADA"]["rejected"] and not r["ADA"].get("muted"))
check("leave: pause stays (APT still unanswered)", json.loads((d / "pause.json").read_text())["paused"] is True)

# Mute: confirm first
q = Q("dm:ADA", m.out[2][0]); asyncio.run(pb._decision_button(q, q.data))
r = json.loads((d / "close-requests.json").read_text())
check("mute: first tap only asks to confirm", not r["ADA"].get("muted") and "Confirm: stop asking" in q.edits[0][0])
q = Q("dmy:ADA", q.message.text); asyncio.run(pb._decision_button(q, q.data))
r = json.loads((d / "close-requests.json").read_text())
check("mute: confirmed -> muted", r["ADA"]["muted"] and r["ADA"]["rejected"])
m = Msg(); asyncio.run(pb._send_decisions(m))
ada = [t for t in m.out if "ADA" in t[0] and "Muted" in t[0]]
check("muted position stays listed with only Close", ada and labels(ada[0][1]) == ["🔴 Close"], str([labels(t[1]) for t in m.out[1:]]))

# answer the last one -> pause clears
q = Q("dl:APT", "x"); asyncio.run(pb._decision_button(q, q.data))
check("all answered -> the pause caused by silence clears", json.loads((d / "pause.json").read_text())["paused"] is False)
# stale button
(d / "close-requests.json").write_text("{}")
q = Q("dl:APT", "x"); asyncio.run(pb._decision_button(q, q.data))
check("stale button after the request is gone changes nothing", "nothing to decide" in q.edits[0][0])
reset(); (d / "close-requests.json").write_text("{}")
m = Msg(); asyncio.run(pb._send_decisions(m))
check("nothing pending -> friendly message", "No decisions waiting" in m.out[0][0], m.out[0][0])

# guardian honours the mute
import harness as H
st, b = H.fresh(), H.Broker()
b.pos("APT", "long", 100, 1.0, 0.90)
b.order("APT", "SELL", 100, 1.02, oid="t1")
H.wjson(st, "open-positions.json", {"APT_1": H.mk_open("APT")})
t = H.z(H.NOW - timedelta(hours=9))
H.wjson(st, "close-requests.json", {"APT": {"reason": "stop_breach", "side": "LONG", "price": 0.9, "requested_at": t,
                                            "last_alert_at": t, "rejected": True, "rejected_at": t, "muted": True}})
g = H.load_guardian(st, b); g.run()
check("guardian: muted position is never asked about again (no message, no change, no close)",
      not any("CLOSE REQUEST" in s for s in g.SENT) and not b.closes, f"sent={g.SENT}")
st, b = H.fresh(), H.Broker()
b.pos("APT", "long", 100, 1.0, 0.90)
b.order("APT", "SELL", 100, 1.02, oid="t1")
H.wjson(st, "open-positions.json", {"APT_1": H.mk_open("APT")})
sent = []
g = H.load_guardian(st, b); g.tg = lambda text, buttons=None: sent.append(buttons); g.run()
flat = [x["text"] for row in sent[0] for x in row]
check("guardian: new alerts carry the same three buttons", flat == ["🔴 Close", "🟢 Leave", "🔕 Leave and don't ask me again"], str(flat))
sys.exit(0 if OK else 1)
