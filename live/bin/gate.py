#!/usr/bin/env python3
"""Polar gate: switches OpenAlice issues on/off by flipping `status` (todo = scheduled,
done = skipped) so no model session is launched when the agent would exit immediately.
File-only checks, no model, no orders. Never touches anything except the status line."""
import json, re
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("/home/kyubi/Polar/live/state")
ISSUES = Path("/home/kyubi/.openalice/workspaces/workspaces/chat-tidy-silver-cove/.alice/issues")


def rj(name, default):
    try:
        return json.loads((STATE / name).read_text())
    except Exception:
        return default


def set_status(issue, want):
    p = ISSUES / f"{issue}.md"
    txt = p.read_text()
    m = re.match(r"(---\n)(.*?)(\n---\n)", txt, re.S)
    if not m:
        return f"{issue}: no frontmatter, skipped"
    head = m.group(2)
    cur = re.search(r"^status:\s*(\S+)", head, re.M)
    if not cur:
        return f"{issue}: no status line, skipped"
    if cur.group(1) == want:
        return f"{issue}: {want} (unchanged)"
    head2 = re.sub(r"^status:\s*\S+", f"status: {want}", head, count=1, flags=re.M)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(m.group(1) + head2 + m.group(3) + txt[m.end():])
    tmp.replace(p)
    return f"{issue}: {cur.group(1)} -> {want}"


def scanner_on():
    if rj("pause.json", {}).get("paused"):
        return False, "paused"
    if rj("circuit-breaker.json", {}).get("halted"):
        return False, "circuit breaker"
    regime = rj("regime.json", {}).get("regime")
    if regime not in ("BULLISH", "BEARISH"):
        return False, f"regime {regime}"
    if 9 <= datetime.now(timezone.utc).hour <= 12:
        return False, "dead-zone hours"
    if len(rj("open-positions.json", {})) + len(rj("pending-orders.json", {})) >= 5:
        return False, "position cap"
    return True, "entries allowed"


def guardian_on():
    work = (rj("open-positions.json", {}) or rj("pending-orders.json", {})
            or rj("close-approvals.json", {}))
    return (True, "positions/orders to manage") if work else (False, "nothing to manage")


if __name__ == "__main__":
    out = []
    for issue, fn in ():  # scanner and guardian are Python timers now
        on, why = fn()
        out.append(set_status(issue, "todo" if on else "done") + f" ({why})")
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), " | ".join(out), flush=True)
