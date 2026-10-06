"""52-week high momentum breakout — out-of-sample pre-registered test.

Thesis: when a crypto asset closes at a new 52-week high it has entered price
discovery — overhead supply has been cleared and prior sellers are sitting on
losses. Momentum literature (Jegadeesh & Titman 1993, Liu & Zhang 2008) and
the crypto-specific evidence (Liu, Tsyvinski 2021) both show strong
persistence over 1–6 months. Daily bars with ATR-based stops let the trade
breathe without unlimited downside.

Strategy (fixed before first run):
  timeframe  daily (Binance USDT pairs)
  signal     today's close > max of prior 252 daily closes (new 52-week high)
  filters:
    1. BTC daily close > SMA50 — bull regime only (bear markets turn momentum
       into a mean-reversion trap; seen clearly in 2022 data)
  entry      next bar's open (no look-ahead)
  stop       1.5 × ATR14 below entry
  target     3.0 × ATR14 above entry  (2:1 R:R — momentum needs room to run)
  time stop  20 bars (~1 calendar month)
  costs      0.18% round trip (taker + slippage)
  direction  long only

Pre-registered PASS criteria (fresh universe, fixed before any run):
  1. Mean R > 0, one-sample t p < 0.05
  2. Sharpe ratio > 0.15
  3. Win rate > 40%
  4. Positive mean R in at least 50% of calendar years with ≥ 20 trades

Contamination split: symbols in Research/trend_oos.py::polar_bases() are the
"polar" universe (used during strategy development); everything else is fresh.

Run: python3 -m Research.momentum_52w_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats
from pathlib import Path

from Research.trend_oos import CACHE as DAILY_CACHE, polar_bases

# ── constants (do NOT change after first run) ─────────────────────────────────
HIGH_PERIOD   = 252   # bars for 52-week high
ATR_PERIOD    = 14
STOP_ATR      = 1.5
TARGET_ATR    = 3.0
MAX_HOLD      = 20
BTC_SMA       = 50
FEE           = 0.0018
START         = pd.Timestamp("2020-01-01", tz="UTC")  # need 252-bar warmup
MIN_BARS      = 300


# ── indicators ────────────────────────────────────────────────────────────────

def _atr14(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    return (
        pd.Series(tr)
        .ewm(alpha=1 / ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False)
        .mean()
        .to_numpy()
    )


# ── BTC regime ────────────────────────────────────────────────────────────────

def _btc_regime(dates: pd.DatetimeIndex) -> np.ndarray | None:
    btc_path = DAILY_CACHE / "BTCUSDT.json"
    if not btc_path.exists():
        return None
    rows = json.loads(btc_path.read_text())
    a = np.array(rows, dtype=float)
    daily_dates = pd.to_datetime(a[:, 0], unit="ms", utc=True).normalize()
    close = pd.Series(a[:, 4].astype(float), index=daily_dates)
    sma = close.rolling(BTC_SMA).mean()
    regime = (close > sma).reindex(dates.normalize(), method="ffill").fillna(False)
    return regime.to_numpy(dtype=bool)


# ── trade simulation ──────────────────────────────────────────────────────────

def trades_for(rows: list, btc_regime: np.ndarray | None) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts_ms = a[:, 0]
    o, h, l, c = a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(c)

    atr = _atr14(h, l, c)
    s = pd.Series(c)

    # Rolling 252-bar max of PRIOR closes (shift by 1 to avoid lookahead)
    high_252 = s.rolling(HIGH_PERIOD).max().shift(1).to_numpy()

    dates = pd.to_datetime(ts_ms, unit="ms", utc=True)

    # ── vectorised signal detection ───────────────────────────────────────────
    good = ~np.isnan(high_252) & ~np.isnan(atr) & (atr > 0) & (high_252 > 0)
    new_high = (c > high_252) & good
    after_start = dates >= START

    valid = new_high & after_start
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
        stop   = entry - stop_dist
        target = entry + TARGET_ATR * atr[i]

        end = min(i + 1 + MAX_HOLD, n)
        o_win = o[i + 1:end]
        l_win = l[i + 1:end]
        h_win = h[i + 1:end]

        gap_sl = np.where(o_win <= stop)[0]
        sl     = np.where(l_win <= stop)[0]
        tp     = np.where(h_win >= target)[0]

        first_sl = gap_sl[0] if len(gap_sl) else (sl[0] if len(sl) else MAX_HOLD)
        first_tp = tp[0] if len(tp) else MAX_HOLD

        if first_sl < first_tp:
            exit_px = o_win[first_sl] if len(gap_sl) and gap_sl[0] == first_sl else stop
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
        ts_ms = np.array([r[0] for r in rows], dtype=float)
        dates = pd.to_datetime(ts_ms, unit="ms", utc=True)
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
        syms = df.symbol.nunique() if "symbol" in df.columns else "?"
        print(f"\n=== {name.upper()} universe: {syms} symbols, {len(df)} trades ===")
        describe("52-week high breakout (long, BTC SMA50 regime)", df.r)
        yr = df.groupby("date").r.agg(["mean", "count"])
        print("  by year: " + "  ".join(
            f"{y}:{m:+.2f}R(n{int(k)})" for y, (m, k) in yr.iterrows()
        ))

    val = pd.Series([x["r"] for x in groups["fresh"]])
    if val.empty:
        print("\nVERDICT: FAIL (no fresh trades)")
        return

    _, p_val = stats.ttest_1samp(val, 0)
    sharpe   = val.mean() / val.std() if val.std() > 0 else 0.0
    win_rate = (val > 0).mean()

    fresh_df = pd.DataFrame(groups["fresh"])
    yr_m     = fresh_df.groupby("date").r.agg(["mean", "count"])
    years_ok = yr_m[yr_m["count"] >= 20]
    pos_yr   = (years_ok["mean"] > 0).mean() if len(years_ok) >= 2 else 0.0

    c1 = val.mean() > 0 and p_val < 0.05
    c2 = sharpe > 0.15
    c3 = win_rate > 0.40
    c4 = pos_yr >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   → mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.15          → Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 40%         → win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years → {100*pos_yr:.0f}%")
    print(f"\nPRE-REGISTERED VERDICT: {'PASS — add to paper daemon' if all([c1,c2,c3,c4]) else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
