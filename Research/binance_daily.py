"""Daily Binance spot klines for every USDT pair, including delisted ones (status BREAK).

Cached one JSON file per symbol under Research/data/binance_daily/.
Run: python3 -m Research.binance_daily
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import aiohttp

BASE = "https://data-api.binance.vision/api/v3"
CACHE = Path(__file__).parent / "data" / "binance_daily"
START_MS = 1514764800000  # 2018-01-01

STABLES = {
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDS", "USDSB", "SUSD", "UST", "USTC",
    "EUR", "GBP", "AUD", "TRY", "BRL", "RUB", "UAH", "NGN", "ZAR", "IDRT", "BIDR", "BVND", "AEUR",
    "EURI", "PAXG", "XUSD", "USDE", "USD1", "RLUSD", "BFUSD", "WBTC", "WBETH", "BETH",
}
LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR)$")


def universe(info: dict) -> list[str]:
    out = []
    for s in info["symbols"]:
        base = s["baseAsset"]
        if s["quoteAsset"] != "USDT" or s["status"] not in ("TRADING", "BREAK"):
            continue
        if base in STABLES or LEVERAGED.search(base):
            continue
        out.append(s["symbol"])
    return sorted(out)


async def _klines(session: aiohttp.ClientSession, sem: asyncio.Semaphore, sym: str) -> None:
    path = CACHE / f"{sym}.json"
    if path.exists():
        return
    rows: list[list] = []
    start = START_MS
    async with sem:
        while True:
            params = {"symbol": sym, "interval": "1d", "startTime": str(start), "limit": "1000"}
            for attempt in range(5):
                async with session.get(f"{BASE}/klines", params=params) as r:
                    if r.status == 429 or r.status == 418:
                        await asyncio.sleep(30 * (attempt + 1))
                        continue
                    if r.status != 200:
                        return
                    batch = await r.json()
                    break
            else:
                return
            if not batch:
                break
            rows += [[int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[7])] for b in batch]
            if len(batch) < 1000:
                break
            start = int(batch[-1][0]) + 86_400_000
            await asyncio.sleep(0.15)
    if rows:
        path.write_text(json.dumps(rows))


async def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE}/exchangeInfo") as r:
            info = await r.json()
        syms = universe(info)
        print(f"{len(syms)} USDT pairs (trading + delisted), fetching...")
        sem = asyncio.Semaphore(4)
        await asyncio.gather(*[_klines(session, sem, s) for s in syms])
    print(f"cached: {len(list(CACHE.glob('*.json')))} symbols")


if __name__ == "__main__":
    asyncio.run(main())
