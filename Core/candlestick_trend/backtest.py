"""Signal-quality check for candlestick_trend, across a universe of pairs
and multiple timeframes, on Bitget spot data via its public REST API (no
auth, no ccxt dependency).

This deliberately does NOT model any trade management (no stop-loss, no
take-profit, no holding-period rule) -- two earlier attempts (a fixed
holding period, then a structural 2:1 TP/SL) both showed weak results, and
there was no way to tell whether that meant the signals themselves lack
directional edge, or just that the exit rule was a bad fit. This measures
the signals on their own terms instead:

  - forward return at fixed horizons (1/3/5/10 bars after entry), signed
    for the signal's direction -- does price move the way the signal said,
    on average, independent of any exit rule?
  - MFE (max favorable excursion) / MAE (max adverse excursion) within
    EXCURSION_WINDOW bars -- how far did price move in your favor before
    it moved against you, and vice versa? A signal with MFE consistently
    well above MAE has room for *some* profitable stop/target; one where
    they're close or MAE dominates doesn't, no matter how it's traded.

Entry is still the NEXT bar's open after the signal bar closes (no
lookahead). No fees/slippage modeled.

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
FORWARD_HORIZONS = (1, 3, 5, 10)  # bars after entry
EXCURSION_WINDOW = 20  # bars, for MFE/MAE -- covers the longest horizon plus margin

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
class SignalRecord:
    symbol: str
    timeframe: str
    pattern: str
    action: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float
    effective_confidence: float
    fwd_ret_1: float | None
    fwd_ret_3: float | None
    fwd_ret_5: float | None
    fwd_ret_10: float | None
    mfe: float | None
    mae: float | None


def measure(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    report_start: pd.Timestamp,
    horizons: tuple[int, ...] = FORWARD_HORIZONS,
    excursion_window: int = EXCURSION_WINDOW,
) -> list[SignalRecord]:
    signals = generate_signals(df)
    records: list[SignalRecord] = []
    for sig in signals:
        signal_time = df.index[sig.index]
        if signal_time < report_start:
            continue  # warmup-only signal, outside the report window
        entry_pos = sig.index + 1
        if entry_pos >= len(df):
            continue  # not enough bars left to even enter
        entry_price = df["open"].iloc[entry_pos]
        sign = 1 if sig.action == "long" else -1

        fwd_ret = {}
        for h in horizons:
            pos = entry_pos + h
            fwd_ret[h] = sign * (df["close"].iloc[pos] - entry_price) / entry_price if pos < len(df) else None

        window_end = min(entry_pos + excursion_window, len(df) - 1)
        window = df.iloc[entry_pos : window_end + 1]
        if sig.action == "long":
            mfe = (window["high"].max() - entry_price) / entry_price
            mae = (entry_price - window["low"].min()) / entry_price
        else:
            mfe = (entry_price - window["low"].min()) / entry_price
            mae = (window["high"].max() - entry_price) / entry_price

        records.append(
            SignalRecord(
                symbol=symbol,
                timeframe=timeframe,
                pattern=sig.pattern,
                action=sig.action,
                signal_time=signal_time,
                entry_time=df.index[entry_pos],
                entry_price=entry_price,
                effective_confidence=sig.effective_confidence,
                fwd_ret_1=fwd_ret[1],
                fwd_ret_3=fwd_ret[3],
                fwd_ret_5=fwd_ret[5],
                fwd_ret_10=fwd_ret[10],
                mfe=mfe,
                mae=mae,
            )
        )
    return records


def run(n_pairs: int = 15, timeframes: dict[str, int] = TIMEFRAMES) -> list[SignalRecord]:
    symbols = top_n_crypto_usdt_pairs(n_pairs)
    all_records: list[SignalRecord] = []
    for timeframe, bars_per_day in timeframes.items():
        report_bars = REPORT_WINDOW_DAYS * bars_per_day
        limit = report_bars + WARMUP_BARS
        for symbol in symbols:
            df = fetch_ohlcv(symbol, timeframe, limit)
            report_start = df.index[-1] - pd.Timedelta(days=REPORT_WINDOW_DAYS)
            all_records.extend(measure(df, symbol, timeframe, report_start))
    return all_records


def _summarize(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    for key, group in df.groupby(group_cols):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(group_cols, key))
        row["n"] = len(group)
        for h in FORWARD_HORIZONS:
            col = f"fwd_ret_{h}"
            valid = group[col].dropna()
            row[f"hit_rate_{h}"] = (valid > 0).mean() if len(valid) else None
            row[f"avg_fwd_ret_{h}_pct"] = valid.mean() * 100 if len(valid) else None
        row["avg_mfe_pct"] = group["mfe"].mean() * 100
        row["avg_mae_pct"] = group["mae"].mean() * 100
        row["mfe_mae_ratio"] = (group["mfe"].mean() / group["mae"].mean()) if group["mae"].mean() > 0 else None
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_by_pattern(records: list[SignalRecord]) -> pd.DataFrame:
    """One row per pattern, pooled across every pair and timeframe -- does
    this pattern have directional edge at all, independent of instrument
    or timeframe?"""
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame([r.__dict__ for r in records])
    return _summarize(df, ["pattern"]).sort_values("n", ascending=False).reset_index(drop=True)


def summarize_by_timeframe(records: list[SignalRecord]) -> pd.DataFrame:
    """One row per timeframe, pooled across every pair and pattern."""
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame([r.__dict__ for r in records])
    result = _summarize(df, ["timeframe"])
    result["timeframe"] = pd.Categorical(result["timeframe"], categories=list(TIMEFRAMES), ordered=True)
    return result.sort_values("timeframe").reset_index(drop=True)


def summarize_by_pattern_and_timeframe(records: list[SignalRecord]) -> pd.DataFrame:
    """One row per (pattern, timeframe) -- the full detail cut."""
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame([r.__dict__ for r in records])
    result = _summarize(df, ["pattern", "timeframe"])
    result["timeframe"] = pd.Categorical(result["timeframe"], categories=list(TIMEFRAMES), ordered=True)
    return result.sort_values(["timeframe", "pattern"]).reset_index(drop=True)


if __name__ == "__main__":
    records = run()
    print(f"{len(records)} total signals across {REPORT_WINDOW_DAYS} days, {len(TIMEFRAMES)} timeframes")
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print("\n=== by timeframe ===")
        print(summarize_by_timeframe(records).to_string(index=False))
        print("\n=== by pattern ===")
        print(summarize_by_pattern(records).to_string(index=False))
