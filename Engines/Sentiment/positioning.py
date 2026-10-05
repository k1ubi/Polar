"""Positioning collector — open interest, funding rate, book depth.

Polls Bitget REST every ~1 minute for every symbol in the universe
and appends to daily JSONL files under Engines/Sentiment/data/positioning/YYYY-MM-DD.jsonl.

Run via: python3 -m Engines.Sentiment.positioning
"""
from __future__ import annotations

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
UNIVERSE_CACHE = ROOT / "Core" / "vpa" / "data" / "universe_cache.json"
DATA_DIR = Path(__file__).parent / "data" / "positioning"

BITGET_BASE = "https://api.bitget.com"
POLL_INTERVAL_SECONDS = 60
MAX_SYMBOLS_PER_REQUEST = 50  # Bitget batch limit


def _load_universe() -> list[str]:
    try:
        return json.loads(UNIVERSE_CACHE.read_text()).get("symbols", [])
    except Exception:
        return []


def _today_file() -> Path:
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{day}.jsonl"


def _append_records(records: list[dict]) -> None:
    path = _today_file()
    with path.open("a") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


async def _fetch_ticker_batch(
    session: aiohttp.ClientSession, symbols: list[str]
) -> list[dict]:
    """Fetch ticker data (OI, mark, funding, depth) for a batch of symbols."""
    url = f"{BITGET_BASE}/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("tickers HTTP %s", resp.status)
                return []
            body = await resp.json()
            raw = body.get("data", [])
            sym_set = set(symbols)
            ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            records = []
            for t in raw:
                sym = t.get("symbol", "")
                if sym not in sym_set:
                    continue
                records.append({
                    "ts": ts,
                    "symbol": sym,
                    "oi": float(t.get("holdingAmount", 0) or 0),
                    "mark": float(t.get("markPrice", 0) or 0),
                    "last": float(t.get("lastPr", 0) or 0),
                    "funding": float(t.get("fundingRate", 0) or 0),
                    "bid_sz": float(t.get("bidSz", 0) or 0),
                    "ask_sz": float(t.get("askSz", 0) or 0),
                    "vol24h_usdt": float(t.get("usdtVolume", 0) or 0),
                })
            return records
    except Exception as exc:
        logger.warning("fetch_ticker_batch failed: %s", exc)
        return []


async def run() -> None:
    symbols = _load_universe()
    if not symbols:
        logger.error("empty universe cache")
        return

    logger.info("positioning collector starting: %d symbols", len(symbols))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                records = await _fetch_ticker_batch(session, symbols)
                if records:
                    _append_records(records)
                    logger.debug("appended %d positioning records", len(records))
            except Exception as exc:
                logger.warning("positioning poll error: %s", exc)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
