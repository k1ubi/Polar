"""MACD + RSI on 1H bars — out-of-sample pre-registered test.

Motivation: the daily MACD+RSI long-only strategy (macd_rsi_long_oos.py)
nearly passed (Sharpe 0.091 vs threshold 0.10) but failed in 2022 and 2025
because the daily BTC SMA20/65 regime filter let through entries in sharp
bounces within larger bear markets. On 1H bars the signal-to-entry lag is
much shorter (next candle = 1 hour vs 1 day) which means stops get hit or
targets reached in hours rather than days — reducing exposure to
multi-day reversals in bad regimes.

Strategy (fixed before first run):
  timeframe  1H (Binance USDT pairs)
  signal     MACD(12,26,9) histogram crosses zero upward (long only)
  filters:
    1. RSI(14) must be between 45 and 75 at signal bar
    2. Price > SMA50 (50H ≈ 2-day medium-term trend filter)
    3. BTC DAILY regime: BTC daily close > SMA20 daily
  stop       1.5 × ATR14(1H) below entry
  target     2.5 × ATR14(1H) above entry
  entry      next bar's open (no lookahead)
  time stop  30 bars (= 30 hours)
  costs      0.18% round trip

Pre-registered PASS criteria (fresh universe):
  1. Mean R > 0, one-sample t p < 0.05
  2. Sharpe ratio > 0.12
  3. Win rate > 38%
  4. Positive mean R in at least 50% of calendar years with >= 20 trades

Data requirements:
  Run Research/binance_1h_fetch.py first  (caches to Research/data/binance_1h/)
  Run Research/binance_daily.py first     (for BTC daily regime)

Run: python3 -m Research.macd_rsi_1h_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats
from pathlib import Path

from Research.trend_oos import CACHE as BINANCE_DAILY_CACHE, polar_bases

HOURLY_CACHE = Path(__file__).parent / "data" / "binance_1h"
BINANCE_CACHE = BINANCE_DAILY_CACHE

# ── constants (do NOT change after first run) ─────────────────────────────────
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG = 9
RSI_PERIOD = 14
RSI_MIN = 45
RSI_MAX = 75
SMA_PERIOD = 50
BTC_SMA_DAILY = 20
ATR_PERIOD = 14
STOP_ATR = 1.5
TARGET_ATR = 2.5
MAX_HOLD = 30
FEE = 0.0018
START = pd.Timestamp("2022-06-01", tz="UTC")
MIN_BARS = 500


# ── vectorized indicators (pandas ewm — C-implemented, fast) ──────────────────

def _indicators(c: np.ndarray, h: np.ndarray, l: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (macd_hist, rsi, sma50, atr14) as numpy arrays."""
    s = pd.Series(c)

    # MACD histogram
    ema_fast = s.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = s.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    hist = (macd - macd.ewm(span=MACD_SIG, adjust=False).mean()).to_numpy()

    # RSI (Wilder's smoothing = ewm with alpha=1/period)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    al = loss.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)).to_numpy()

    # SMA50
    sma50 = s.rolling(SMA_PERIOD).mean().to_numpy()

    # ATR14 (Wilder)
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    atr = pd.Series(tr).ewm(alpha=1 / ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean().to_numpy()

    return hist, rsi, sma50, atr


# ── BTC daily regime — pre-aligned to hourly dates ────────────────────────────

def _btc_regime_hourly(hourly_dates: pd.DatetimeIndex) -> np.ndarray | None:
    """Boolean array aligned to hourly_dates: True = BTC close > SMA20 on that date."""
    btc_path = BINANCE_CACHE / "BTCUSDT.json"
    if not btc_path.exists():
        return None
    rows = json.loads(btc_path.read_text())
    a = np.array(rows, dtype=float)
    daily_dates = pd.to_datetime(a[:, 0], unit="ms", utc=True).normalize()
    close = pd.Series(a[:, 4].astype(float), index=daily_dates)
    regime = (close > close.rolling(BTC_SMA_DAILY).mean()).reindex(
        hourly_dates.normalize(), method="ffill"
    ).fillna(False).to_numpy(dtype=bool)
    return regime


# ── trade simulation ──────────────────────────────────────────────────────────

def trades_for(rows: list, btc_regime: np.ndarray | None) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts_ms = a[:, 0]
    o, h, l, c = a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(c)

    hist, rsi, sma50, atr = _indicators(c, h, l)

    dates = pd.to_datetime(ts_ms, unit="ms", utc=True)
    warmup = MACD_SLOW + MACD_SIG + max(SMA_PERIOD, BTC_SMA_DAILY * 24) + 5

    # ── vectorised signal detection ───────────────────────────────────────────
    valid = np.zeros(n, dtype=bool)
    start_i = max(warmup, 1)
    # Avoid NaNs in slices
    good = (
        ~np.isnan(hist) & ~np.isnan(rsi) & ~np.isnan(sma50) & ~np.isnan(atr) & (atr > 0)
    )
    zero_cross = np.zeros(n, dtype=bool)
    zero_cross[start_i:] = (hist[start_i - 1:-1] < 0) & (hist[start_i:] > 0)

    valid = (
        good
        & zero_cross
        & (rsi > RSI_MIN) & (rsi < RSI_MAX)
        & (c > sma50)
        & (pd.to_datetime(ts_ms, unit="ms", utc=True) >= START).to_numpy()
    )
    if btc_regime is not None:
        valid &= btc_regime

    signal_indices = np.where(valid)[0]
    if len(signal_indices) == 0:
        return []

    # ── simulate exits only at signal bars ───────────────────────────────────
    out = []
    occupied_until = 0  # no overlapping positions

    for i in signal_indices:
        if i < occupied_until or i + 1 >= n:
            continue

        entry = o[i + 1]
        if entry <= 0:
            continue

        stop_dist = STOP_ATR * atr[i]
        stop = entry - stop_dist
        target = entry + TARGET_ATR * atr[i]

        end = min(i + 1 + MAX_HOLD, n)
        exit_px = c[end - 1]  # time stop default
        exit_bar = end - 1

        o_win = o[i + 1:end]
        l_win = l[i + 1:end]
        h_win = h[i + 1:end]

        # gap through stop
        gap_sl = np.where(o_win <= stop)[0]
        # wick stop
        sl = np.where(l_win <= stop)[0]
        # target
        tp = np.where(h_win >= target)[0]

        first_sl = gap_sl[0] if len(gap_sl) else (sl[0] if len(sl) else MAX_HOLD)
        first_tp = tp[0] if len(tp) else MAX_HOLD

        if first_sl < first_tp:
            j = i + 1 + first_sl
            exit_px = o_win[first_sl] if len(gap_sl) and gap_sl[0] == first_sl else stop
            exit_bar = j
        elif first_tp < MAX_HOLD:
            exit_px = target
            exit_bar = i + 1 + first_tp

        r = (exit_px - entry - FEE * entry) / stop_dist
        out.append({"date": dates[i].year, "r": float(r)})
        occupied_until = exit_bar + 1

    return out


# ── reporting ─────────────────────────────────────────────────────────────────

def describe(label: str, r: pd.Series) -> None:
    if len(r) < 2:
        print(f"  {label:56s} n={len(r)}")
        return
    _, p = stats.ttest_1samp(r, 0)
    sharpe = r.mean() / r.std() if r.std() > 0 else 0.0
    print(
        f"  {label:56s} n={len(r):6d}  mean {r.mean():+.3f}R  "
        f"win {100*(r > 0).mean():3.0f}%  Sharpe {sharpe:.3f}  p {p:.4f}"
    )


def main() -> None:
    if not HOURLY_CACHE.exists() or not any(HOURLY_CACHE.iterdir()):
        print("ERROR: 1H cache is empty.")
        print("  Run first: python3 -m Research.binance_1h_fetch")
        return

    contaminated = polar_bases()
    groups: dict[str, list] = {"fresh": [], "polar": []}

    for f in sorted(HOURLY_CACHE.glob("*.json")):
        rows = json.loads(f.read_text())
        if not rows:
            continue
        ts_ms = np.array([r[0] for r in rows], dtype=float)
        dates = pd.to_datetime(ts_ms, unit="ms", utc=True)
        regime = _btc_regime_hourly(dates)

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
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} signals ===")
        describe("all longs (1H MACD+RSI, BTC SMA20 regime)", df.r)
        yr = df.groupby("date").r.agg(["mean", "count"])
        print("  by year: " + "  ".join(
            f"{y}:{m:+.2f}R(n{int(k)})" for y, (m, k) in yr.iterrows()
        ))

    val = pd.Series([x["r"] for x in groups["fresh"]])
    if val.empty:
        print("\nVERDICT: FAIL (no fresh trades)")
        return

    _, p_val = stats.ttest_1samp(val, 0)
    sharpe = val.mean() / val.std() if val.std() > 0 else 0.0
    win_rate = (val > 0).mean()

    fresh_df = pd.DataFrame(groups["fresh"])
    yr_m = fresh_df.groupby("date").r.agg(["mean", "count"])
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
