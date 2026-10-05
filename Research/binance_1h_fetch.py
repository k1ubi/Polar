"""Binance 1H USDT klines for research — same pattern as binance_daily.py.

Fetches 1H bars from START_MS to now for symbols already in the daily cache
that have >= MIN_DAILY_BARS history. Skips symbols already cached.

Caches to Research/data/binance_1h/<SYMBOL>.json, same format as daily:
  [open_time_ms, open, high, low, close, quote_volume]

Run: python3 -m Research.binance_1h_fetch
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

BASE = "https://data-api.binance.vision/api/v3"
DAILY_CACHE = Path(__file__).parent / "data" / "binance_daily"
HOURLY_CACHE = Path(__file__).parent / "data" / "binance_1h"
START_MS = 1640995200000   # 2022-01-01 00:00 UTC
STEP_MS = 3_600_000        # 1 hour in ms
MIN_DAILY_BARS = 730       # ~2 years of daily data — symbol must be established


def _universe() -> list[str]:
    """Symbols from the daily cache with enough history to be worth testing."""
    syms = []
    for f in DAILY_CACHE.glob("*.json"):
        rows = json.loads(f.read_text())
        if len(rows) >= MIN_DAILY_BARS:
            syms.append(f.stem)
    return sorted(syms)


async def _fetch_symbol(session: aiohttp.ClientSession, sem: asyncio.Semaphore, sym: str) -> None:
    out_path = HOURLY_CACHE / f"{sym}.json"
    if out_path.exists():
        return

    rows: list[list] = []
    start = START_MS
    async with sem:
        while True:
            params = {
                "symbol": sym,
                "interval": "1h",
                "startTime": str(start),
                "limit": "1000",
            }
            for attempt in range(5):
                async with session.get(f"{BASE}/klines", params=params) as r:
                    if r.status in (429, 418):
                        await asyncio.sleep(30 * (attempt + 1))
                        continue
                    if r.status != 200:
                        return   # delisted / unavailable
                    batch = await r.json()
                    break
            else:
                return

            if not batch:
                break
            rows += [
                [int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[7])]
                for b in batch
            ]
            if len(batch) < 1000:
                break
            start = int(batch[-1][0]) + STEP_MS
            await asyncio.sleep(0.12)

    if rows:
        out_path.write_text(json.dumps(rows, separators=(",", ":")))


async def main() -> None:
    HOURLY_CACHE.mkdir(parents=True, exist_ok=True)
    syms = _universe()
    already = len(list(HOURLY_CACHE.glob("*.json")))
    print(f"{len(syms)} symbols to fetch ({already} already cached), starting...")
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(4)
        await asyncio.gather(*[_fetch_symbol(session, sem, s) for s in syms])
    total = len(list(HOURLY_CACHE.glob("*.json")))
    print(f"done — {total} symbols cached to {HOURLY_CACHE}")


if __name__ == "__main__":
    asyncio.run(main())
