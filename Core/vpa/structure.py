"""Market structure: pivots, trend construction, congestion zones, levels.

This is the layer that answers "WHERE are we?" — and in VPA that question
is not optional decoration, it is a precondition. Coulling is explicit that
the same candle means opposite things in different locations ("just as a
candle can have a different significance, depending on where it appears in
the trend, the same is true with VPA"), and that an anomaly without a
location is not actionable. `signals.py` therefore consumes this module's
output as a required input, never as a bonus.

Four constructs, in dependency order:

  1. `isolated_pivots`   — Ch7's strict three-bar reversal.
  2. `trend_states`      — Ch8's dynamic trend from the pivot sequence.
  3. `congestion_zones`  — Ch7's ceiling/floor ranges (the "cause" whose
                           size sets the expected size of the "effect").
  4. `Level`             — horizontal support/resistance, graded by the
                           chapter's own significance criteria.

Everything here is CAUSAL: state computed at bar i depends only on bars
<= i, so re-running on a truncated prefix reproduces an identical prefix.
`tests/test_causality.py` checks this mechanically — it is the property
that separates a real backtest from a hindsight-fitted one, and this
project has already been burned once by a forming-bar leak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from Core.vpa.spread import atr

# Ch7 gives no tolerance number ("consider them to be 'elastic'"), so this
# is an ATR-relative default, not a book value.
LEVEL_TOLERANCE_ATR = 0.25
CLEAR_WATER_ATR = 0.5  # Ch7's "clearly defined close above the ceiling"


@dataclass(frozen=True)
class Pivot:
    bar_index: int
    price: float
    kind: str  # "high" | "low"


def isolated_pivots(df: pd.DataFrame) -> list[Pivot]:
    """Ch7's three-bar reversal, applied symmetrically.

    The book's own wording for a pivot high omits a clause its pivot-low
    definition includes; read literally that would make the two asymmetric.
    Coulling's pivot low is "a three bar/candle pattern where the centre
    candle has a lower low and a lower high than those on either side", so
    a pivot high requires BOTH a higher high and a higher low than its
    neighbours. Implemented symmetrically here.

    This is deliberately stricter than a bare `high[i] > high[i±1]` swing
    test: requiring both extremes to step in the same direction is what
    makes a pivot a genuine local reversal rather than a single long wick.
    """
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    pivots: list[Pivot] = []
    for i in range(1, len(df) - 1):
        if (
            highs[i] > highs[i - 1] and highs[i] > highs[i + 1]
            and lows[i] > lows[i - 1] and lows[i] > lows[i + 1]
        ):
            pivots.append(Pivot(i, float(highs[i]), "high"))
        elif (
            lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
            and highs[i] < highs[i - 1] and highs[i] < highs[i + 1]
        ):
            pivots.append(Pivot(i, float(lows[i]), "low"))
    return pivots


@dataclass(frozen=True)
class TrendState:
    bar_index: int
    label: str  # "HH" | "HL" | "LH" | "LL" | "undefined"
    trend: str  # "up" | "down" | "ranging" | "undefined"


def trend_states(pivots: list[Pivot]) -> list[TrendState]:
    """Ch8's dynamic trend, built one pivot at a time.

    A trend is "up" only while BOTH series step up (higher highs AND higher
    lows) — Ch8 requires each successive PH > prior PH and each PL > prior
    PL to stay in the position. A pivot that fails to extend does NOT flip
    the trend to the opposite direction; per Ch8 it demotes the state to
    "ranging" (a possible secondary congestion phase), and only the
    subsequent breakout resolves which way it goes. That asymmetry is
    load-bearing: treating a single lower high as a downtrend would flip
    direction on every ordinary pullback.
    """
    states: list[TrendState] = []
    last_high = last_low = None
    high_label = low_label = "undefined"
    for p in pivots:
        if p.kind == "high":
            high_label = "undefined" if last_high is None else ("HH" if p.price > last_high.price else "LH")
            last_high = p
        else:
            low_label = "undefined" if last_low is None else ("HL" if p.price > last_low.price else "LL")
            last_low = p

        if high_label == "HH" and low_label == "HL":
            trend = "up"
        elif high_label == "LH" and low_label == "LL":
            trend = "down"
        elif high_label == "undefined" and low_label == "undefined":
            trend = "undefined"
        else:
            trend = "ranging"
        states.append(TrendState(p.bar_index, high_label if p.kind == "high" else low_label, trend))
    return states


def trend_at(states: list[TrendState], bar_index: int) -> str:
    """The trend as it was known AT `bar_index` — i.e. from the most recent
    pivot that had already formed. Returns "undefined" before any pivot.

    Note a structural lag that is a property of pivots, not a bug: a pivot
    at bar i is only confirmable at bar i+1 (it needs its right-hand
    neighbour). This function therefore only consults pivots whose bar_index
    is strictly less than `bar_index`, so a caller judging bar i never uses
    a pivot that bar i itself completed."""
    latest = "undefined"
    for s in states:
        if s.bar_index < bar_index:
            latest = s.trend
        else:
            break
    return latest


@dataclass(frozen=True)
class CongestionZone:
    start_bar: int
    end_bar: int
    ceiling: float
    floor: float

    @property
    def bars(self) -> int:
        return self.end_bar - self.start_bar + 1

    @property
    def height(self) -> float:
        return self.ceiling - self.floor

    @property
    def cause(self) -> float:
        """Wyckoff's second law made numeric: the "cause" accumulated by a
        congestion zone, which sets how large an "effect" (subsequent trend
        move) to expect. Ch7: "the longer a market consolidates in a narrow
        range, then the more dramatic the resulting price action once the
        market moves away."

        Duration x height. Deliberately NOT duration alone — a 50-bar drift
        across a huge range is not the same coiled spring as 50 bars in a
        tight one, and the book's "narrow range" qualifier says so."""
        return self.bars * self.height

    def contains(self, price: float) -> bool:
        return self.floor <= price <= self.ceiling


