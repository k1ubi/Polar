"""MACD + RSI momentum strategy — out-of-sample pre-registered test.

Strategy (fixed before the first run):
  signal     MACD histogram crosses zero (neg→pos = long, pos→neg = short)
  filter     RSI(14) must be ABOVE 45 for longs, BELOW 55 for shorts
             at the moment of the cross — avoids entering on a histogram
             flip that is already exhausted (RSI divergence screen)
  trend      price must be above SMA50 for longs, below SMA50 for shorts
             (regime filter: don't fight the medium-term trend)
  stop       1.5 × ATR14 from the entry bar's close (Wilder smoothing)
  target     3.0 × ATR14 from entry (2:1 R/R)
  entry      next bar's open (same convention as vpa_book_breakout.py)
  time stop  20 bars
  costs      0.18% round trip; gap through stop fills at open

Rationale: MACD histogram zero-cross is a classic trend-initiation signal,
but generates many whipsaws in ranging markets. RSI > 45 (for longs) rules
out "dead-cat bounce" entries where momentum is already reversing.
SMA50 prevents counter-trend trades in mean-reverting regimes.
Result is a discretionary-filter approach widely used by practitioners but
rarely tested rigorously on crypto — this test fills that gap.

Data: same Binance daily cache as Research/trend_oos.py.
      Run Research/binance_daily.py first if the cache is empty.

Pre-registered PASS criteria (fresh universe only):
  1. Mean R > 0, one-sample t p < 0.05
  2. Sharpe ratio (mean R / std R) > 0.10
  3. Win rate > 40% (prevents a strategy that wins on tiny tail but loses constantly)
  4. Positive mean R in at least half the calendar years with >= 5 trades

Run: python3 -m Research.macd_rsi_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats

from Research.trend_oos import CACHE, polar_bases

# ── strategy constants ────────────────────────────────────────────────────────
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIG = 9
RSI_PERIOD = 14
RSI_LONG_FLOOR = 45     # long only if RSI > 45 at cross
RSI_SHORT_CEIL = 55     # short only if RSI < 55 at cross
SMA_PERIOD = 50         # trend filter
ATR_PERIOD = 14
STOP_ATR = 1.5
TARGET_ATR = 3.0        # 2:1 R/R
MAX_HOLD = 20           # bars
FEE = 0.0018            # 0.18% round trip
START = pd.Timestamp("2020-01-01")
MIN_BARS = 300          # need enough history for SMA50 + MACD warm-up


# ── indicator helpers ────────────────────────────────────────────────────────

def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    delta = np.diff(close, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / period
    n = len(close)
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    # Seed at period
    for k in range(period, n):
        if np.isnan(avg_gain[k - 1]):
            if k < period:
                continue
            avg_gain[k] = np.mean(gain[1:k + 1])
            avg_loss[k] = np.mean(loss[1:k + 1])
        else:
            avg_gain[k] = alpha * gain[k] + (1 - alpha) * avg_gain[k - 1]
            avg_loss[k] = alpha * loss[k] + (1 - alpha) * avg_loss[k - 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = avg_gain / np.where(avg_loss == 0, np.nan, avg_loss)
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1)
    out = np.full(len(arr), np.nan)
    for i in range(len(arr)):
        if np.isnan(arr[i]):
            continue
        if np.isnan(out[i - 1]) if i > 0 else True:
            out[i] = arr[i]
        else:
            out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _macd_hist(close: np.ndarray) -> np.ndarray:
    ema_fast = _ema(close, MACD_FAST)
    ema_slow = _ema(close, MACD_SLOW)
    macd = ema_fast - ema_slow
    sig = _ema(np.where(np.isnan(macd), np.nan, macd), MACD_SIG)
    return macd - sig


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    alpha = 1.0 / period
    out = np.full(len(tr), np.nan)
    for i in range(len(tr)):
        if np.isnan(tr[i]):
            continue
        if np.isnan(out[i - 1]) if i > 0 else True:
            out[i] = tr[i]
        else:
            out[i] = alpha * tr[i] + (1 - alpha) * out[i - 1]
    return out


# ── trade simulation ─────────────────────────────────────────────────────────

def trades_for(rows: list) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < MIN_BARS:
        return []

    ts, o, h, l, c = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(c)

    hist = _macd_hist(c)
    rsi = _rsi(c, RSI_PERIOD)
    sma50 = pd.Series(c).rolling(SMA_PERIOD).mean().to_numpy()
    atr = _atr(h, l, c, ATR_PERIOD)

    out = []
    i = MACD_SLOW + MACD_SIG + SMA_PERIOD + 10  # warm-up
    while i < n - 1:
        if pd.Timestamp(ts[i], unit="ms") < START:
            i += 1
            continue

        # MACD histogram zero-cross detection
        prev_h = hist[i - 1]
        curr_h = hist[i]
        if np.isnan(prev_h) or np.isnan(curr_h):
            i += 1
            continue
        if np.isnan(rsi[i]) or np.isnan(sma50[i]) or np.isnan(atr[i]) or atr[i] <= 0:
            i += 1
            continue

        long_cross = prev_h < 0 and curr_h > 0
        short_cross = prev_h > 0 and curr_h < 0

        # Apply filters
        if long_cross and rsi[i] > RSI_LONG_FLOOR and c[i] > sma50[i]:
            side = 1
        elif short_cross and rsi[i] < RSI_SHORT_CEIL and c[i] < sma50[i]:
            side = -1
        else:
            i += 1
            continue

        entry = o[i + 1]
        if entry <= 0:
            i += 1
            continue

        stop_dist = STOP_ATR * atr[i]
        target_dist = TARGET_ATR * atr[i]
        stop = entry - side * stop_dist
        target = entry + side * target_dist
        risk = stop_dist  # in price units

        exit_px = None
        j = i + 1
        for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
            if side == 1:
                if o[j] <= stop:
                    exit_px = o[j]
                    break
                if l[j] <= stop:
                    exit_px = stop
                    break
                if h[j] >= target:
                    exit_px = target
                    break
            else:
                if o[j] >= stop:
                    exit_px = o[j]
                    break
                if h[j] >= stop:
                    exit_px = stop
                    break
                if l[j] <= target:
                    exit_px = target
                    break
        if exit_px is None:
            exit_px = c[min(j, n - 1)]

        r = ((exit_px - entry) * side - FEE * entry) / risk
        out.append({
            "date": pd.Timestamp(ts[i], unit="ms"),
            "side": side,
            "r": r,
            "rsi_at_entry": float(rsi[i]),
            "with_trend": bool(c[i] > sma50[i]) == bool(side == 1),
        })
        i = j + 1  # one position per symbol at a time

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
        f"win {100*(r > 0).mean():3.0f}%  Sharpe {sharpe:.2f}  p {p:.3f}"
    )


def main() -> None:
    if not CACHE.exists() or not any(CACHE.iterdir()):
        print("ERROR: Binance daily cache is empty.")
        print("  Run: python3 -m Research.binance_daily")
        return

    contaminated = polar_bases()
    groups: dict[str, list] = {"fresh": [], "polar": []}

    for f in sorted(CACHE.glob("*.json")):
        t = trades_for(json.loads(f.read_text()))
        for x in t:
            x["symbol"] = f.stem
        bucket = "polar" if f.stem.removesuffix("USDT") in contaminated else "fresh"
        groups[bucket].extend(t)

    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        if df.empty:
            print(f"\n{name.upper()}: no trades found.")
            continue
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} signals ===")
        describe("all signals", df.r)
        describe("longs only", df[df.side == 1].r)
        describe("shorts only", df[df.side == -1].r)
        describe("with trend filter (should all be True here)", df[df.with_trend].r)

        yr = df.groupby(df.date.dt.year).r.agg(["mean", "count"])
        print(
            "  by year: "
            + "  ".join(f"{y}:{m:+.2f}R(n{int(k)})" for y, (m, k) in yr.iterrows())
        )

    val = pd.Series([x["r"] for x in groups["fresh"]])
    if val.empty:
        print("\nPRE-REGISTERED VERDICT: FAIL (no fresh trades)")
        return

    t_stat, p_val = stats.ttest_1samp(val, 0)
    sharpe = val.mean() / val.std() if val.std() > 0 else 0.0
    win_rate = (val > 0).mean()

    fresh_df = pd.DataFrame(groups["fresh"])
    yr_means = fresh_df.groupby(fresh_df.date.dt.year).r.agg(["mean", "count"])
    years_with_5 = yr_means[yr_means["count"] >= 5]
    positive_years = (years_with_5["mean"] > 0).mean() if len(years_with_5) >= 2 else 0.0

    c1 = val.mean() > 0 and p_val < 0.05
    c2 = sharpe > 0.10
    c3 = win_rate > 0.40
    c4 = positive_years >= 0.50

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0 and p < 0.05  →  mean={val.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.10           →  Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 40%          →  win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive in ≥50% of years (n≥5) → {100*positive_years:.0f}%")
    all_pass = c1 and c2 and c3 and c4
    print(f"\nVERDICT: {'PASS — deploy to paper daemon' if all_pass else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
