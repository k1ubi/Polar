"""Hourly snapshot of every Core's live state files.

Run via: python3 -m Core.Library.backup
Triggered by polar-backup.service / polar-backup.timer (hourly).
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent  # /home/kyubi/Polar

# All Cores whose data/ directory should be snapshotted.
CORES: list[Path] = [
    ROOT / "Core" / "vpa" / "data",
    ROOT / "Core" / "vpascalper" / "data",
    ROOT / "Core" / "weekend_hunter" / "data",
    ROOT / "Core" / "trend_paper" / "data",
    ROOT / "Core" / "funding_carry" / "data",
    ROOT / "Core" / "admin_bot" / "data",
    ROOT / "Core" / "Library" / "data",
]

BACKUP_ROOT = ROOT / "backups"


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run() -> None:
    ts = _timestamp()
    dest = BACKUP_ROOT / ts
    dest.mkdir(parents=True, exist_ok=True)

    for src in CORES:
        if not src.exists():
            continue
        rel = src.relative_to(ROOT)
        target = dest / rel
        shutil.copytree(src, target, dirs_exist_ok=True)
        print(f"backed up {rel} → backups/{ts}/{rel}")

    # Keep only the 48 most recent snapshots (48h of hourly backups).
    snapshots = sorted(BACKUP_ROOT.iterdir())
    for old in snapshots[:-48]:
        shutil.rmtree(old, ignore_errors=True)
        print(f"pruned old snapshot: {old.name}")

    print(f"backup complete: {ts}")


if __name__ == "__main__":
    run()
