"""Customer registry — Bitget credentials + Telegram chat IDs.

Stored in Core/customer_bot/data/customers.json with 0600 permissions.
Never committed (add to .gitignore if needed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
CUSTOMERS_PATH = DATA_DIR / "customers.json"


def _load() -> dict:
    try:
        return json.loads(CUSTOMERS_PATH.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOMERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CUSTOMERS_PATH)


def get(chat_id: int) -> dict | None:
    return _load().get(str(chat_id))


def upsert(customer: dict) -> None:
    data = _load()
    data[str(customer["chat_id"])] = customer
    _save(data)


def remove(chat_id: int) -> None:
    data = _load()
    data.pop(str(chat_id), None)
    _save(data)


def all_customers() -> list[dict]:
    return list(_load().values())


def enabled_customers() -> list[dict]:
    return [c for c in _load().values() if c.get("enabled")]
