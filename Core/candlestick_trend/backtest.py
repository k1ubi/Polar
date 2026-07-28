"""Quick sanity backtest: candlestick_trend's signals over a recent window,
across a universe of pairs and multiple timeframes, on Bitget spot data via
its public REST API (no auth, no ccxt dependency).

This is NOT a rigorous backtest -- the Core has no position-sizing,
stop-loss, or execution model yet (see Doc/Core/candlestick_trend.md). To
turn a signal into a number, this script applies the simplest possible,
clearly-labeled evaluation: enter at the NEXT bar's open after the signal
bar closes (no lookahead), exit HOLD_BARS bars later, no fees/slippage.
That exit rule is a placeholder for getting a directional read quickly,
not a claim about how this would be traded live.

`signal.py`'s default `sensitivity=150` was empirically tuned against 1h
BTC/USDT data specifically (see its module docstring) -- this script runs
every timeframe under that SAME untouched default on purpose, so the
per-timeframe results below show whether that tuning holds up or breaks
down on higher timeframes (which move a larger % per bar and so measure a
structurally larger trend magnitude), rather than silently re-tuning it
away.

Usage: python -m Core.candlestick_trend.backtest
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import pandas as pd

from Core.candlestick_trend.signal import generate_signals

BITGET_TICKERS_URL = "https://api.bitget.com/api/v2/spot/market/tickers"
BITGET_SYMBOLS_URL = "https://api.bitget.com/api/v2/spot/public/symbols"
BITGET_CANDLES_URL = "https://api.bitget.com/api/v2/spot/market/candles"
BITGET_MAX_CANDLES = 1000
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

STABLECOINS = {"USDC", "USDGO", "USDT", "FDUSD", "TUSD", "DAI", "USDE", "PYUSD"}
MIN_MARKET_CAP_USD = 10_000_000

REPORT_WINDOW_DAYS = 7
WARMUP_BARS = 100  # >> IDENTITY_TREND_LOOKBACK (50) with headroom, same across all timeframes
HOLD_BARS = 5  # placeholder fixed holding period, in bars -- see module docstring

# granularity -> bars per day, so we know how many report-window bars to ask for
TIMEFRAMES = {
    "15min": 24 * 4,
    "1h": 24,
    "4h": 6,
    "1day": 1,
}


def _fetch_json(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["data"]


def _coingecko_market_caps(pages: int = 3) -> dict[str, float]:
    """symbol (uppercase) -> market cap USD, from CoinGecko's public
    /coins/markets, largest-cap match wins on ticker collisions (e.g. a
    micro-cap token sharing a ticker with an established coin)."""
    mcap_by_symbol: dict[str, float] = {}
    for page in range(1, pages + 1):
        coins = _fetch_json_full(f"{COINGECKO_MARKETS_URL}?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}")
        for c in coins:
            sym = c["symbol"].upper()
            mc = c["market_cap"] or 0
            if sym not in mcap_by_symbol or mc > mcap_by_symbol[sym]:
                mcap_by_symbol[sym] = mc
        if page < pages:
            time.sleep(8)  # CoinGecko's free public tier rate-limits aggressively
    return mcap_by_symbol


def _fetch_json_full(url: str, retries: int = 5) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(15)
                continue
            raise


def top_n_crypto_usdt_pairs(n: int = 15, min_market_cap: float = MIN_MARKET_CAP_USD) -> list[str]:
    """Top `n` USDT spot pairs by 24h volume, restricted to base assets with
    a CoinGecko market cap above `min_market_cap` -- excludes Bitget's
    tokenized-stock products (baseCoin prefixed 'r', e.g. rNVDA/rTSLA),
    stablecoin base assets, and micro-cap/untracked tokens (a coin CoinGecko
    doesn't even list in its top ~1250 by market cap fails this floor by
    construction)."""
    symbols = _fetch_json(BITGET_SYMBOLS_URL)
    tickers = _fetch_json(BITGET_TICKERS_URL)
    mcap_by_symbol = _coingecko_market_caps()

    eligible = {
        s["symbol"]: s["baseCoin"].upper()
        for s in symbols
        if s["quoteCoin"] == "USDT"
        and not s["baseCoin"].startswith("r")
        and s["baseCoin"].upper() not in STABLECOINS
        and s["status"] == "online"
        and mcap_by_symbol.get(s["baseCoin"].upper(), 0) >= min_market_cap
    }
    pairs = [t for t in tickers if t["symbol"] in eligible]
    pairs.sort(key=lambda t: float(t["usdtVolume"]), reverse=True)
    return [t["symbol"] for t in pairs[:n]]


def fetch_ohlcv(symbol: str, granularity: str, limit: int) -> pd.DataFrame:
    limit = min(limit, BITGET_MAX_CANDLES)
    url = f"{BITGET_CANDLES_URL}?symbol={symbol}&granularity={granularity}&limit={limit}"
    rows = _fetch_json(url)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "base_vol", "quote_vol", "usdt_vol"])
    for col in ("open", "high", "low", "close", "base_vol"):
        df[col] = df[col].astype(float)
    df.index = pd.to_datetime(df["ts"].astype(int), unit="ms", utc=True)
    return df[["open", "high", "low", "close"]].assign(volume=df["base_vol"].values)


@dataclass
class Trade:
    symbol: str
    timeframe: str
    pattern: str
    action: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    effective_confidence: float
    return_pct: float


def simulate(df: pd.DataFrame, symbol: str, timeframe: str, report_start: pd.Timestamp, hold_bars: int = HOLD_BARS) -> list[Trade]:
    signals = generate_signals(df)
    trades: list[Trade] = []
    for sig in signals:
        signal_time = df.index[sig.index]
        if signal_time < report_start:
            continue  # warmup-only signal, outside the report window
        entry_pos = sig.index + 1
        exit_pos = sig.index + 1 + hold_bars
        if exit_pos >= len(df):
            continue  # not enough bars left to close the trade within the fetched data
        entry_price = df["open"].iloc[entry_pos]
        exit_price = df["close"].iloc[exit_pos]
        raw_return = (exit_price - entry_price) / entry_price
        return_pct = raw_return if sig.action == "long" else -raw_return
        trades.append(
            Trade(
                symbol=symbol,
                timeframe=timeframe,
                pattern=sig.pattern,
                action=sig.action,
                signal_time=signal_time,
                entry_time=df.index[entry_pos],
                entry_price=entry_price,
                exit_time=df.index[exit_pos],
                exit_price=exit_price,
                effective_confidence=sig.effective_confidence,
                return_pct=return_pct,
            )
        )
    return trades


def run(n_pairs: int = 15, timeframes: dict[str, int] = TIMEFRAMES) -> list[Trade]:
    symbols = top_n_crypto_usdt_pairs(n_pairs)
    all_trades: list[Trade] = []
    for timeframe, bars_per_day in timeframes.items():
        report_bars = REPORT_WINDOW_DAYS * bars_per_day
        limit = report_bars + WARMUP_BARS
        for symbol in symbols:
            df = fetch_ohlcv(symbol, timeframe, limit)
            report_start = df.index[-1] - pd.Timedelta(days=REPORT_WINDOW_DAYS)
            all_trades.extend(simulate(df, symbol, timeframe, report_start))
    return all_trades


def summarize(trades: list[Trade]) -> pd.DataFrame:
    """One row per (symbol, timeframe): trades, wins, losses, win rate,
    total PnL % (sum of per-trade returns), avg PnL % per trade."""
    if not trades:
        return pd.DataFrame(columns=["symbol", "timeframe", "trades", "wins", "losses", "win_rate", "total_pnl_pct", "avg_pnl_pct"])
    df = pd.DataFrame([t.__dict__ for t in trades])
    rows = []
    for (symbol, timeframe), group in df.groupby(["symbol", "timeframe"]):
        wins = int((group["return_pct"] > 0).sum())
        losses = int((group["return_pct"] <= 0).sum())
        rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "trades": len(group),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(group),
                "total_pnl_pct": group["return_pct"].sum() * 100,
                "avg_pnl_pct": group["return_pct"].mean() * 100,
            }
        )
    order = list(TIMEFRAMES)
    result = pd.DataFrame(rows)
    result["timeframe"] = pd.Categorical(result["timeframe"], categories=order, ordered=True)
    return result.sort_values(["timeframe", "symbol"]).reset_index(drop=True)


if __name__ == "__main__":
    trades = run()
    summary = summarize(trades)
    print(f"{len(trades)} total trades across {REPORT_WINDOW_DAYS} days, {len(TIMEFRAMES)} timeframes")
    with pd.option_context("display.max_rows", None, "display.width", 140):
        print(summary.to_string(index=False))
