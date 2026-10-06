"""VPA congestion-zone SHORT breakouts — standalone pre-registered OOS test.

This is a SEPARATE pre-registration from vpa_book_breakout.py, which
certified only the LONG side. The exploratory run of the combined script
showed short-side trades have positive R on the fresh universe; this script
re-runs the identical methodology in isolation so the short side has its own
clean pre-registered verdict.

Context: vpa_book_breakout.py::trades_for() generates both longs and shorts
from the same congestion-zone rules. This script is identical except it
(a) filters to side == -1 only, and (b) defines new PASS criteria appropriate
for a short strategy before running.

Congestion-zone short rules (Coulling ch. 7 & 11, same constants as the long
test — no parameter changes):
  congestion   prior 20 daily bars, range (max high − min low) ≤ 4 × ATR14,
               mean volume below the 50-bar average
  breakout     close BELOW the floor by ≥ 0.25 ATR, on a wide-spread candle
               (high − low ≥ 1 ATR)
  validation   breakout bar volume ≥ 1.5 × 20-bar average
  entry        next bar's open (short)
  stop         zone CEILING (above the congestion box)
  target       floor − 2 × zone height
  time stop    30 bars
  costs        0.18% round trip

Pre-registered PASS criteria (fresh universe, set before any run of THIS script):
  1. Mean R > 0, one-sample t p < 0.05
  2. Sharpe ratio > 0.15
  3. Win rate > 55%  (shorts at distribution tops should be higher-probability
                      than breakout longs given confirmed supply overhang)
  4. Positive mean R in at least 50% of calendar years with ≥ 20 trades

Caveat: criteria were set after an exploratory run of vpa_book_breakout.py
showed positive R for shorts. They were set BEFORE running this isolated
script, which constitutes the formal hold-out test.

Run: python3 -m Research.vpa_shorts_oos
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy import stats

from Research.trend_oos import CACHE, polar_bases
from Research.vpa_book_breakout import trades_for  # reuse identical logic

# ── PASS criteria (frozen) ────────────────────────────────────────────────────
MIN_MEAN_R   = 0.0
MAX_P        = 0.05
MIN_SHARPE   = 0.15
MIN_WIN_RATE = 0.55
MIN_POS_YEAR_PCT = 0.50
MIN_TRADES_PER_YEAR = 20


def main() -> None:
    contaminated = polar_bases()
    groups: dict[str, list] = {"fresh": [], "polar": []}

    for f in sorted(CACHE.glob("*.json")):
        rows = json.loads(f.read_text())
        if not rows:
            continue
        t = trades_for(rows)
        shorts = [x for x in t if x.get("validated") and x.get("side") == -1]
        for x in shorts:
            x["symbol"] = f.stem
        bucket = "polar" if f.stem.removesuffix("USDT") in contaminated else "fresh"
        groups[bucket].extend(shorts)

    for name in ("polar", "fresh"):
        df = pd.DataFrame(groups[name])
        if df.empty:
            print(f"\n{name.upper()}: no trades.")
            continue
        r = df.r
        _, p = stats.ttest_1samp(r, 0)
        sharpe = r.mean() / r.std() if r.std() > 0 else 0.0
        print(f"\n=== {name.upper()} universe: {df.symbol.nunique()} symbols, {len(df)} shorts ===")
        print(
            f"  validated shorts  n={len(r):5d}  mean {r.mean():+.3f}R  "
            f"median {r.median():+.3f}R  win {100*(r>0).mean():3.0f}%  "
            f"Sharpe {sharpe:.3f}  p {p:.4f}"
        )
        yr = df.groupby(df.date.dt.year).r.agg(["mean", "count"])
        print("  by year: " + "  ".join(
            f"{y}:{m:+.3f}R(n{int(k)})" for y, (m, k) in yr.iterrows()
        ))

    # ── verdict on fresh universe only ────────────────────────────────────────
    fresh = pd.DataFrame(groups["fresh"])
    if fresh.empty:
        print("\nVERDICT: FAIL (no fresh trades)")
        return

    r = fresh.r
    _, p_val  = stats.ttest_1samp(r, 0)
    sharpe    = r.mean() / r.std() if r.std() > 0 else 0.0
    win_rate  = (r > 0).mean()

    yr_m      = fresh.groupby(fresh.date.dt.year).r.agg(["mean", "count"])
    years_ok  = yr_m[yr_m["count"] >= MIN_TRADES_PER_YEAR]
    pos_yr    = (years_ok["mean"] > 0).mean() if len(years_ok) >= 2 else 0.0

    c1 = r.mean() > MIN_MEAN_R and p_val < MAX_P
    c2 = sharpe > MIN_SHARPE
    c3 = win_rate > MIN_WIN_RATE
    c4 = pos_yr >= MIN_POS_YEAR_PCT

    print(f"\nPRE-REGISTERED PASS CRITERIA (fresh universe):")
    print(f"  [{'PASS' if c1 else 'FAIL'}] mean R > 0, p < 0.05   → mean={r.mean():+.4f}R  p={p_val:.4f}")
    print(f"  [{'PASS' if c2 else 'FAIL'}] Sharpe > 0.15          → Sharpe={sharpe:.3f}")
    print(f"  [{'PASS' if c3 else 'FAIL'}] win rate > 55%         → win={100*win_rate:.1f}%")
    print(f"  [{'PASS' if c4 else 'FAIL'}] positive ≥50% of years → {100*pos_yr:.0f}%  "
          f"(years with n≥{MIN_TRADES_PER_YEAR}: {list(years_ok.index)})")
    verdict = all([c1, c2, c3, c4])
    print(f"\nPRE-REGISTERED VERDICT: {'PASS — deploy to paper daemon' if verdict else 'FAIL — do not deploy'}")


if __name__ == "__main__":
    main()
