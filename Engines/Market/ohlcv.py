"""OHLCV fetcher for Bitget USDT-FUTURES market data (no auth required)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

BITGET_BASE = "https://api.bitget.com"
MAX_CONSECUTIVE_FAILURES: dict[str, int] = {}


async def fetch_ohlcv(
    session: aiohttp.ClientSession,
    symbol: str,
    granularity: str = "1Dutc",
    limit: int = 365,
    product_type: str = "USDT-FUTURES",
) -> list[list]:
    """Fetch OHLCV candles from Bitget.

    Returns a list of [ts_ms, open, high, low, close, base_vol, quote_vol].
    Raises ValueError on HTTP error so callers can handle per-symbol failures.

    Note: granularity must use Bitget's exact spelling. Daily = "1Dutc" (NOT "1D").
    """
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": product_type,
        "granularity": granularity,
        "limit": str(limit),
    }
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} for {symbol}")
        body = await resp.json()
        return body.get("data", [])


async def fetch_ohlcv_safe(
    session: aiohttp.ClientSession,
    symbol: str,
    granularity: str = "1Dutc",
    limit: int = 365,
) -> list[list] | None:
    """Fetch with per-symbol failure tracking. Returns None on error."""
    failures = MAX_CONSECUTIVE_FAILURES
    if failures.get(symbol, 0) >= 5:
        return None
    try:
        bars = await fetch_ohlcv(session, symbol, granularity, limit)
        failures[symbol] = 0
        return bars
    except Exception as exc:
        failures[symbol] = failures.get(symbol, 0) + 1
        logger.warning("fetch_ohlcv %s failed (%d): %s", symbol, failures[symbol], exc)
        return None
