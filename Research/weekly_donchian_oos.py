"""Weekly Donchian + volume breakout — OOS pre-registered test.

Thesis: the daily Donchian+volume strategy (donchian_vol_oos.py) showed
genuine edge (+0.088R, p=0.000) but Sharpe 0.061 is too noisy for deployment.
The noise sources are: (1) 2022 bear market passed the BTC SMA50 filter,
(2) too many marginal daily signals in 2024-2026 altcoin winter. Resampling
to weekly bars reduces signal frequency by ~5x, keeping only weeks where the
breakout AND volume condition both held across 5 daily bars. Switching to BTC
SMA200 (vs SMA50) eliminates most bear-market exposure.

Strategy (fixed before first run):
  timeframe  weekly (daily Binance data resampled to Mon-Sun week)
  signal     weekly close > max of prior 26 weekly closes (6-month high)
             AND weekly volume ≥ 1.5 × 26-week average volume
  filters:
    1. BTC weekly close > 20-week SMA (BTC SMA200 daily ≈ SMA40 weekly)
  entry      next week's open (Monday open of the bar after signal)
  stop       1.5 × ATR(10-week) below entry
  target     2.5 × ATR(10-week) above entry
  time stop  8 weeks (~2 months)
  costs      0.18% round trip
  direction  long only

Pre-registered PASS criteria (fresh universe, fixed before any run):
  1. Mean R > 0, t-test p < 0.05
  2. Sharpe ratio > 0.15
  3. Win rate > 42%
  4. Positive mean R in at least 50% of calendar years with ≥ 10 trades

Run: python3 -m Research.weekly_donchian_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats
from pathlib import Path

from Research.trend_oos import CACHE as DAILY_CACHE, polar_bases

# ── constants (do NOT change after first run) ─────────────────────────────────
DONCHIAN_PERIOD  = 26   # weeks (6-month high)
VOL_PERIOD       = 26
VOL_MULT         = 1.5
ATR_PERIOD       = 10   # weeks
BTC_SMA_W        = 40   # weekly ≈ BTC SMA200 daily
STOP_ATR         = 1.5
TARGET_ATR       = 2.5
MAX_HOLD         = 8    # weeks
FEE              = 0.0018
START            = pd.Timestamp("2019-01-01", tz="UTC")
MIN_WEEKS        = 80


def _resample_weekly(rows: list) -> pd.DataFrame:
    """Resample daily OHLCV rows to weekly (ISO week, Mon-Sun)."""
    a = np.array(rows, dtype=float)
    df = pd.DataFrame({
        "ts":   pd.to_datetime(a[:, 0], unit="ms", utc=True),
        "open": a[:, 1], "high": a[:, 2], "low": a[:, 3],
        "close": a[:, 4], "vol":  a[:, 5],
    }).set_index("ts")
    # Use ISO week (W-SUN: week ends Sunday; label = Sunday)
    w = df.resample("W").agg({
        "open":  "first",
        "high":  "max",
        "low":   "min",
        "close": "last",
        "vol":   "sum",
    }).dropna()
    return w


def _btc_regime_weekly(weekly_idx: pd.DatetimeIndex) -> np.ndarray | None:
    btc_path = DAILY_CACHE / "BTCUSDT.json"
    if not btc_path.exists():
        return None
    rows = json.loads(btc_path.read_text())
    btc_w = _resample_weekly(rows)
    sma   = btc_w["close"].rolling(BTC_SMA_W).mean()
    regime = (btc_w["close"] > sma).reindex(weekly_idx, method="ffill").fillna(False)
    return regime.to_numpy(dtype=bool)


def trades_for(rows: list, btc_regime: np.ndarray | None) -> list[dict]:
    w = _resample_weekly(rows)
    if len(w) < MIN_WEEKS:
        return []

    o = w["open"].to_numpy()
    h = w["high"].to_numpy()
    l = w["low"].to_numpy()
    c = w["close"].to_numpy()
    v = w["vol"].to_numpy()
    dates = w.index
    n = len(c)

    s  = pd.Series(c)
    sv = pd.Series(v)

    don_high = s.rolling(DONCHIAN_PERIOD).max().shift(1).to_numpy()
    vol_avg  = sv.rolling(VOL_PERIOD).mean().shift(1).to_numpy()

    prev_c = np.r_[np.nan, c[:-1]]
    tr  = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    atr = pd.Series(tr).ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean().to_numpy()

    good        = ~np.isnan(don_high) & ~np.isnan(vol_avg) & ~np.isnan(atr) & (atr > 0) & (vol_avg > 0)
    don_break   = c > don_high
    vol_confirm = v >= VOL_MULT * vol_avg
    after_start = dates >= START

    valid = good & don_break & vol_confirm & after_start
    if btc_regime is not None:
        valid = valid & btc_regime

    signal_indices = np.where(valid)[0]
    if len(signal_indices) == 0:
        return []

    out = []
    occupied_until = 0

    for i in signal_indices:
        if i < occupied_until or i + 1 >= n:
            continue

        entry = o[i + 1]
        if entry <= 0:
            continue

        stop_dist = STOP_ATR * atr[i]
        stop      = entry - stop_dist
        target    = entry + TARGET_ATR * atr[i]
        end       = min(i + 1 + MAX_HOLD, n)

        o_win = o[i + 1:end]
        l_win = l[i + 1:end]
        h_win = h[i + 1:end]

        gap_sl = np.where(o_win <= stop)[0]
        sl     = np.where(l_win <= stop)[0]
        tp     = np.where(h_win >= target)[0]

        first_sl = gap_sl[0] if len(gap_sl) else (sl[0] if len(sl) else MAX_HOLD)
        first_tp = tp[0] if len(tp) else MAX_HOLD

        if first_sl < first_tp:
            exit_px  = o_win[first_sl] if len(gap_sl) and gap_sl[0] == first_sl else stop
            exit_bar = i + 1 + first_sl
        elif first_tp < MAX_HOLD:
            exit_px  = target
            exit_bar = i + 1 + first_tp
        else:
            exit_px  = c[end - 1]
            exit_bar = end - 1

        r = (exit_px - entry - FEE * entry) / stop_dist
        out.append({"date": dates[i].year, "r": float(r)})
        occupied_until = exit_bar + 1

    return out


def describe(label: str, r: pd.Series) -> None:
    if len(r) < 2:
        print(f"  {label:62s} n={len(r)}")
        return
    _, p = stats.ttest_1samp(r, 0)
    sharpe = r.mean() / r.std() if r.std() > 0 else 0.0
    print(
        f"  {label:62s} n={len(r):5d}  mean {r.mean():+.3f}R  "
        f"win {100*(r>0).mean():3.0f}%  Sharpe {sharpe:.3f}  p {p:.4f}"
    )


def main() -> None:
    contaminated = polar_bases()
    groups: dict[str, list] = {"fresh": [], "polar": []}

    files = sorted(DAILY_CACHE.glob("*.json"))
    print(f"Scanning {len(files)} symbols (resampling to weekly)...")

    for f in files:
        rows = json.loads(f.read_text())
        if not rows:
            continue
        w      = _resample_weekly(rows)
        regime = _btc_regime_weekly(w.index)
        t      = trades_for(rows, regime)
        for x in t:
            x["symbol"] = f.stem
        bucket = "polar" if f.stem.removesuffix("USDT") in contaminated else "fresh"
        groups[bucket].extend(t)

    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        if df.empty:
            print(f"\n{name.upper()}: no trades.")
            continue
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} trades ===")
        describe("Weekly Donchian 26W break + vol≥1.5x (BTC SMA40W)", df.r)
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
    years_ok  = yr_m[yr_m["count"] >= 10]
    pos_yr    = (years_ok["mean"] > 0).mean() if len(years_ok) >= 2 else 0.0

    c1 = val.mean() > 0 and p_val < 0.05
    c2 = sharpe > 0.15
    c3 = win_rate > 0.42
    c4 = pos_yr >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   → mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.15          → Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 42%         → win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years → {100*pos_yr:.0f}%")
    print(f"\nPRE-REGISTERED VERDICT: {'PASS — add to paper daemon' if all([c1,c2,c3,c4]) else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
