"""VPA position store — thread-safe atomic JSON persistence."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Core.Library.risk import consecutive_losses as _shared_consecutive_losses
from Core.Library.risk import loss_streak_cooldown_until as _shared_cooldown_until
from Core.Library.risk import MAX_CONSECUTIVE_LOSSES

POSITIONS_PATH = Path(__file__).parent / "data" / "positions.json"


# ── atomic write helper ──────────────────────────────────────────────────────

def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON atomically: temp file → fsync → os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── public API ───────────────────────────────────────────────────────────────

def load(path: Path = POSITIONS_PATH) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(positions: list[dict], path: Path = POSITIONS_PATH) -> None:
    _atomic_write(path, positions)


def get_open(path: Path = POSITIONS_PATH) -> list[dict]:
    return [p for p in load(path) if p.get("status") == "open"]


def get_closed(path: Path = POSITIONS_PATH) -> list[dict]:
    return [p for p in load(path) if p.get("status") == "closed"]


def upsert(position: dict, path: Path = POSITIONS_PATH) -> None:
    """Insert or replace a position by id."""
    positions = load(path)
    idx = next((i for i, p in enumerate(positions) if p["id"] == position["id"]), None)
    if idx is None:
        positions.append(position)
    else:
        positions[idx] = position
    save(positions, path)


# ── risk helpers (delegates to shared module) ────────────────────────────────

def consecutive_losses(path: Path = POSITIONS_PATH) -> int:
    return _shared_consecutive_losses(path)


def loss_streak_cooldown_until(path: Path = POSITIONS_PATH) -> datetime | None:
    return _shared_cooldown_until(path)


def is_in_cooldown(path: Path = POSITIONS_PATH) -> bool:
    until = loss_streak_cooldown_until(path)
    if until is None:
        return False
    return datetime.now(tz=timezone.utc) < until


def realized_pnl_usdt(path: Path = POSITIONS_PATH) -> float:
    return sum(
        p.get("pnl_usdt") or 0.0
        for p in get_closed(path)
    )
