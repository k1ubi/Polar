"""MACD + RSI on 1H bars — out-of-sample pre-registered test.

Motivation: the daily MACD+RSI long-only strategy (macd_rsi_long_oos.py)
nearly passed (Sharpe 0.091 vs threshold 0.10) but failed in 2022 and 2025
because the daily BTC SMA20/65 regime filter let through entries in sharp
bounces within larger bear markets. On 1H bars the signal-to-entry lag is
much shorter (next candle = 1 hour vs 1 day) which means stops get hit or
targets reached in hours rather than days — reducing exposure to
multi-day reversals in bad regimes.

Strategy (fixed before first run):
  timeframe  1H (Bitget SPOT USDT pairs)
  signal     MACD(12,26,9) histogram crosses zero upward (long only)
             Uses MACD on 1H: fast=12h, slow=26h, signal=9h
  filters:
    1. RSI(14) must be between 45 and 75 at signal bar
       (above 45 = building momentum; below 75 = not yet overbought)
    2. Price > SMA50 (50H ≈ 2-day SMA — medium-term trend filter)
    3. BTC DAILY regime: BTC daily close > SMA20 daily (DAILY data from
       Binance cache for robustness — do not use 1H BTC here, too noisy)
  stop       1.5 × ATR14(1H) below entry
  target     2.5 × ATR14(1H) above entry  (reduced vs daily: 1.67:1 R/R;
             1H trades need tighter targets — 3× ATR on 1H is often > 1 day)
  entry      next bar's open (no lookahead)
  time stop  30 bars (= 30 hours ≈ 1.25 days)
  costs      0.18% round trip

Pre-registered PASS criteria (ALL must hold on the FRESH universe, i.e.
symbols NOT in Research/data/bitget_1h/ on the first run's contaminated set):
  1. Mean R > 0, one-sample t p < 0.05
  2. Sharpe ratio (mean R / std R) > 0.12
  3. Win rate > 38%
  4. Positive mean R in at least 50% of calendar years with >= 20 trades

Data requirements:
  Run Research/bitget_1h_fetch.py first (caches to Research/data/bitget_1h/)
  Run Research/binance_daily.py first   (for BTC daily regime)

Run: python3 -m Research.macd_rsi_1h_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats

# ── paths ─────────────────────────────────────────────────────────────────────
from pathlib import Path
from Research.trend_oos import CACHE as BINANCE_CACHE, polar_bases

HOURLY_CACHE = Path(__file__).parent / "data" / "bitget_1h"

# ── constants (do NOT change after first run) ─────────────────────────────────
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG = 9
RSI_PERIOD = 14
RSI_MIN = 45        # entry allowed only if RSI > 45 (building momentum)
RSI_MAX = 75        # entry allowed only if RSI < 75 (not overbought)
SMA_PERIOD = 50     # 50H ≈ 2-day medium-term trend filter
BTC_SMA_DAILY = 20  # BTC daily 4-week SMA regime filter
ATR_PERIOD = 14
STOP_ATR = 1.5
TARGET_ATR = 2.5    # tighter target on 1H (2.5 vs 3.0 on daily)
MAX_HOLD = 30       # bars (30 hours)
FEE = 0.0018
START = pd.Timestamp("2021-06-01", tz="UTC")  # after enough warm-up bars
MIN_BARS = 500


# ── indicator helpers ─────────────────────────────────────────────────────────

def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1)
    out = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        if np.isnan(arr[i]):
            continue
        out[i] = arr[i] if (i == 0 or np.isnan(out[i - 1])) else alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _macd_hist(close: np.ndarray) -> np.ndarray:
    macd = _ema(close, MACD_FAST) - _ema(close, MACD_SLOW)
    return macd - _ema(np.where(np.isnan(macd), np.nan, macd), MACD_SIG)


def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    delta = np.diff(close, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / period
    n = len(close)
    ag, al = np.full(n, np.nan), np.full(n, np.nan)
    for i in range(period, n):
        if np.isnan(ag[i - 1]):
            ag[i] = np.mean(gain[1:i + 1])
            al[i] = np.mean(loss[1:i + 1])
        else:
            ag[i] = alpha * gain[i] + (1 - alpha) * ag[i - 1]
            al[i] = alpha * loss[i] + (1 - alpha) * al[i - 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = ag / np.where(al == 0, np.nan, al)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    alpha = 1.0 / period
    out = np.full(len(tr), np.nan)
    for i in range(len(tr)):
        if np.isnan(tr[i]):
            continue
        out[i] = tr[i] if (i == 0 or np.isnan(out[i - 1])) else alpha * tr[i] + (1 - alpha) * out[i - 1]
    return out


# ── BTC daily regime (uses Binance daily cache) ────────────────────────────────

def _btc_regime() -> pd.Series | None:
    """Returns a daily pd.Series of bool: True = BTC daily close > SMA20."""
    btc_path = BINANCE_CACHE / "BTCUSDT.json"
    if not btc_path.exists():
        return None
    rows = json.loads(btc_path.read_text())
    a = np.array(rows, dtype=float)
    ts = pd.to_datetime(a[:, 0], unit="ms", utc=True).normalize()
    c = a[:, 4]
    sma = pd.Series(c, index=ts).rolling(BTC_SMA_DAILY).mean()
    return pd.Series(c, index=ts) > sma


# ── trade simulation ──────────────────────────────────────────────────────────

def trades_for(rows: list, btc_regime_daily: pd.Series | None) -> list[dict]:
    a = np.array([[float(x) for x in row[:6]] for row in rows], dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts_ms, o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(c)

    hist = _macd_hist(c)
    rsi = _rsi(c, RSI_PERIOD)
    sma50 = pd.Series(c).rolling(SMA_PERIOD).mean().to_numpy()
    atr = _atr(h, l, c, ATR_PERIOD)
    dates_utc = pd.to_datetime(ts_ms, unit="ms", utc=True).normalize()

    out = []
    warmup = MACD_SLOW + MACD_SIG + SMA_PERIOD + BTC_SMA_DAILY * 24 + 10
    i = max(warmup, 100)
    while i < n - 1:
        bar_dt = dates_utc[i]
        if bar_dt < START:
            i += 1
            continue

        prev_h, curr_h = hist[i - 1], hist[i]
        if np.isnan(prev_h) or np.isnan(curr_h):
            i += 1
            continue
        if np.isnan(rsi[i]) or np.isnan(sma50[i]) or np.isnan(atr[i]) or atr[i] <= 0:
            i += 1
            continue

        # MACD histogram zero-cross upward
        if not (prev_h < 0 and curr_h > 0):
            i += 1
            continue

        # RSI band filter
        if not (RSI_MIN < rsi[i] < RSI_MAX):
            i += 1
            continue

        # Per-symbol 50H trend filter
        if c[i] <= sma50[i]:
            i += 1
            continue

        # BTC daily regime (check the DAILY bar for the bar's date)
        if btc_regime_daily is not None:
            regime = btc_regime_daily.get(bar_dt)
            if regime is not None and not regime:
                i += 1
                continue

        entry = o[i + 1]
        if entry <= 0:
            i += 1
            continue

        stop_dist = STOP_ATR * atr[i]
        target_dist = TARGET_ATR * atr[i]
        stop = entry - stop_dist
        target = entry + target_dist

        exit_px = None
        j = i + 1
        for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
            if o[j] <= stop:
                exit_px = o[j]
                break
            if l[j] <= stop:
                exit_px = stop
                break
            if h[j] >= target:
                exit_px = target
                break
        if exit_px is None:
            exit_px = c[min(j, n - 1)]

        r = ((exit_px - entry) - FEE * entry) / stop_dist
        out.append({
            "date": bar_dt.date(),
            "r": float(r),
        })
        i = j + 1

    return out


# ── reporting ─────────────────────────────────────────────────────────────────

def describe(label: str, r: pd.Series) -> None:
    if len(r) < 2:
        print(f"  {label:52s} n={len(r)}")
        return
    _, p = stats.ttest_1samp(r, 0)
    sharpe = r.mean() / r.std() if r.std() > 0 else 0.0
    print(
        f"  {label:52s} n={len(r):5d}  mean {r.mean():+.3f}R  "
        f"win {100*(r > 0).mean():3.0f}%  Sharpe {sharpe:.2f}  p {p:.4f}"
    )


def main() -> None:
    if not HOURLY_CACHE.exists() or not any(HOURLY_CACHE.iterdir()):
        print("ERROR: 1H cache is empty.")
        print("  Run first: python3 -m Research.bitget_1h_fetch")
        return

    regime = _btc_regime()
    if regime is None:
        print("WARNING: BTC daily data missing — regime filter disabled.")
        print("  Run first: python3 -m Research.binance_daily")

    # contaminated = symbols that were in any prior Polar universe cache
    contaminated = polar_bases()

    groups: dict[str, list] = {"fresh": [], "polar": []}
    for f in sorted(HOURLY_CACHE.glob("*.json")):
        stem = f.stem  # e.g. BTCUSDT
        rows = json.loads(f.read_text())
        t = trades_for(rows, regime)
        for x in t:
            x["symbol"] = stem
        bucket = "polar" if stem.removesuffix("USDT") in contaminated else "fresh"
        groups[bucket].extend(t)

    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        if df.empty:
            print(f"\n{name.upper()}: no trades.")
            continue
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} signals ===")
        describe("all longs (1H MACD+RSI, BTC regime)", df.r)
        yr = df.groupby(pd.to_datetime(df.date).dt.year).r.agg(["mean", "count"])
        print(
            "  by year: "
            + "  ".join(f"{y}:{m:+.2f}R(n{int(k)})" for y, (m, k) in yr.iterrows())
        )

    val = pd.Series([x["r"] for x in groups["fresh"]])
    if val.empty:
        print("\nVERDICT: FAIL (no fresh trades)")
        return

    _, p_val = stats.ttest_1samp(val, 0)
    sharpe = val.mean() / val.std() if val.std() > 0 else 0.0
    win_rate = (val > 0).mean()
    fresh_df = pd.DataFrame(groups["fresh"])
    yr_m = fresh_df.groupby(pd.to_datetime(fresh_df.date).dt.year).r.agg(["mean", "count"])
    years_ok = yr_m[yr_m["count"] >= 20]
    pos_yr_pct = (years_ok["mean"] > 0).mean() if len(years_ok) >= 2 else 0.0

    c1 = val.mean() > 0 and p_val < 0.05
    c2 = sharpe > 0.12
    c3 = win_rate > 0.38
    c4 = pos_yr_pct >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   →  mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.12          →  Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 38%         →  win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years →  {100*pos_yr_pct:.0f}%")
    all_pass = c1 and c2 and c3 and c4
    print(f"\nVERDICT: {'PASS — deploy to paper daemon' if all_pass else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
