"""Fetch and cache Bitget SPOT 1H OHLCV bars for the MACD+RSI research universe.

Paginates backwards in time using the `endTime` parameter until START_DATE
is reached. Saves each symbol as Research/data/bitget_1h/<SYMBOL>.json in
ascending order (oldest first), same format as the Binance daily cache.

Rate limiting: ~0.12s between requests per symbol, 4 workers in parallel.
Expected runtime: ~5 minutes for a 50-symbol universe.

Run: python3 -m Research.bitget_1h_fetch [--symbols N] [--workers W]
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

BITGET_CANDLES_URL = "https://api.bitget.com/api/v2/spot/market/candles"
BITGET_TICKERS_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
MAX_PER_REQUEST = 1000
DELAY_BETWEEN_REQUESTS = 0.15   # seconds
START_DATE = pd.Timestamp("2021-01-01", tz="UTC")
MIN_MCAP_USD = 500_000_000      # $500M mcap floor — keeps universe high-quality
STABLECOINS = {"USDC", "USDT", "FDUSD", "TUSD", "DAI", "USDE", "PYUSD", "USDGO"}

CACHE_DIR = Path(__file__).parent / "data" / "bitget_1h"


def _fetch(url: str, retries: int = 4) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(3)
                continue
            raise
    return []


def _top_symbols(n: int) -> list[str]:
    """Return top-N USDT spot symbols by market cap, excluding stablecoins."""
    mcap: dict[str, float] = {}
    for page in range(1, 7):
        url = f"{COINGECKO_MARKETS_URL}?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                coins = json.load(resp)
            for c in coins:
                sym = c["symbol"].upper()
                mc = c.get("market_cap") or 0
                if mc < MIN_MCAP_USD:
                    break
                mcap[sym] = max(mcap.get(sym, 0), mc)
        except Exception as exc:
            logger.warning("CoinGecko page %d failed: %s", page, exc)
            break

    # Get available USDT spot pairs from Bitget
    try:
        tickers = _fetch(BITGET_TICKERS_URL)
        available = {t["symbol"] for t in tickers if t["symbol"].endswith("USDT")}
    except Exception as exc:
        logger.error("failed to fetch Bitget tickers: %s", exc)
        return []

    ranked = sorted(
        [(sym + "USDT", mc) for sym, mc in mcap.items()
         if sym + "USDT" in available and sym not in STABLECOINS],
        key=lambda x: -x[1],
    )
    result = [sym for sym, _ in ranked[:n]]
    logger.info("universe: %d symbols (top %d by mcap)", len(result), n)
    return result


def _fetch_symbol(symbol: str) -> tuple[str, list | None]:
    """Fetch all 1H bars from START_DATE to now. Returns (symbol, bars) sorted asc."""
    all_bars: list = []
    end_time_ms: int | None = None
    start_ms = int(START_DATE.timestamp() * 1000)

    while True:
        url = f"{BITGET_CANDLES_URL}?symbol={symbol}&granularity=1h&limit={MAX_PER_REQUEST}"
        if end_time_ms is not None:
            url += f"&endTime={end_time_ms}"
        try:
            batch = _fetch(url)
        except Exception as exc:
            logger.warning("%s fetch error: %s", symbol, exc)
            return symbol, None

        if not batch:
            break

        all_bars.extend(batch)
        # Bitget returns bars newest-first; oldest in this batch is last
        oldest_ts = int(batch[-1][0])
        if oldest_ts <= start_ms:
            break
        end_time_ms = oldest_ts - 1
        time.sleep(DELAY_BETWEEN_REQUESTS)

    if not all_bars:
        return symbol, None

    # Deduplicate and sort ascending (oldest first)
    seen: dict[int, list] = {}
    for row in all_bars:
        ts = int(row[0])
        if ts >= start_ms:
            seen[ts] = row
    sorted_bars = [seen[k] for k in sorted(seen)]

    if len(sorted_bars) < 500:
        logger.warning("%s: only %d bars — skipping", symbol, len(sorted_bars))
        return symbol, None

    logger.info("%s: %d bars (%.1f months)", symbol, len(sorted_bars),
                len(sorted_bars) / 24 / 30.5)
    return symbol, sorted_bars


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, default=50, help="Universe size")
    parser.add_argument("--workers", type=int, default=4, help="Parallel fetchers")
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    symbols = _top_symbols(args.symbols)
    if not symbols:
        logger.error("no symbols returned from CoinGecko/Bitget")
        return

    # Always include BTC and ETH as regime anchors
    for anchor in ("BTCUSDT", "ETHUSDT"):
        if anchor not in symbols:
            symbols.insert(0, anchor)
    symbols = list(dict.fromkeys(symbols))  # deduplicate preserving order

    logger.info("fetching %d symbols with %d workers", len(symbols), args.workers)

    saved = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_fetch_symbol, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym, bars = fut.result()
            if bars is not None:
                out = CACHE_DIR / f"{sym}.json"
                out.write_text(json.dumps(bars, separators=(",", ":")))
                saved += 1

    logger.info("done: %d/%d symbols cached to %s", saved, len(symbols), CACHE_DIR)


if __name__ == "__main__":
    main()
