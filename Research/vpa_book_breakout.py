"""Coulling, A Complete Guide To Volume Price Analysis (2013), ch. 7 & 11:
congestion-zone breakout validated by volume.

Book rules, translated (fixed before the first run):
  congestion   prior 20 daily bars, range (max high - min low) <= 4 x ATR14,
               and their mean volume below the 50-bar average ("volume well below average")
  breakout     close beyond ceiling/floor by >= 0.25 ATR ("a clear close well above"),
               on a wide-spread candle (high-low >= 1 ATR)
  validation   breakout-bar volume >= 1.5 x 20-bar average ("validated by the volume")
  entry        next bar's open
  stop         opposite side of the congestion zone ("stop below the last pivot low")
  target       measured move: 2 x zone height from the breakout level ("cause and effect")
  time stop    30 bars
  costs        0.18% round trip; gap through the stop fills at the open

Primary question: do VOLUME-VALIDATED breakouts make money after fees, and do they beat
the same breakouts WITHOUT volume validation (the book's central claim)?
PASS needs, on the FRESH universe: validated mean R > 0 with p < 0.05, AND validated
mean R > unvalidated mean R (Welch p < 0.10).
Secondary (reported, not part of PASS): the book's "trade with the dominant trend" filter,
using the slower timeframe's trend (close vs 20-week = 100-day SMA).

Run: python3 -m Research.vpa_book_breakout
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats

from Research.trend_oos import CACHE, polar_bases

ZONE = 20
MAX_ZONE_ATR = 4.0
BREAK_ATR = 0.25
WIDE_ATR = 1.0
VOL_MULT = 1.5
TARGET_MULT = 2.0
MAX_HOLD = 30
FEE = 0.0018
START = pd.Timestamp("2019-01-01")


def trades_for(rows: list) -> list[dict]:
    a = np.array(rows, dtype=float)
    if len(a) < 200:
        return []
    ts, o, h, l, c, v = a.T
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - l, np.abs(h - prev_c), np.abs(l - prev_c)], axis=1)
    atr = pd.Series(tr).rolling(14).mean().to_numpy()
    vavg20 = pd.Series(v).rolling(20).mean().shift(1).to_numpy()
    vavg50 = pd.Series(v).rolling(50).mean().shift(1).to_numpy()
    sma100 = pd.Series(c).rolling(100).mean().to_numpy()
    out = []
    i = 100
    n = len(c)
    while i < n - 1:
        lo, hi = i - ZONE, i
        ceil, floor = h[lo:hi].max(), l[lo:hi].min()
        a_ = atr[i - 1]
        if not (a_ > 0) or v[i] <= 0 or not (vavg50[lo] > 0):
            i += 1
            continue
        congested = (ceil - floor) <= MAX_ZONE_ATR * a_ and v[lo:hi].mean() < vavg50[lo]
        wide = (h[i] - l[i]) >= WIDE_ATR * a_
        side = 0
        if congested and wide and c[i] >= ceil + BREAK_ATR * a_:
            side = 1
        elif congested and wide and c[i] <= floor - BREAK_ATR * a_:
            side = -1
        if side == 0 or pd.Timestamp(ts[i], unit="ms") < START:
            i += 1
            continue
        entry = o[i + 1]
        height = ceil - floor
        stop = floor if side == 1 else ceil
        target = (ceil + TARGET_MULT * height) if side == 1 else (floor - TARGET_MULT * height)
        risk = (entry - stop) * side
        if risk <= 0:
            i += 1
            continue
        exit_px, j = None, i + 1
        for j in range(i + 1, min(i + 1 + MAX_HOLD, n)):
            if side == 1:
                if o[j] <= stop: exit_px = o[j]; break
                if l[j] <= stop: exit_px = stop; break
                if h[j] >= target: exit_px = target; break
            else:
                if o[j] >= stop: exit_px = o[j]; break
                if h[j] >= stop: exit_px = stop; break
                if l[j] <= target: exit_px = target; break
        if exit_px is None:
            exit_px = c[j]
        r = ((exit_px - entry) * side - FEE * entry) / risk
        out.append({
            "date": pd.Timestamp(ts[i], unit="ms"), "side": side, "r": r,
            "validated": bool(v[i] >= VOL_MULT * vavg20[i]),
            "with_trend": bool((c[i] > sma100[i]) == (side == 1)),
            "zone_bars": ZONE, "risk_pct": risk / entry,
        })
        i = j + 1  # one position per symbol at a time
    return out


def describe(label: str, r: pd.Series) -> None:
    if len(r) < 2:
        print(f"  {label:40s} n={len(r)}")
        return
    t, p = stats.ttest_1samp(r, 0)
    print(f"  {label:40s} n={len(r):5d}  mean {r.mean():+.3f}R  median {r.median():+.3f}R  "
          f"win {100*(r > 0).mean():3.0f}%  p {p:.3f}")


def main() -> None:
    contaminated = polar_bases()
    groups = {"fresh": [], "polar": []}
    for f in sorted(CACHE.glob("*.json")):
        t = trades_for(json.loads(f.read_text()))
        for x in t:
            x["symbol"] = f.stem
        groups["polar" if f.stem.removesuffix("USDT") in contaminated else "fresh"] += t
    result = {}
    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} breakouts ===")
        val, unval = df[df.validated].r, df[~df.validated].r
        describe("all breakouts", df.r)
        describe("volume-validated (the book's trade)", val)
        describe("NOT volume-validated", unval)
        w = stats.ttest_ind(val, unval, equal_var=False)
        print(f"  validated minus unvalidated: {val.mean() - unval.mean():+.3f}R  Welch p {w.pvalue:.3f}")
        describe("validated + with dominant trend", df[df.validated & df.with_trend].r)
        describe("validated + against dominant trend", df[df.validated & ~df.with_trend].r)
        describe("validated longs", df[df.validated & (df.side == 1)].r)
        describe("validated shorts", df[df.validated & (df.side == -1)].r)
        yr = df[df.validated].groupby(df[df.validated].date.dt.year).r.agg(["mean", "count"])
        print("  validated by year: " + "  ".join(f"{y}:{m:+.2f}R(n{int(k)})" for y, (m, k) in yr.iterrows()))
        result[name] = (val, unval, w.pvalue)
    val, unval, wp = result["fresh"]
    ok = val.mean() > 0 and stats.ttest_1samp(val, 0).pvalue < 0.05 and val.mean() > unval.mean() and wp < 0.10
    print(f"\nPRE-REGISTERED VERDICT (fresh universe): {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