MIN_ZONE_BARS = 10


def congestion_zones(
    pivots: list[Pivot],
    df: pd.DataFrame | None = None,
    min_pivots: int = 4,
    min_bars: int = MIN_ZONE_BARS,
) -> list[CongestionZone]:
    """Ch7/Ch11's congestion detection: a zone opens when consecutive pivots
    stop extending and instead oscillate between a repeated ceiling and floor.

    Three conditions, in the book's own order of authority:

    1. VOLUME FIRST. Ch11 is explicit that volume, not geometry, is what
       identifies a congestion phase: "The volume is all well below average
       (the white dotted line) therefore we already know that we are in a
       congestion phase, and the pivots are merely aids, to help define the
       price region for us." When `df` is supplied this is enforced — a
       candidate range whose mean volume is not below its own trailing
       average is a pause in a trend, not accumulation/distribution.
    2. PIVOT COUNT. Ch7: "the appearance of one pivot does not mean we are
       moving into a congestion phase." Four (two highs, two lows) is the
       smallest count that shows both boundaries respected twice.
    3. DURATION. A four-bar oscillation is noise, not a consolidation, and
       — because `cause` feeds the expected size of the subsequent move —
       admitting one produces a confidently-tiny target off nothing.
    """
    zones: list[CongestionZone] = []
    if not pivots:
        return zones

    runs: list[list[Pivot]] = []
    run: list[Pivot] = []
    for p in pivots:
        if not run:
            run = [p]
            continue
        ceiling = max(x.price for x in run + [p])
        floor = min(x.price for x in run + [p])
        prev_ceiling = max(x.price for x in run)
        prev_floor = min(x.price for x in run)
        # the new pivot keeps the run alive if it does not materially widen
        # the envelope -- i.e. it is oscillating inside, not breaking out.
        widened = (ceiling - floor) > (prev_ceiling - prev_floor) * 1.15 if len(run) > 1 else False
        if widened:
            runs.append(run)
            run = [p]
        else:
            run.append(p)
    runs.append(run)

    for r in runs:
        if len(r) < min_pivots:
            continue
        zone = _zone_from(r)
        if zone.bars < min_bars:
            continue
        if df is not None and not _volume_confirms_congestion(df, zone):
            continue
        zones.append(zone)
    return zones


