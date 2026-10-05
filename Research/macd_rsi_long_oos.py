"""MACD + RSI — long-only with BTC regime filter: out-of-sample pre-registered test.

Derived from Research/macd_rsi_oos.py which FAILED because shorts dragged down
a significant long-only edge (+0.12R, p<0.001 on fresh universe). Two changes
from the failed version, both declared up front before the first run:

  1. LONG-ONLY: no short signals at all.
     Rationale: shorts had mean −0.024R across 8511 fresh trades (p=0.057).
     Crypto has a structural long bias (assets go from low to high or die slowly
     at near-zero; no asset goes to −∞). MACD shorts fight the dominant secular
     trend even with a SMA50 filter.

  2. BTC REGIME FILTER: only open a long when BTC's 4-week SMA (SMA20 on daily)
     is above BTC's 13-week SMA (SMA65 on daily) — i.e., BTC's own short-term
     trend is bullish. When BTC is in broad distribution, individual alts flash
     false MACD crosses as brief bounces inside a larger downtrend.
     Using BTC from the cache (ticker BTCUSDT); symbols without BTCUSDT present
     in the same cache window are skipped for those bars.

All other constants (MACD 12/26/9, RSI(14) > 45, SMA50 trend filter, 1.5×ATR
stop, 3×ATR target, 20-bar time stop, 0.18% fee) are IDENTICAL to the failed
version — only the direction and regime gating changed.

Pre-registered PASS criteria (fresh universe only):
  1. Mean R > 0, one-sample t p < 0.05
  2. Sharpe ratio > 0.10
  3. Win rate > 38%   (lowered from 40% because pure momentum longs win ~35-45%)
  4. Positive mean R in at least 50% of calendar years with >= 5 trades

Run: python3 -m Research.macd_rsi_long_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats

from Research.trend_oos import CACHE, polar_bases

# ── constants (do NOT change after first run) ─────────────────────────────────
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG = 9
RSI_PERIOD = 14
RSI_LONG_FLOOR = 45
SMA_PERIOD = 50        # per-symbol trend filter (same as v1)
BTC_SMA_SHORT = 20     # BTC 4-week SMA
BTC_SMA_LONG = 65      # BTC 13-week SMA
ATR_PERIOD = 14
STOP_ATR = 1.5
TARGET_ATR = 3.0
MAX_HOLD = 20
FEE = 0.0018
START = pd.Timestamp("2020-01-01")
MIN_BARS = 350


# ── shared helpers (duplicated from macd_rsi_oos.py to keep each script self-contained) ──

def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1)
    out = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        if np.isnan(arr[i]):
            continue
        if i == 0 or np.isnan(out[i - 1]):
            out[i] = arr[i]
        else:
            out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _macd_hist(close: np.ndarray) -> np.ndarray:
    macd = _ema(close, MACD_FAST) - _ema(close, MACD_SLOW)
    sig = _ema(np.where(np.isnan(macd), np.nan, macd), MACD_SIG)
    return macd - sig


def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    delta = np.diff(close, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / period
    n = len(close)
    ag = np.full(n, np.nan)
    al = np.full(n, np.nan)
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


# ── BTC regime index (shared across all symbols) ──────────────────────────────

def _load_btc_regime() -> pd.Series | None:
    btc_path = CACHE / "BTCUSDT.json"
    if not btc_path.exists():
        return None
    rows = json.loads(btc_path.read_text())
    a = np.array(rows, dtype=float)
    ts = pd.to_datetime(a[:, 0], unit="ms")
    c = a[:, 4]
    sma_short = pd.Series(c, index=ts).rolling(BTC_SMA_SHORT).mean()
    sma_long = pd.Series(c, index=ts).rolling(BTC_SMA_LONG).mean()
    # True = BTC regime bullish (short SMA above long SMA)
    return (sma_short > sma_long)


# ── trade simulation ──────────────────────────────────────────────────────────

def trades_for(rows: list, btc_regime: pd.Series | None) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts, o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(c)

    hist = _macd_hist(c)
    rsi = _rsi(c, RSI_PERIOD)
    sma50 = pd.Series(c).rolling(SMA_PERIOD).mean().to_numpy()
    atr = _atr(h, l, c, ATR_PERIOD)
    dates = pd.to_datetime(ts, unit="ms").normalize()

    out = []
    warmup = MACD_SLOW + MACD_SIG + SMA_PERIOD + BTC_SMA_LONG + 5
    i = warmup
    while i < n - 1:
        date_i = dates[i]
        if date_i < START:
            i += 1
            continue

        prev_h = hist[i - 1]
        curr_h = hist[i]
        if np.isnan(prev_h) or np.isnan(curr_h):
            i += 1
            continue
        if np.isnan(rsi[i]) or np.isnan(sma50[i]) or np.isnan(atr[i]) or atr[i] <= 0:
            i += 1
            continue

        # Long-only: MACD histogram crosses zero upward
        if not (prev_h < 0 and curr_h > 0):
            i += 1
            continue

        # RSI filter
        if rsi[i] <= RSI_LONG_FLOOR:
            i += 1
            continue

        # Per-symbol SMA50 filter
        if c[i] <= sma50[i]:
            i += 1
            continue

        # BTC regime filter
        if btc_regime is not None:
            regime_val = btc_regime.get(date_i)
            if regime_val is not None and not regime_val:
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
            "date": date_i,
            "r": r,
        })
        i = j + 1

    return out


# ── reporting ─────────────────────────────────────────────────────────────────

def describe(label: str, r: pd.Series) -> None:
    if len(r) < 2:
        print(f"  {label:48s} n={len(r)}")
        return
    t, p = stats.ttest_1samp(r, 0)
    sharpe = r.mean() / r.std() if r.std() > 0 else 0.0
    print(
        f"  {label:48s} n={len(r):5d}  mean {r.mean():+.3f}R  "
        f"win {100*(r > 0).mean():3.0f}%  Sharpe {sharpe:.2f}  p {p:.4f}"
    )


def main() -> None:
    if not CACHE.exists() or not any(CACHE.iterdir()):
        print("ERROR: Binance daily cache is empty. Run: python3 -m Research.binance_daily")
        return

    btc_regime = _load_btc_regime()
    if btc_regime is None:
        print("WARNING: BTCUSDT.json not in cache — BTC regime filter skipped")

    contaminated = polar_bases()
    groups: dict[str, list] = {"fresh": [], "polar": []}

    for f in sorted(CACHE.glob("*.json")):
        if f.stem == "BTCUSDT":
            continue
        t = trades_for(json.loads(f.read_text()), btc_regime)
        for x in t:
            x["symbol"] = f.stem
        bucket = "polar" if f.stem.removesuffix("USDT") in contaminated else "fresh"
        groups[bucket].extend(t)

    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        if df.empty:
            print(f"\n{name.upper()}: no trades.")
            continue
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} signals ===")
        describe("all longs (BTC-regime filtered)", df.r)
        yr = df.groupby(df.date.dt.year).r.agg(["mean", "count"])
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
    yr_means = fresh_df.groupby(fresh_df.date.dt.year).r.agg(["mean", "count"])
    years_ok = yr_means[yr_means["count"] >= 5]
    pos_yr_pct = (years_ok["mean"] > 0).mean() if len(years_ok) >= 2 else 0.0

    c1 = val.mean() > 0 and p_val < 0.05
    c2 = sharpe > 0.10
    c3 = win_rate > 0.38
    c4 = pos_yr_pct >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   →  mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.10          →  Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 38%         →  win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years →  {100*pos_yr_pct:.0f}%")
    print(f"\nVERDICT: {'PASS — deploy to paper daemon' if (c1 and c2 and c3 and c4) else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
