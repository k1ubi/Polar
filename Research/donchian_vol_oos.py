"""Donchian 20-day breakout with volume confirmation — OOS pre-registered test.

Thesis: the classic turtle-system Donchian breakout fires too often on
low-volume moves that immediately reverse. VPA's core finding — that volume
validates breakouts — suggests a hybrid: take the Donchian 20-day signal only
when the breakout bar's volume exceeds the recent average. This should
eliminate the distribution-phase false breakouts that killed RSI(2) and 52W
momentum in 2024-2025.

Strategy (fixed before first run):
  timeframe  daily (Binance USDT pairs)
  signal     close > max of prior 20 closes (Donchian 20-day high)
             AND volume ≥ 1.5 × 20-bar average volume
  filters:
    1. BTC close > SMA50 (regime — same filter as VPA)
  entry      next bar's open
  stop       1.5 × ATR14 below entry
  target     2.5 × ATR14 above entry
  time stop  20 bars
  costs      0.18% round trip
  direction  long only

Pre-registered PASS criteria (fresh universe, fixed before any run):
  1. Mean R > 0, t-test p < 0.05
  2. Sharpe ratio > 0.12
  3. Win rate > 42%
  4. Positive mean R in at least 50% of calendar years with ≥ 20 trades

Run: python3 -m Research.donchian_vol_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats
from pathlib import Path

from Research.trend_oos import CACHE as DAILY_CACHE, polar_bases

# ── constants (do NOT change after first run) ─────────────────────────────────
DONCHIAN_PERIOD  = 20
VOL_PERIOD       = 20
VOL_MULT         = 1.5
ATR_PERIOD       = 14
BTC_SMA          = 50
STOP_ATR         = 1.5
TARGET_ATR       = 2.5
MAX_HOLD         = 20
FEE              = 0.0018
START            = pd.Timestamp("2019-01-01", tz="UTC")
MIN_BARS         = 100


# ── BTC regime ────────────────────────────────────────────────────────────────

def _btc_regime(dates: pd.DatetimeIndex) -> np.ndarray | None:
    btc_path = DAILY_CACHE / "BTCUSDT.json"
    if not btc_path.exists():
        return None
    rows = json.loads(btc_path.read_text())
    a = np.array(rows, dtype=float)
    daily_dates = pd.to_datetime(a[:, 0], unit="ms", utc=True).normalize()
    close = pd.Series(a[:, 4].astype(float), index=daily_dates)
    sma   = close.rolling(BTC_SMA).mean()
    regime = (close > sma).reindex(dates.normalize(), method="ffill").fillna(False)
    return regime.to_numpy(dtype=bool)


# ── trade simulation ──────────────────────────────────────────────────────────

def trades_for(rows: list, btc_regime: np.ndarray | None) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts_ms = a[:, 0]
    o, h, l, c, v = a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
    n = len(c)

    s = pd.Series(c)
    sv = pd.Series(v)

    # Donchian: prior 20-bar high (shift 1 to exclude current bar)
    don_high = s.rolling(DONCHIAN_PERIOD).max().shift(1).to_numpy()
    # Volume: 20-bar average (shift 1)
    vol_avg  = sv.rolling(VOL_PERIOD).mean().shift(1).to_numpy()
    # ATR14
    prev_c = np.r_[np.nan, c[:-1]]
    tr  = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    atr = pd.Series(tr).ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean().to_numpy()

    dates = pd.to_datetime(ts_ms, unit="ms", utc=True)

    # ── vectorised signal detection ───────────────────────────────────────────
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
        ts_ms  = np.array([r[0] for r in rows], dtype=float)
        dates  = pd.to_datetime(ts_ms, unit="ms", utc=True)
        regime = _btc_regime(dates)

        t = trades_for(rows, regime)
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
        describe("Donchian 20D break + vol≥1.5x avg (BTC SMA50 regime)", df.r)
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
    c2 = sharpe > 0.12
    c3 = win_rate > 0.42
    c4 = pos_yr >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   → mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.12          → Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 42%         → win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years → {100*pos_yr:.0f}%")
    print(f"\nPRE-REGISTERED VERDICT: {'PASS — add to paper daemon' if all([c1,c2,c3,c4]) else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
