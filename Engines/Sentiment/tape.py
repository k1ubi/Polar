"""Trade tape collector — aggressor side, delta, CVD.

Streams Bitget WebSocket trade feed for every symbol in the universe,
aggregates into 1-minute OHLCV-style tape records, and appends to
daily JSONL files under Engines/Sentiment/data/tape/YYYY-MM-DD.jsonl.

Run via: python3 -m Engines.Sentiment.tape
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
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
DATA_DIR = Path(__file__).parent / "data" / "tape"

BITGET_WS = "wss://ws.bitget.com/v2/ws/public"
MAX_CONSECUTIVE_FAILURES = 5

# Symbols that have failed too many times and are parked
_parked: dict[str, int] = {}


def _load_universe() -> list[str]:
    try:
        return json.loads(UNIVERSE_CACHE.read_text()).get("symbols", [])
    except Exception:
        return []


def _today_file() -> Path:
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{day}.jsonl"


def _append_record(record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"))
    with _today_file().open("a") as f:
        f.write(line + "\n")


class MinuteBucket:
    __slots__ = ("minute", "symbol", "buy_vol", "sell_vol", "buy_n", "sell_n", "last_price", "gaps")

    def __init__(self, minute: str, symbol: str):
        self.minute = minute
        self.symbol = symbol
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.buy_n = 0
        self.sell_n = 0
        self.last_price = 0.0
        self.gaps = 0

    def add(self, side: str, size: float, price: float) -> None:
        if side == "buy":
            self.buy_vol += size
            self.buy_n += 1
        else:
            self.sell_vol += size
            self.sell_n += 1
        self.last_price = price

    def to_record(self) -> dict:
        return {
            "minute": self.minute,
            "symbol": self.symbol,
            "buy_vol": round(self.buy_vol, 4),
            "sell_vol": round(self.sell_vol, 4),
            "delta": round(self.buy_vol - self.sell_vol, 4),
            "buy_n": self.buy_n,
            "sell_n": self.sell_n,
            "last_price": self.last_price,
            "gaps": self.gaps,
        }


_buckets: dict[str, MinuteBucket] = {}


def _current_minute() -> str:
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:00+00:00")


def _flush_stale(current_minute: str) -> None:
    stale = [sym for sym, b in _buckets.items() if b.minute != current_minute]
    for sym in stale:
        bucket = _buckets.pop(sym)
        _append_record(bucket.to_record())


def _on_trade(symbol: str, trades: list[dict]) -> None:
    minute = _current_minute()
    _flush_stale(minute)

    if symbol not in _buckets:
        _buckets[symbol] = MinuteBucket(minute, symbol)
    bucket = _buckets[symbol]

    for trade in trades:
        side = "buy" if trade.get("side") == "buy" else "sell"
        size = float(trade.get("baseVolume", trade.get("size", 0)))
        price = float(trade.get("price", 0))
        bucket.add(side, size, price)


async def _subscribe(ws: aiohttp.ClientWebSocketResponse, symbols: list[str]) -> None:
    """Subscribe to trade channels for all symbols in batches."""
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        args = [{"instType": "USDT-FUTURES", "channel": "trade", "instId": s} for s in batch]
        await ws.send_json({"op": "subscribe", "args": args})
        await asyncio.sleep(0.1)


async def run() -> None:
    symbols = _load_universe()
    if not symbols:
        logger.error("empty universe cache")
        return

    logger.info("tape collector starting: %d symbols", len(symbols))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.ws_connect(BITGET_WS, heartbeat=20) as ws:
                    await _subscribe(ws, symbols)
                    logger.info("subscribed to %d trade channels", len(symbols))

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("event") == "error":
                                logger.warning("WS error: %s", data)
                                continue
                            arg = data.get("arg", {})
                            symbol = arg.get("instId")
                            trades = data.get("data", [])
                            if symbol and trades:
                                _on_trade(symbol, trades)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except Exception as exc:
                logger.warning("tape WS disconnected: %s — reconnecting in 5s", exc)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
