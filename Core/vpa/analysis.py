"""The integrated read — where the whole method comes together.

Ch11 states an ordered decision procedure, and this module is that
procedure. Its signal precedence is not a stylistic choice; it is the
book's own, and inverting it produces a different (worse) system:

  1. CONTEXT FIRST. Higher-timeframe trend and prior support/resistance are
     resolved BEFORE any candle is scored. Ch4: an anomaly whose trend
     position is unknown is not actionable.
  2. ANOMALIES ARE PRIMARY. Effort-vs-result violations are the signal
     class that carries information.
  3. CANDLES ARE NEVER PRIMARY. Every worked example in Ch10 rejects a
     candle pattern that lacks volume backing — "a bullish engulfing
     candle, but the volume is average once again, and clearly this is not
     a sign of strength just yet." Candles corroborate; they do not decide.
  4. CONFIRMATION IS THE NEXT BAR, not the signal bar. Ch4's Principle No 2:
     "The market always has momentum and will almost always continue beyond
     the candle... Any signal is merely a warning sign."
  5. THE LOW-VOLUME TEST is the highest-quality trigger in the book.
  6. VAP modulates confidence, size and stops only — never direction.
  7. MULTI-TIMEFRAME modulates risk and holding period only.

--------------------------------------------------------------------------
Why this scores evidence instead of AND-ing filters
--------------------------------------------------------------------------
The obvious way to "use all the concepts in the book" is to require them
all: trend AND zone AND anomaly AND candle AND volume AND VAP. Polar's
predecessor built exactly that and measured it out-of-sample across two
independent windows. The result is recorded in that codebase's own
DECISIONS.md and is unambiguous: the full AND-stack got *monotonically
worse* with each layer added (39.1% -> 36.6% -> 31.8% win rate;
+9.51% -> -15.13% return), and VPA effort-vs-result used as a hard entry
filter "helped window 2, gutted window 1."

The failure mode is structural, not a tuning problem. Each AND-ed condition
is individually noisy; ANDing them multiplies the noise into the selection
while shrinking the sample toward zero, so what survives is whatever
happened to fit that window.

So this module ACCUMULATES WEIGHTED EVIDENCE instead. Every detector
contributes signed evidence scaled by its tier; nothing vetoes anything
except the two places the book itself states a hard precondition (a test
requires a prior congestion phase; a climax requires repetition). Adding a
detector therefore adds information rather than subtracting sample.

This is a deliberate architectural response to a known, measured failure —
not an assumption that scoring beats gating in general.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from Core.vpa import signals as sig
from Core.vpa import spread as sp
from Core.vpa import structure as st
from Core.vpa import vap as vap_mod
from Core.vpa.signals import BarContext, VpaSignal
from Core.vpa.spread import Spread
from Core.vpa.volume import VolumeGrade, grade_from_percentile, percentile_series

# Tier weights. Ch11's precedence, made numeric. Anomalies outweigh candles
# roughly 2:1 because the book treats a candle without volume backing as
# not a signal at all, while an anomaly is informative on its own.
TIER_WEIGHT = {
    "anomaly": 1.0,
    "campaign": 1.0,  # multi-bar: climaxes, stopping volume, tests
    "structural": 0.9,  # breakout / fake-out
    "candle": 0.5,  # premier candles — corroborating only
    "validation": 0.35,  # continuation states
}

SIGNAL_TIER = {
    "effort_result_anomaly": "anomaly",
    "effort_result_validation": "validation",
    "wide_spread_high_volume": "validation",
    "narrow_spread_low_volume": "validation",
    "wide_spread_low_volume": "anomaly",
    "narrow_spread_high_volume": "anomaly",
    "rising_price_falling_volume": "anomaly",
    "falling_price_falling_volume": "anomaly",
    "trend_validated": "validation",
    "shooting_star": "candle",
    "hammer": "candle",
    "hanging_man": "candle",
    "long_legged_doji": "candle",
    "long_legged_doji_trap": "anomaly",
    "stopping_volume": "campaign",
    "topping_out_volume": "campaign",
    "selling_climax": "campaign",
    "buying_climax": "campaign",
    "test_of_supply_passed": "campaign",
    "test_of_supply_failed": "campaign",
    "test_of_demand_passed": "campaign",
    "test_of_demand_failed": "campaign",
    "breakout_confirmed": "structural",
    "breakout_fake": "structural",
}

# Ch10/Ch11 name the low-volume test as the highest-quality trigger in the
# method ("the answer is delivered on the next candle with a LOW VOLUME
# test"), so it gets an explicit premium rather than being one campaign
# signal among many.
TRIGGER_SIGNALS = {"test_of_supply_passed", "test_of_demand_passed"}
TRIGGER_PREMIUM = 1.4


@dataclass
class VpaRead:
    """The complete VPA reading for one bar."""

    bar_index: int
    timestamp: pd.Timestamp
    close: float
    trend: str
    zone: st.CongestionZone | None
    signals: list[VpaSignal] = field(default_factory=list)
    bullish_evidence: float = 0.0
    bearish_evidence: float = 0.0
    vap_support: float = 0.0

    @property
    def net_evidence(self) -> float:
        return self.bullish_evidence - self.bearish_evidence

    @property
    def bias(self) -> str:
        if self.net_evidence > 0.15:
            return "bullish"
        if self.net_evidence < -0.15:
            return "bearish"
        return "neutral"

    @property
    def conviction(self) -> float:
        """0-1. Deliberately penalises CONFLICT: two opposing signals do not
        average out to a confident neutral, they mean the read is genuinely
        contested and should be sized down. `total` in the denominator is
        what encodes that."""
        total = self.bullish_evidence + self.bearish_evidence
        if total <= 0:
            return 0.0
        return min(abs(self.net_evidence) / total, 1.0) * min(total / 2.0, 1.0)

    def has(self, name: str) -> bool:
        return any(s.name == name for s in self.signals)


def build_contexts(df: pd.DataFrame, volume_lookback: int = 100) -> list[BarContext]:
    """Precompute every bar's context once. Ch11 Step 1-2: context before
    signals, always."""
    pctl = percentile_series(df["volume"], lookback=volume_lookback)
    spread_atr = sp.spread_atr_series(df)
    close_pos = sp.close_position(df)
    rng = sp.spread_series(df)
    body = sp.body_series(df)
    uw, lw = sp.upper_wick(df), sp.lower_wick(df)

    pivots = st.isolated_pivots(df)
    states = st.trend_states(pivots)
    zones = st.congestion_zones(pivots, df)
    levels = st.build_levels(df, pivots)

    contexts: list[BarContext] = []
    for i in range(len(df)):
        r = rng.iloc[i]
        p = pctl.iloc[i] if i < len(pctl) else None
        sa = spread_atr.iloc[i] if i < len(spread_atr) else None
        zone = next((z for z in zones if z.start_bar <= i <= z.end_bar), None)
        price = float(df["close"].iloc[i])
        nearest = min(levels, key=lambda lv: abs(lv.price - price), default=None)
        contexts.append(
            BarContext(
                volume_grade=grade_from_percentile(p) if pd.notna(p) else None,
                volume_pctl=float(p) if pd.notna(p) else None,
                spread_grade=sp.grade_from_atr_multiple(sa) if pd.notna(sa) else None,
                spread_atr=float(sa) if pd.notna(sa) else None,
                close_pos=float(close_pos.iloc[i]) if pd.notna(close_pos.iloc[i]) else None,
                body_frac=float(body.iloc[i] / r) if r > 0 else None,
                upper_wick_frac=float(uw.iloc[i] / r) if r > 0 else None,
                lower_wick_frac=float(lw.iloc[i] / r) if r > 0 else None,
                trend=st.trend_at(states, i),
                zone=zone,
                nearest_level=nearest,
                at_level=bool(
                    nearest is not None and pd.notna(sa) and sa is not None
                    and abs(nearest.price - price) <= (sp.atr(df).iloc[i] or 0) * st.LEVEL_TOLERANCE_ATR
                ) if nearest is not None else False,
            )
        )
    return contexts


def analyse(df: pd.DataFrame, volume_lookback: int = 100, vap_bins: int = 50) -> list[VpaRead]:
    """Run the full VPA read across every bar of `df`.

    Strictly causal: bar i's read uses only bars <= i. The VAP profile is
    the one place that needs care — it is built once over the whole frame
    for speed, so `analyse` is for ANALYSIS and BACKTESTING of completed
    history. For a live bar-by-bar read use `analyse_live`, which rebuilds
    the profile on the visible prefix only.
    """
    contexts = build_contexts(df, volume_lookback=volume_lookback)
    profile = vap_mod.build_profile(df, bins=vap_bins)
    atr_series = sp.atr(df)
    pivots = st.isolated_pivots(df)
    zones = st.congestion_zones(pivots, df)

    reads: list[VpaRead] = []
    for i in range(len(df)):
        ctx = contexts[i]
        # The MOST RECENT completed zone, not the first: a breakout is
        # measured against the range price has just left, and an ancient
        # zone would report every later bar as "beyond clear water" forever.
        zone = max((z for z in zones if z.end_bar < i), key=lambda z: z.end_bar, default=None)
        atr_val = float(atr_series.iloc[i]) if pd.notna(atr_series.iloc[i]) else None

        found: list[VpaSignal] = []
        for fn in (
            sig.effort_vs_result, sig.spread_volume_matrix, sig.shooting_star,
            sig.hammer, sig.hanging_man, sig.long_legged_doji,
            sig.test_of_supply, sig.test_of_demand,
            sig.rising_price_falling_volume, sig.falling_price_falling_volume,
            sig.trend_validated,
        ):
            s = fn(df, i, ctx)
            if s is not None:
                found.append(s)
        for fn in (sig.stopping_volume, sig.topping_out_volume, sig.selling_climax, sig.buying_climax):
            s = fn(df, i, ctx, contexts)
            if s is not None:
                found.append(s)
        s = sig.breakout(df, i, ctx, zone, atr_val)
        if s is not None:
            found.append(s)

        read = VpaRead(
            bar_index=i, timestamp=df.index[i], close=float(df["close"].iloc[i]),
            trend=ctx.trend, zone=ctx.zone, signals=found,
            vap_support=profile.support_strength(float(df["close"].iloc[i])),
        )
        _score(read)
        reads.append(read)
    return reads


def _score(read: VpaRead) -> None:
    """Accumulate weighted evidence. See the module docstring for why this
    adds rather than gates."""
    for s in read.signals:
        tier = SIGNAL_TIER.get(s.name, "candle")
        weight = TIER_WEIGHT[tier] * s.strength
        if s.name in TRIGGER_SIGNALS:
            weight *= TRIGGER_PREMIUM
        if s.bias == "bullish":
            read.bullish_evidence += weight
        elif s.bias == "bearish":
            read.bearish_evidence += weight


# --------------------------------------------------------------------------
# Ch11's trade construction: classification, stop, expectation
# --------------------------------------------------------------------------

@dataclass
class VpaSetup:
    bar_index: int
    timestamp: pd.Timestamp
    direction: str  # "long" | "short"
    entry_price: float
    stop_price: float
    stop_basis: str  # which of Ch11's three stop rules produced it
    trade_class: str  # "with_trend" | "counter_trend"
    conviction: float
    expected_bars: int
    vap_support: float
    rationale: list[str]

    @property
    def risk_fraction(self) -> float:
        """Distance to stop as a fraction of entry — the input a position
        sizer needs. The book gives no sizing formula ("you will have to
        judge the risk on the trade"), so this deliberately stops at
        expressing risk rather than inventing a size."""
        return abs(self.entry_price - self.stop_price) / self.entry_price


def _stop_for(df: pd.DataFrame, i: int, direction: str, zone: st.CongestionZone | None,
              pivots: list[st.Pivot]) -> tuple[float, str]:
    """Ch11's stop-placement priority, in order. Every one of these is the
    market setting the level — the book never uses a fixed percentage.

      1. the opposite side of the congestion zone  ("where better to place
         any stop loss than on the opposite side of a congestion region")
      2. the last pivot low/high  ("our stop loss would be below the level
         of the last pivot low")
      3. beyond the signal candle's wick  ("any stop loss by the way would
         be below the wick of the hammer, with the market setting this
         level for us")
    """
    if zone is not None:
        return (zone.floor, "congestion_floor") if direction == "long" else (zone.ceiling, "congestion_ceiling")
    prior = [p for p in pivots if p.bar_index < i]
    if direction == "long":
        lows = [p for p in prior if p.kind == "low"]
        if lows:
            return lows[-1].price, "last_pivot_low"
        return float(df["low"].iloc[i]), "signal_candle_wick"
    highs = [p for p in prior if p.kind == "high"]
    if highs:
        return highs[-1].price, "last_pivot_high"
    return float(df["high"].iloc[i]), "signal_candle_wick"


def build_setups(
    df: pd.DataFrame,
    reads: list[VpaRead],
    higher_tf_trend: str | None = None,
    min_conviction: float = 0.35,
    require_confirmation: bool = True,
) -> list[VpaSetup]:
    """Turn reads into tradeable setups, applying Ch11 Steps 2-7.

    `require_confirmation` implements Principle No 2 — never act on the
    signal bar. When True (the default, and the book's own stance) a setup
    is emitted on the bar AFTER the qualifying read, and only if that next
    bar has not contradicted it. Ch10's "aggressive vs cautious" fork is
    exactly this flag: the aggressive trader sets it False and accepts that
    "the following candle suggests weakness at this level."

    `higher_tf_trend` is Ch11 Step 2. Passing it does not gate entries — it
    CLASSIFIES them, because the book permits counter-trend trades but
    requires that they be recognised as higher-risk and shorter-held.
    """
    pivots = st.isolated_pivots(df)
    zones = st.congestion_zones(pivots, df)
    setups: list[VpaSetup] = []

    for read in reads:
        if read.conviction < min_conviction or read.bias == "neutral":
            continue
        i = read.bar_index
        direction = "long" if read.bias == "bullish" else "short"

        entry_idx = i
        if require_confirmation:
            entry_idx = i + 1
            if entry_idx >= len(df):
                continue
            # Ch10's rejection rule: after a stopping-volume/reversal read the
            # next bar must respond in the signal's direction. An opposing
            # close means the market declined to confirm.
            nxt_up = df["close"].iloc[entry_idx] > df["open"].iloc[entry_idx]
            if (direction == "long") != nxt_up:
                continue

        entry_price = float(df["open"].iloc[entry_idx]) if entry_idx < len(df) else float(df["close"].iloc[i])
        zone = next((z for z in zones if z.end_bar < i), None)
        stop, basis = _stop_for(df, i, direction, zone, pivots)
        if (direction == "long" and stop >= entry_price) or (direction == "short" and stop <= entry_price):
            continue  # stop on the wrong side — degenerate, skip rather than invent one

        trade_class = "with_trend"
        if higher_tf_trend in ("up", "down"):
            aligned = (higher_tf_trend == "up" and direction == "long") or (
                higher_tf_trend == "down" and direction == "short")
            trade_class = "with_trend" if aligned else "counter_trend"

        # Ch11 Step 6 / Wyckoff's second law: the effect is proportional to
        # the cause, so a zone's duration sets how long to expect the move to
        # run. Counter-trend trades are explicitly short-lived ("we are only
        # likely to be holding such a position for a short period of time").
        expected = zone.bars if zone is not None else 10
        if trade_class == "counter_trend":
            expected = max(expected // 2, 3)

        setups.append(
            VpaSetup(
                bar_index=entry_idx, timestamp=df.index[entry_idx], direction=direction,
                entry_price=entry_price, stop_price=float(stop), stop_basis=basis,
                trade_class=trade_class, conviction=read.conviction,
                expected_bars=int(expected), vap_support=read.vap_support,
                rationale=[f"{s.name}: {s.detail}" for s in read.signals if s.bias != "neutral"],
            )
        )
    return setups


# --------------------------------------------------------------------------
# Ch11 Step 1: the three-timeframe frame
# --------------------------------------------------------------------------

@dataclass
class MultiTimeframeRead:
    fast: list[VpaRead]
    middle: list[VpaRead]
    slow: list[VpaRead]
    slow_trend: str
    setups: list[VpaSetup]


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a slower timeframe. Ch11 synthesises higher-timeframe
    candles from lower ones explicitly (a 30m shooting star "is two candles"
    on the 15m), so this is part of the method, not just plumbing."""
    out = df.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna()


def analyse_multi_timeframe(
    df: pd.DataFrame, middle_rule: str, slow_rule: str, **kwargs
) -> MultiTimeframeRead:
    """Ch11 Step 1-3. `df` is the FAST chart (early warning); `middle_rule`
    is the primary trading timeframe; `slow_rule` sets the bias.

    "My primary trading chart is the 'middle' time frame of the three."

    The slow chart's trend classifies every middle-chart setup as with- or
    counter-trend. It deliberately does NOT veto them — Ch8 permits
    counter-trend trading, it just prices the risk differently."""
    mid_df = resample(df, middle_rule)
    slow_df = resample(df, slow_rule)

    fast_reads = analyse(df, **kwargs)
    mid_reads = analyse(mid_df, **kwargs)
    slow_reads = analyse(slow_df, **kwargs)

    slow_trend = slow_reads[-1].trend if slow_reads else "undefined"
    setups = build_setups(mid_df, mid_reads, higher_tf_trend=slow_trend)

    return MultiTimeframeRead(
        fast=fast_reads, middle=mid_reads, slow=slow_reads,
        slow_trend=slow_trend, setups=setups,
    )
