"""Incremental daily universe cache update for VPA.

Run via: python3 -m Core.vpa.daily_scan [--workers N]
Triggered nightly at 02:00 UTC by polar-vpa-daily-scan.service.

Fetches fresh OHLCV data for every symbol in the universe and rebuilds
the universe_cache.json so the daemon starts each day with up-to-date bars.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

ROOT = Path(__file__).parent.parent.parent
UNIVERSE_CACHE = Path(__file__).parent / "data" / "universe_cache.json"

# Bitget OHLCV endpoint (no auth required for public market data)
BITGET_BASE = "https://api.bitget.com"
MAX_CONSECUTIVE_FAILURES = 5


def _load_universe() -> list[str]:
    try:
        data = json.loads(UNIVERSE_CACHE.read_text())
        return data.get("symbols", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def _fetch_symbol(
    session: aiohttp.ClientSession,
    symbol: str,
    failures: dict[str, int],
) -> tuple[str, list | None]:
    """Fetch recent OHLCV candles for one symbol. Returns (symbol, bars|None)."""
    if failures.get(symbol, 0) >= MAX_CONSECUTIVE_FAILURES:
        logger.debug("skipping parked symbol %s", symbol)
        return symbol, None

    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "granularity": "1Dutc",
        "limit": "365",
    }
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                body = await resp.json()
                failures[symbol] = 0
                return symbol, body.get("data", [])
            else:
                failures[symbol] = failures.get(symbol, 0) + 1
                logger.warning("fetch %s → HTTP %s", symbol, resp.status)
                return symbol, None
    except Exception as exc:
        failures[symbol] = failures.get(symbol, 0) + 1
        logger.warning("fetch %s failed: %s", symbol, exc)
        return symbol, None


async def run(workers: int = 4) -> None:
    symbols = _load_universe()
    if not symbols:
        logger.error("universe cache empty or missing: %s", UNIVERSE_CACHE)
        return

    logger.info("starting daily scan: %d symbols, %d workers", len(symbols), workers)

    failures: dict[str, int] = {}
    results: dict[str, list] = {}
    sem = asyncio.Semaphore(workers)

    async def bounded_fetch(session: aiohttp.ClientSession, sym: str) -> None:
        async with sem:
            sym, bars = await _fetch_symbol(session, sym, failures)
            if bars is not None:
                results[sym] = bars

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(bounded_fetch(session, s) for s in symbols))

    logger.info(
        "scan complete: %d/%d symbols fetched, %d parked",
        len(results),
        len(symbols),
        sum(1 for v in failures.values() if v >= MAX_CONSECUTIVE_FAILURES),
    )

    # Persist updated cache
    cache_path = Path(__file__).parent / "data" / "ohlcv_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                "symbols": list(results.keys()),
                "bars": results,
            },
            separators=(",", ":"),
        )
    )
    logger.info("wrote %s", cache_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="VPA incremental daily cache update")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(run(workers=args.workers))


if __name__ == "__main__":
    main()
