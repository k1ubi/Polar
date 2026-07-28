"""Volume At Price — where volume actually transacted, not when.

Ch9's construction: rotate the volume axis 90 degrees so volume accumulates
against PRICE rather than time — "taking that volume bar, and cutting it
open to reveal where the concentration of volumes actually occurred."

The chapter is emphatic about this module's role, and it is a role that
constrains the whole design:

    "VAP is a supporting technique to VPA, NOT the other way round... use
    VAP as a tool with which to identify price congestion along with support
    and resistance zones, which you can then confirm with traditional
    analysis using VPA."

So nothing here ever generates an entry. VAP does exactly three jobs, all
of them modulation of a decision made elsewhere:

  1. locate candidate support/resistance zones (high-volume nodes),
  2. scale confidence/size — a strong node behind you is conviction, a weak
     one is a reason to stand aside (never a reason to trade the other way),
  3. place stops — beyond a high-volume node, never behind a thin one.

The one genuinely non-obvious rule in the chapter is TIME NORMALISATION
(`node_intensity`): volume mass must be divided by the number of bars that
produced it. Ch9's own worked case is a zone that accumulated "almost the
same concentration of volume" as a benchmark zone but in a third of the
time — that compression is the distribution tell, and raw mass hides it
completely.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class VapNode:
    low: float
    high: float
    volume: float
    up_bars: int
    down_bars: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def bias(self) -> float:
        """Ch9's colour split. Note this counts BARS, not volume — "what each
        bar represents, with the two colours, is the number of up or down
        candles associated with that phase of price action." Range -1..+1."""
        total = self.up_bars + self.down_bars
        return 0.0 if total == 0 else (self.up_bars - self.down_bars) / total


@dataclass(frozen=True)
class VapProfile:
    nodes: list[VapNode]

    @property
    def point_of_control(self) -> VapNode | None:
        """The single highest-volume price bin — the market's fairest-value
        price over the window, and the level most expensive to push through."""
        return max(self.nodes, key=lambda n: n.volume) if self.nodes else None

    def high_volume_nodes(self, quantile: float = 0.8) -> list[VapNode]:
        """Nodes in the top `quantile` of volume: Ch9's "strong platform of
        support", which "will take some extreme volumes to penetrate"."""
        if not self.nodes:
            return []
        cutoff = float(np.quantile([n.volume for n in self.nodes], quantile))
        return [n for n in self.nodes if n.volume >= cutoff]

    def low_volume_nodes(self, quantile: float = 0.2) -> list[VapNode]:
        """Thin regions. Ch9: "it would not take much effort to penetrate
        this level, either from below or above" — weak symmetrically, which
        is why they make bad stop locations in both directions."""
        if not self.nodes:
            return []
        cutoff = float(np.quantile([n.volume for n in self.nodes], quantile))
        return [n for n in self.nodes if n.volume <= cutoff]

    def volume_at(self, price: float) -> float:
        for n in self.nodes:
            if n.low <= price <= n.high:
                return n.volume
        return 0.0

    def support_strength(self, price: float) -> float:
        """0-1: how much traded volume sits at `price` relative to the
        heaviest node. This is the confidence/size multiplier and the stop
        quality check — Ch9's "if it is strong, then we have additional
        confidence to take a position, if it is weak, we may hold back"."""
        poc = self.point_of_control
        if poc is None or poc.volume <= 0:
            return 0.0
        return min(self.volume_at(price) / poc.volume, 1.0)


def build_profile(df: pd.DataFrame, bins: int = 50) -> VapProfile:
    """Distribute each bar's volume across the price range it traded through.

    The book gives no intra-bar distribution model, so volume is spread
    UNIFORMLY across each candle's high-low range. That is the honest
    default: assuming it concentrated at the close (a common shortcut) would
    smuggle in an assumption the data does not support, and Ch9's own
    intra-bar reasoning ("if the concentration of volume was at the bottom,
    then this is more likely to be buying volume") explicitly requires
    sub-bar data we do not have here."""
    if df.empty:
        return VapProfile([])
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        return VapProfile([])

    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    up = np.zeros(bins, dtype=int)
    down = np.zeros(bins, dtype=int)

    for row in df.itertuples():
        bar_lo, bar_hi = float(row.low), float(row.high)
        if bar_hi <= bar_lo:
            idx = min(max(np.searchsorted(edges, bar_lo) - 1, 0), bins - 1)
            vol[idx] += float(row.volume)
            (up if row.close >= row.open else down)[idx] += 1
            continue
        first = min(max(np.searchsorted(edges, bar_lo) - 1, 0), bins - 1)
        last = min(max(np.searchsorted(edges, bar_hi) - 1, 0), bins - 1)
        span = last - first + 1
        share = float(row.volume) / span
        vol[first: last + 1] += share
        if row.close >= row.open:
            up[first: last + 1] += 1
        else:
            down[first: last + 1] += 1

    nodes = [
        VapNode(low=float(edges[k]), high=float(edges[k + 1]), volume=float(vol[k]),
                up_bars=int(up[k]), down_bars=int(down[k]))
        for k in range(bins)
    ]
    return VapProfile(nodes)


def node_intensity(profile: VapProfile, low: float, high: float, bars: int) -> float:
    """Ch9's time-normalisation rule — the chapter's real payload.

    Volume mass in a price zone DIVIDED BY the bars that built it. The
    worked case: a zone that accumulated nearly the same volume as a
    benchmark zone "which lasted for three times as long". Raw mass called
    those two equivalent; intensity correctly flags the compressed one as
    distribution.

    High intensity + narrow price range = distribution/accumulation (the
    market absorbed heavy volume without moving). High intensity + deep
    price range = genuine support building. Ch9 uses exactly that range
    distinction to separate its third and fourth levels."""
    if bars <= 0:
        return 0.0
    mass = sum(n.volume for n in profile.nodes if n.high >= low and n.low <= high)
    return mass / bars