def _volume_confirms_congestion(df: pd.DataFrame, zone: CongestionZone, lookback: int = 50) -> bool:
    """Ch11's volume-first rule: congestion shows below-average volume.

    Compares the zone's own mean volume against the trailing average of the
    bars BEFORE it (never bars inside it, which would be circular)."""
    start = max(zone.start_bar - lookback, 0)
    if zone.start_bar - start < 10:
        return True  # not enough prior history to judge — don't reject on ignorance
    prior_avg = df["volume"].iloc[start: zone.start_bar].mean()
    zone_avg = df["volume"].iloc[zone.start_bar: zone.end_bar + 1].mean()
    if prior_avg <= 0:
        return True
    return bool(zone_avg < prior_avg)


def _zone_from(run: list[Pivot]) -> CongestionZone:
    return CongestionZone(
        start_bar=min(p.bar_index for p in run),
        end_bar=max(p.bar_index for p in run),
        ceiling=max(p.price for p in run),
        floor=min(p.price for p in run),
    )


@dataclass
class Level:
    """A horizontal support/resistance level.

    Ch7 never states a minimum touch count; levels arise from pivots and are
    "reinforced" by later pivots at the same price. `touches` is therefore a
    strength input, not a validity gate — a one-touch level is real, just
    weak. Ch7 is also explicit that levels never expire ("these areas of
    price congestion remain on the charts forever"), so nothing here
    windows them out by age."""

    price: float
    touches: int = 1
    first_bar: int = 0
    last_bar: int = 0
    broken: bool = False
    kind: str = "resistance"  # flips on break -- Ch7's "polarity flip"
    bars_touched: list[int] = field(default_factory=list)

    def significance(self, current_bar: int) -> float:
        """Ch7's grading criteria, combined. More touches = stronger; a level
        that has survived longer is stronger; a level already broken once is
        weaker (but not worthless -- it becomes the flipped level)."""
        age = max(current_bar - self.first_bar, 1)
        score = self.touches * (1.0 + age ** 0.5 / 10.0)
        return score * (0.5 if self.broken else 1.0)


def build_levels(df: pd.DataFrame, pivots: list[Pivot], tolerance_atr: float = LEVEL_TOLERANCE_ATR) -> list[Level]:
    """Cluster pivots into levels within an ATR-scaled tolerance band.

    Ch7's "First Principle": levels are "NOT rods of steel... consider them
    to be 'elastic' with a little bit of 'give'." The band is ATR-relative
    so the same code works on a $0.06 alt and on BTC."""
    a = atr(df)
    levels: list[Level] = []
    for p in pivots:
        band = a.iloc[p.bar_index] if p.bar_index < len(a) and pd.notna(a.iloc[p.bar_index]) else None
        if band is None or band <= 0:
            continue
        tol = band * tolerance_atr
        match = next((lv for lv in levels if abs(lv.price - p.price) <= tol), None)
        if match is None:
            levels.append(
                Level(
                    price=p.price, touches=1, first_bar=p.bar_index, last_bar=p.bar_index,
                    kind="resistance" if p.kind == "high" else "support",
                    bars_touched=[p.bar_index],
                )
            )
        else:
            # weighted mean keeps the level anchored near where it has most
            # often been respected, rather than drifting to the newest touch
            match.price = (match.price * match.touches + p.price) / (match.touches + 1)
            match.touches += 1
            match.last_bar = p.bar_index
            match.bars_touched.append(p.bar_index)
    return levels
