"""Out-of-sample test of trend_paper's time-series momentum signal.

Design copied from trend_paper / trend_following_followup.py (Polar Protocol, rev. 2026-09-12):
sign of trailing 365-day return, monthly rebalance, 1/vol90 weights normalised to mean 1,
fees charged only when a leg's signal changes. Two changes, both declared up front:
  * Periods are strictly NON-overlapping (rebalance on the 1st, hold to the next 1st),
    which is the "coarser sampling" the Protocol named as the honest next step.
  * The test universe is every Binance USDT pair Polar has NEVER held in any universe
    cache (delisted pairs included). The original study only ever saw Polar's own universe.

Pre-registered verdict, fixed before the first run:
  PASS  if, on the FRESH universe: mean monthly return > 0, Newey-West p < 0.10,
        beats >= 95% of 1000 random-sign draws, and is positive in at least half the
        calendar years with >= 6 periods.
  Anything else is reported as FAIL. The Polar-universe run is shown for reference only.

Run: python3 -m Research.trend_oos
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).parent.parent
CACHE = Path(__file__).parent / "data" / "binance_daily"

FORMATION_DAYS = 365
VOL_LOOKBACK_DAYS = 90
MIN_HISTORY_DAYS = FORMATION_DAYS + VOL_LOOKBACK_DAYS
MIN_MEDIAN_QUOTE_VOL_USD = 2_000_000
FEE_ROUND_TRIP = 0.0018  # daemon's own real-fill assumption, not the optimistic 0.12%
NULL_DRAWS = 1000
SEED = 7


def polar_bases() -> set[str]:
    bases: set[str] = set()
    for f in ROOT.glob("Core/*/data/universe_cache.json"):
        data = json.loads(f.read_text())
        syms = data.get("symbols", data) if isinstance(data, dict) else data
        for s in syms:
            bases.add(str(s).removesuffix("USDT"))
    return bases


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    closes, qvols = {}, {}
    for f in CACHE.glob("*.json"):
        rows = json.loads(f.read_text())
        if len(rows) < MIN_HISTORY_DAYS:
            continue
        idx = pd.to_datetime([r[0] for r in rows], unit="ms")
        closes[f.stem] = pd.Series([r[4] for r in rows], index=idx)
        qvols[f.stem] = pd.Series([r[5] for r in rows], index=idx)
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(qvols).sort_index()


def run(close: pd.DataFrame, qvol: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """One row per (rebalance date, symbol) leg with sign, weight and next-period return."""
    close, qvol = close[symbols], qvol[symbols]
    logret = np.log(close).diff()
    dates = close.index[(close.index.day == 1)]
    legs = []
    prev_sign: dict[str, float] = {}
    for t0, t1 in zip(dates[:-1], dates[1:]):
        past = close.loc[:t0]
        if len(past) <= FORMATION_DAYS:
            continue
        p0 = past.iloc[-1]
        p_form = past.iloc[-1 - FORMATION_DAYS]
        vol = logret.loc[:t0].iloc[-VOL_LOOKBACK_DAYS:].std()
        liq = qvol.loc[:t0].iloc[-30:].median()
        hist = past.notna().sum()
        ok = (hist > MIN_HISTORY_DAYS) & p0.notna() & p_form.notna() & (vol > 0) & (liq >= MIN_MEDIAN_QUOTE_VOL_USD)
        elig = ok[ok].index
        if len(elig) < 5:
            prev_sign = {}
            continue
        # exit price: close at t1, or the last close before t1 if the pair was delisted mid-period
        window = close.loc[t0:t1, elig]
        p1 = window.ffill().iloc[-1]
        sign = np.sign(p0[elig] / p_form[elig] - 1)
        w = 1 / vol[elig]
        w = w / w.mean()
        for s in elig:
            fee = FEE_ROUND_TRIP if prev_sign.get(s) != sign[s] else 0.0
            legs.append((t0, s, sign[s], w[s], p1[s] / p0[s] - 1, fee))
        prev_sign = {s: sign[s] for s in elig}
    return pd.DataFrame(legs, columns=["date", "symbol", "sign", "w", "ret", "fee"])


def portfolio(legs: pd.DataFrame, signs: np.ndarray | None = None) -> pd.Series:
    s = legs["sign"].to_numpy() if signs is None else signs
    pnl = legs["w"].to_numpy() * (s * legs["ret"].to_numpy() - legs["fee"].to_numpy())
    return pd.Series(pnl, index=legs["date"]).groupby(level=0).mean()


def long_run_variance(x: pd.Series, lags: int = 2) -> float:
    """Newey-West (Bartlett kernel) long-run variance."""
    x = x.to_numpy() - x.mean()
    n = len(x)
    return (x @ x) / n + 2 * sum((1 - k / (lags + 1)) * (x[k:] @ x[:-k]) / n for k in range(1, lags + 1))


def summarize(name: str, legs: pd.DataFrame) -> dict:
    port = portfolio(legs)
    n = len(port)
    lrv = long_run_variance(port)
    t_hac = port.mean() / np.sqrt(lrv / n)
    p_hac = 2 * (1 - stats.t.cdf(abs(t_hac), n - 1))
    rng = np.random.default_rng(SEED)
    null_means = np.array([
        portfolio(legs, rng.choice([-1.0, 1.0], size=len(legs))).mean() for _ in range(NULL_DRAWS)
    ])
    beat = (port.mean() > null_means).mean()
    yearly = port.groupby(port.index.year).agg(["mean", "count"])
    yearly_ok = yearly[yearly["count"] >= 6]
    pos_years = int((yearly_ok["mean"] > 0).sum())
    long_leg = portfolio(legs[legs.sign > 0]).mean() if (legs.sign > 0).any() else float("nan")
    short_leg = portfolio(legs[legs.sign < 0]).mean() if (legs.sign < 0).any() else float("nan")
    verdict = (
        port.mean() > 0 and p_hac < 0.10 and beat >= 0.95 and pos_years * 2 >= len(yearly_ok)
    )
    print(f"\n=== {name} ===")
    print(f"symbols used {legs.symbol.nunique()}, legs {len(legs)}, months {n} "
          f"({port.index.min():%Y-%m} -> {port.index.max():%Y-%m}), median legs/month {legs.groupby('date').size().median():.0f}")
    print(f"mean/month {100*port.mean():+.2f}%  median {100*port.median():+.2f}%  "
          f"t(HAC) {t_hac:+.2f}  p(HAC) {p_hac:.3f}  beats random-sign {100*beat:.1f}%")
    print(f"long legs {100*long_leg:+.2f}%/mo   short legs {100*short_leg:+.2f}%/mo   "
          f"share short {100*(legs.sign<0).mean():.0f}%")
    cum = (1 + port).cumprod()
    print(f"compounded x{cum.iloc[-1]:.2f}, worst month {100*port.min():+.1f}%, max drawdown {100*(1-cum/cum.cummax()).max():.1f}%")
    print("by year: " + "  ".join(f"{y}:{100*r['mean']:+.1f}%(n{int(r['count'])})" for y, r in yearly.iterrows()))
    return {"pass": verdict, "mean": port.mean(), "p": p_hac, "beat": beat}


def main() -> None:
    close, qvol = load_panel()
    contaminated = polar_bases()
    all_syms = list(close.columns)
    fresh = [s for s in all_syms if s.removesuffix("USDT") not in contaminated]
    polar = [s for s in all_syms if s.removesuffix("USDT") in contaminated]
    print(f"panel {close.shape[1]} symbols x {close.shape[0]} days; Polar-universe {len(polar)}, fresh {len(fresh)}")
    ref = summarize("REFERENCE: Polar universe (already seen by the original study)", run(close, qvol, polar))
    res = summarize("PRIMARY: fresh universe (never in any Polar universe)", run(close, qvol, fresh))
    print(f"\nPRE-REGISTERED VERDICT (fresh universe): {'PASS' if res['pass'] else 'FAIL'}")


if __name__ == "__main__":
    main()
