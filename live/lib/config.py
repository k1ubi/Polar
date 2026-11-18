"""Shared configuration loader. Reads live/.env on first import.
All credentials come from environment variables; this file contains no secrets.
"""
import os
from pathlib import Path


def _load_env():
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

# Telegram — required at runtime; empty string safe for dry-runs (tg() checks LIVE flag)
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT_ID", "")
