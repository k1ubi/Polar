"""RSI(2) mean reversion — out-of-sample pre-registered test.

Thesis: short-term RSI(2) measures micro-exhaustion of buyers/sellers.
When an asset is in a confirmed uptrend (price > SMA200) but RSI(2) drops
below 10, it has been aggressively sold in the short term while the larger
trend remains intact — a temporary dislocation with a high probability of
mean-reverting within days. Larry Connors documented this in equities;
crypto's higher volatility makes the dips deeper and recoveries sharper.

Strategy (fixed before first run):
  timeframe  daily (Binance USDT pairs)
  signal     RSI(2) < 10  AND  close > SMA(200) (uptrend filter)
  entry      next bar's open
  stop       1.5 × ATR14 below entry  (hard floor — mean reversion gone wrong)
  exit       RSI(2) crosses above 65  (reversion complete)
             OR stop hit
             OR 10-bar time stop
  costs      0.18% round trip
  direction  long only

Pre-registered PASS criteria (fresh universe, fixed before any run):
  1. Mean R > 0, t-test p < 0.05
  2. Sharpe ratio > 0.15
  3. Win rate > 50%  (mean reversion should win more often than momentum)
  4. Positive mean R in at least 50% of calendar years with ≥ 20 trades

Run: python3 -m Research.rsi2_mean_reversion_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats
from pathlib import Path

from Research.trend_oos import CACHE as DAILY_CACHE, polar_bases

# ── constants (do NOT change after first run) ─────────────────────────────────
RSI_PERIOD   = 2
RSI_ENTRY    = 10    # enter when RSI(2) < this
RSI_EXIT     = 65    # exit when RSI(2) crosses above this
SMA_PERIOD   = 200   # uptrend filter
ATR_PERIOD   = 14
STOP_ATR     = 1.5
MAX_HOLD     = 10
FEE          = 0.0018
START        = pd.Timestamp("2019-01-01", tz="UTC")  # need 200-bar SMA warmup
MIN_BARS     = 250


# ── indicators ────────────────────────────────────────────────────────────────

def _indicators(c: np.ndarray, h: np.ndarray, l: np.ndarray):
    s = pd.Series(c)

    # RSI(2) with Wilder's smoothing
    delta = s.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rsi2  = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).to_numpy()

    # SMA200
    sma200 = s.rolling(SMA_PERIOD).mean().to_numpy()

    # ATR14 Wilder
    prev_c = np.r_[np.nan, c[:-1]]
    tr  = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    atr = pd.Series(tr).ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean().to_numpy()

    return rsi2, sma200, atr


# ── trade simulation ──────────────────────────────────────────────────────────

def trades_for(rows: list) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts_ms = a[:, 0]
    o, h, l, c = a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(c)

    rsi2, sma200, atr = _indicators(c, h, l)
    dates = pd.to_datetime(ts_ms, unit="ms", utc=True)

    # ── vectorised signal detection ───────────────────────────────────────────
    good = ~np.isnan(rsi2) & ~np.isnan(sma200) & ~np.isnan(atr) & (atr > 0)
    oversold   = rsi2 < RSI_ENTRY
    in_uptrend = c > sma200
    after_start = dates >= START

    valid = good & oversold & in_uptrend & after_start
    signal_indices = np.where(valid)[0]
    if len(signal_indices) == 0:
        return []

    # ── simulate exits ────────────────────────────────────────────────────────
    out = []
    occupied_until = 0

    for i in signal_indices:
        if i < occupied_until or i + 1 >= n:
            continue

        entry = o[i + 1]
        if entry <= 0:
            continue

        stop_dist = STOP_ATR * atr[i]
        stop = entry - stop_dist

        end = min(i + 1 + MAX_HOLD, n)
        exit_px  = c[end - 1]
        exit_bar = end - 1

        for j in range(i + 1, end):
            # Stop hit
            if l[j] <= stop:
                exit_px  = o[j] if o[j] <= stop else stop
                exit_bar = j
                break
            # RSI(2) crosses above exit threshold
            if not np.isnan(rsi2[j]) and rsi2[j] > RSI_EXIT:
                exit_px  = c[j]
                exit_bar = j
                break

        r = (exit_px - entry - FEE * entry) / stop_dist
        out.append({"date": dates[i].year, "r": float(r)})
        occupied_until = exit_bar + 1

    return out


# ── reporting ─────────────────────────────────────────────────────────────────

def describe(label: str, r: pd.Series) -> None:
    if len(r) < 2:
        print(f"  {label:60s} n={len(r)}")
        return
    _, p = stats.ttest_1samp(r, 0)
    sharpe = r.mean() / r.std() if r.std() > 0 else 0.0
    print(
        f"  {label:60s} n={len(r):5d}  mean {r.mean():+.3f}R  "
        f"win {100*(r>0).mean():3.0f}%  Sharpe {sharpe:.3f}  p {p:.4f}"
    )


def main() -> None:
    contaminated = polar_bases()
    groups: dict[str, list] = {"fresh": [], "polar": []}

    files = sorted(DAILY_CACHE.glob("*.json"))
    print(f"Scanning {len(files)} symbols...")

    for f in files:
        rows = json.loads(f.read_text())
        if not rows:
            continue
        t = trades_for(rows)
        for x in t:
            x["symbol"] = f.stem
        bucket = "polar" if f.stem.removesuffix("USDT") in contaminated else "fresh"
        groups[bucket].extend(t)

    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        if df.empty:
            print(f"\n{name.upper()}: no trades.")
            continue
        syms = df.symbol.nunique()
        print(f"\n=== {name.upper()} universe: {syms} symbols, {len(df)} trades ===")
        describe("RSI(2)<10 dip in uptrend (close>SMA200)", df.r)
        yr = df.groupby("date").r.agg(["mean", "count"])
        print("  by year: " + "  ".join(
            f"{y}:{m:+.2f}R(n{int(k)})" for y, (m, k) in yr.iterrows()
        ))

    val = pd.Series([x["r"] for x in groups["fresh"]])
    if val.empty:
        print("\nVERDICT: FAIL (no fresh trades)")
        return

    _, p_val  = stats.ttest_1samp(val, 0)
    sharpe    = val.mean() / val.std() if val.std() > 0 else 0.0
    win_rate  = (val > 0).mean()

    fresh_df  = pd.DataFrame(groups["fresh"])
    yr_m      = fresh_df.groupby("date").r.agg(["mean", "count"])
    years_ok  = yr_m[yr_m["count"] >= 20]
    pos_yr    = (years_ok["mean"] > 0).mean() if len(years_ok) >= 2 else 0.0

    c1 = val.mean() > 0 and p_val < 0.05
    c2 = sharpe > 0.15
    c3 = win_rate > 0.50
    c4 = pos_yr >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   → mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.15          → Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 50%         → win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years → {100*pos_yr:.0f}%")
    print(f"\nPRE-REGISTERED VERDICT: {'PASS — add to paper daemon' if all([c1,c2,c3,c4]) else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
