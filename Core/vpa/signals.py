"""The VPA signal catalogue — every mechanically-testable reading in the book.

Organised the way Coulling organises the method itself, not alphabetically:

  Tier 1  the master classifier      — validation vs. anomaly (Ch4)
  Tier 2  the spread x volume matrix — the four canonical bar readings (Ch4)
  Tier 3  the premier candles        — shooting star, hammer, hanging man,
                                       long-legged doji (Ch6)
  Tier 4  multi-bar campaigns        — stopping/topping volume, climaxes,
                                       tests, anomaly sequences (Ch5, Ch6)
  Tier 5  structural events          — breakout, fake-out, re-test (Ch7)

Two conventions that will cause sign errors if missed:

  * Coulling INVERTS the usual climax naming, and says so explicitly: "a
    selling climax appears at the top of the bullish trend, whilst the
    buying climax appears at the bottom of a bearish trend... many people
    refer to these events the other way round!" Named her way here.

  * A signal's `bias` is what it says about FUTURE price, not the colour of
    the candle. A narrow-spread UP bar on high volume is bearish.

Every detector takes the same `(df, i, ctx)` shape and returns
`VpaSignal | None`, so `analysis.py` can run them uniformly. `ctx` carries
the precomputed per-bar context (volume grade, spread grade, trend, zones)
— context is a REQUIRED input, not an optional refinement, because Ch4 is
explicit that an anomaly whose trend position is unknown is not actionable.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from Core.vpa import spread as sp
from Core.vpa.spread import Spread
from Core.vpa.structure import CongestionZone, Level, TrendState, trend_at
from Core.vpa.volume import VolumeGrade


@dataclass(frozen=True)
class VpaSignal:
    name: str
    bar_index: int
    bias: str  # "bullish" | "bearish" | "neutral"
    kind: str  # "validation" | "anomaly"
    strength: float  # 0-1, this signal's own conviction before context weighting
    detail: str
    span: int = 1  # how many bars the signal covers (multi-bar campaigns > 1)


@dataclass
class BarContext:
    """Everything a detector is allowed to know about bar `i`, precomputed
    once by `analysis.py` rather than recomputed per detector."""

    volume_grade: VolumeGrade | None
    volume_pctl: float | None
    spread_grade: Spread | None
    spread_atr: float | None
    close_pos: float | None  # 0=on the low, 1=on the high
    body_frac: float | None  # |close-open| / range
    upper_wick_frac: float | None
    lower_wick_frac: float | None
    trend: str  # "up" | "down" | "ranging" | "undefined"
    zone: CongestionZone | None  # congestion zone containing this bar, if any
    nearest_level: Level | None
    at_level: bool


# Wick/body geometry cutoffs. The book gives no ratios anywhere ("purely
# visual"), so these are reasoned defaults, flagged as such in Doc/Core/vpa.md.
NARROW_BODY = 0.30  # body <= this fraction of range = "narrow body"
DEEP_WICK = 0.45  # wick >= this fraction of range = "deep wick"
DOJI_BODY = 0.10
SMALL_WICK = 0.15  # both wicks <= this = "small wicks", licences pure-side attribution


# --------------------------------------------------------------------------
# Tier 1 — the master classifier: validation or anomaly
# --------------------------------------------------------------------------

def effort_vs_result(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """Wyckoff's third law, the book's central analytical idea.

    EFFORT is volume, RESULT is spread. The book's own test is a bar-over-bar
    agreement of DIRECTION, not a ratio: "the third candle... closes with a
    spread which is wider than both the first and the second, we should
    expect a volume bar which reflects Wyckoff's third law... increased the
    result should be matched by increased effort."

    So: sign(delta spread) == sign(delta volume) is validation; a mismatch is
    an anomaly. Both directions count — "the market requires effort to both
    rise AND fall" is called out as the standard student error.

    Deliberately returns the raw agreement reading for EVERY bar rather than
    only firing on anomalies: `analysis.py` needs the validation reading too,
    because a run of validations is what licenses "the trend is genuine".
    """
    if i < 1:
        return None
    d_spread = (df["high"].iloc[i] - df["low"].iloc[i]) - (df["high"].iloc[i - 1] - df["low"].iloc[i - 1])
    d_volume = df["volume"].iloc[i] - df["volume"].iloc[i - 1]
    if d_spread == 0 or d_volume == 0:
        return None
    agrees = (d_spread > 0) == (d_volume > 0)
    if agrees:
        return VpaSignal(
            "effort_result_validation", i, "neutral", "validation", 0.25,
            "spread and volume moved together — price is validated by volume",
        )
    # An anomaly's bias depends on which way the mismatch runs.
    if d_spread > 0 and d_volume < 0:
        return VpaSignal(
            "effort_result_anomaly", i, "neutral", "anomaly", 0.45,
            "wider spread on LOWER volume — a big result from little effort; move not supported",
        )
    return VpaSignal(
        "effort_result_anomaly", i, "neutral", "anomaly", 0.5,
        "narrower spread on HIGHER volume — big effort, no result; absorption",
    )


# --------------------------------------------------------------------------
# Tier 2 — the spread x volume matrix (Ch4, Figs 4.10-4.13)
# --------------------------------------------------------------------------

def spread_volume_matrix(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """The four canonical single-bar readings. Two validations, two anomalies.

    Cell 3 (wide + low) and cell 4 (narrow + high) are the tradeable ones;
    the two validations are continuation states that mostly serve to keep a
    position rather than open one.

    Note on the source: Fig 4.13's HEADING reads "Narrow Spread Candle, Low
    Volume" but its body text describes narrow spread + HIGH volume, which
    is the only reading that makes it the fourth matrix cell (narrow+low is
    already Fig 4.11). Implemented per the body text.
    """
    if ctx.volume_grade is None or ctx.spread_grade is None:
        return None
    up = df["close"].iloc[i] > df["open"].iloc[i]
    vol_high = ctx.volume_grade >= VolumeGrade.HIGH
    vol_low = ctx.volume_grade <= VolumeGrade.LOW

    if ctx.spread_grade == Spread.WIDE and vol_high:
        small_wicks = (
            ctx.upper_wick_frac is not None and ctx.lower_wick_frac is not None
            and ctx.upper_wick_frac <= SMALL_WICK and ctx.lower_wick_frac <= SMALL_WICK
        )
        return VpaSignal(
            "wide_spread_high_volume", i, "bullish" if up else "bearish", "validation",
            0.55 if small_wicks else 0.4,
            "wide spread on high volume — genuine move, insiders participating"
            + ("; small wicks confirm one-sided pressure" if small_wicks else "; wicks show opposing pressure"),
        )

    if ctx.spread_grade == Spread.NARROW and vol_low:
        return VpaSignal(
            "narrow_spread_low_volume", i, "neutral", "validation", 0.15,
            "narrow spread on low volume — small result from small effort; benign, no information",
        )

    if ctx.spread_grade == Spread.WIDE and vol_low:
        return VpaSignal(
            "wide_spread_low_volume", i, "bearish" if up else "bullish", "anomaly", 0.55,
            "wide spread on LOW volume — the market is not joining in; a fake move to trap "
            "traders and take out stops, expect it to be marked back",
        )

    if ctx.spread_grade == Spread.NARROW and vol_high:
        # Ch4: this is an exhaustion/absorption signature and REQUIRES trend
        # position -- "typical of a candle pattern that starts to develop at
        # the top of a bullish trend, or the bottom of a bearish trend".
        if ctx.trend == "up":
            return VpaSignal(
                "narrow_spread_high_volume", i, "bearish", "anomaly", 0.6,
                "narrow spread on high volume at the top of an up move — buying absorbed by "
                "insider selling; the market has reached a price where more effort buys no more gain",
            )
        if ctx.trend == "down":
            return VpaSignal(
                "narrow_spread_high_volume", i, "bullish", "anomaly", 0.6,
                "narrow spread on high volume at the bottom of a down move — selling absorbed; "
                "had selling followed through this would be a wide spread down bar, and it is not",
            )
        return VpaSignal(
            "narrow_spread_high_volume", i, "neutral", "anomaly", 0.3,
            "narrow spread on high volume mid-trend — absorption, but without a trend extreme "
            "to reverse it is a pause signal only",
        )
    return None


# --------------------------------------------------------------------------
# Tier 3 — the premier candles (Ch6)
# --------------------------------------------------------------------------

def _is_shooting_star(ctx: BarContext) -> bool:
    return (
        ctx.body_frac is not None and ctx.upper_wick_frac is not None and ctx.lower_wick_frac is not None
        and ctx.body_frac <= NARROW_BODY
        and ctx.upper_wick_frac >= DEEP_WICK
        and ctx.lower_wick_frac <= ctx.upper_wick_frac * 0.35
    )


def _is_hammer(ctx: BarContext) -> bool:
    return (
        ctx.body_frac is not None and ctx.upper_wick_frac is not None and ctx.lower_wick_frac is not None
        and ctx.body_frac <= NARROW_BODY
        and ctx.lower_wick_frac >= DEEP_WICK
        and ctx.upper_wick_frac <= ctx.lower_wick_frac * 0.35
    )


def shooting_star(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """Ch6's first premier candle. Volume GRADES it rather than confirming or
    denying it: "There is never an anomaly with a shooting star, only ever a
    validation." Low volume does not invalidate the shape, it just makes the
    weakness minor.

    Crucially this is a signal of POTENTIAL weakness at that price, not a
    reversal — "Their appearance DOES NOT signal an immediate reversal."
    """
    if not _is_shooting_star(ctx) or ctx.volume_grade is None:
        return None
    if ctx.volume_grade >= VolumeGrade.HIGH:
        strength, note = 0.6, "on high volume — professional money is selling out here"
    elif ctx.volume_grade >= VolumeGrade.AVERAGE:
        strength, note = 0.4, "on average volume — a relatively strong signal"
    else:
        strength, note = 0.2, "on low volume — weakness present but probably not significant"
    # Ch6's explicit suppression: right after a breakout from accumulation a
    # shooting star is a pause, not the start of a reversal.
    if ctx.trend == "up":
        strength += 0.1
    return VpaSignal("shooting_star", i, "bearish", "validation", min(strength, 1.0),
                     f"shooting star {note}")


def hammer(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """The mirror. Ch6 is emphatic that one is not enough: "a single hammer
    will simply not be enough to halt the move lower, even if the volume is
    above average" — a genuine buying climax needs two or three. Strength is
    scaled accordingly and `analysis.py` escalates on repetition."""
    if not _is_hammer(ctx) or ctx.volume_grade is None:
        return None
    if ctx.trend == "up":
        return None  # same shape at the top of an up move is a hanging man
    if ctx.volume_grade >= VolumeGrade.HIGH:
        strength, note = 0.55, "on high volume — insiders buying heavily; possible buying climax"
    elif ctx.volume_grade >= VolumeGrade.AVERAGE:
        strength, note = 0.35, "on average volume — a possible reversal, intraday scale"
    else:
        strength, note = 0.2, "on low volume — little interest at this level; any reversal likely minor"
    return VpaSignal("hammer", i, "bullish", "validation", strength, f"hammer {note}")


def hanging_man(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """Hammer geometry at the TOP of an established up move. Ch6 makes
    above-average volume a REQUIRED condition here (unlike the shooting star,
    where volume only grades): it "is only a sign of weakness provided it is
    associated with above average volume"."""
    if not _is_hammer(ctx) or ctx.volume_grade is None:
        return None
    if ctx.trend != "up":
        return None
    if ctx.volume_grade < VolumeGrade.ABOVE_AVERAGE:
        return None
    return VpaSignal(
        "hanging_man", i, "bearish", "validation", 0.4,
        "hanging man on above-average volume at the top of an up move — first sustained selling "
        "pressure; buyers still absorbed it, so this is early warning only",
    )


def long_legged_doji(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """The one premier candle where a volume ANOMALY exists, and the anomaly
    inverts the meaning entirely.

    Ch6: "A long legged doji candle should always be validated by a minimum
    of average volume, and preferably high or ultra high. If it is low, then
    it is an anomaly and therefore a trap set by the insiders." The low-volume
    branch is a stop-hunt around news, and the correct action is to STAY OUT
    — so it is emitted as an explicit neutral warning rather than suppressed,
    because "no signal" and "an active trap" are different states.
    """
    if ctx.body_frac is None or ctx.upper_wick_frac is None or ctx.lower_wick_frac is None:
        return None
    if not (ctx.body_frac <= DOJI_BODY and ctx.upper_wick_frac >= 0.3 and ctx.lower_wick_frac >= 0.3):
        return None
    if ctx.volume_grade is None:
        return None
    if ctx.volume_grade <= VolumeGrade.LOW:
        return VpaSignal(
            "long_legged_doji_trap", i, "neutral", "anomaly", 0.5,
            "long-legged doji on LOW volume — effort/result violation; this is price racking to "
            "hunt stops, typically around news. Stay out, wait for further candles",
        )
    bias = "bearish" if ctx.trend == "up" else ("bullish" if ctx.trend == "down" else "neutral")
    return VpaSignal(
        "long_legged_doji", i, bias, "validation", 0.45 if bias != "neutral" else 0.25,
        "long-legged doji on validating volume — bulls and bears in balance; potential reversal "
        f"of the prevailing {ctx.trend} move",
    )


# --------------------------------------------------------------------------
# Tier 4 — multi-bar campaigns (Ch5, Ch6)
# --------------------------------------------------------------------------

STOPPING_MIN_BARS = 3


def stopping_volume(df: pd.DataFrame, i: int, ctx: BarContext, contexts: list[BarContext]) -> VpaSignal | None:
    """Ch6's braking sequence inside a fast move DOWN: successive bars with
    deep lower wicks and progressively NARROWING bodies on RISING volume,
    terminating in a hammer. "It takes two or three sessions for the brakes
    to be applied."

    This is the precursor to a buying climax, and is a genuinely multi-bar
    signal — a single deep-wicked bar is not stopping volume.
    """
    if i < STOPPING_MIN_BARS or ctx.trend != "down":
        return None
    window = range(i - STOPPING_MIN_BARS + 1, i + 1)
    cs = [contexts[j] for j in window]
    if any(c.lower_wick_frac is None or c.body_frac is None for c in cs):
        return None
    if not all(c.lower_wick_frac >= 0.3 for c in cs):
        return None
    bodies = [c.body_frac for c in cs]
    if not all(bodies[k] >= bodies[k + 1] for k in range(len(bodies) - 1)):
        return None  # bodies must narrow (or hold), never widen
    vols = df["volume"].iloc[i - STOPPING_MIN_BARS + 1: i + 1].tolist()
    if not all(vols[k] <= vols[k + 1] for k in range(len(vols) - 1)):
        return None
    if not _is_hammer(ctx):
        return None
    return VpaSignal(
        "stopping_volume", i, "bullish", "validation", 0.6,
        "stopping volume — deep lower wicks on rising volume with narrowing bodies, terminating "
        "in a hammer; insiders applying the brakes to a fast move lower",
        span=STOPPING_MIN_BARS,
    )


def topping_out_volume(df: pd.DataFrame, i: int, ctx: BarContext, contexts: list[BarContext]) -> VpaSignal | None:
    """The mirror at the top of a bullish run: deep upper wicks, narrowing
    spreads, high and rising volume, terminating in a shooting star. Ch6
    identifies this sequence as the distribution phase itself."""
    if i < STOPPING_MIN_BARS or ctx.trend != "up":
        return None
    window = range(i - STOPPING_MIN_BARS + 1, i + 1)
    cs = [contexts[j] for j in window]
    if any(c.upper_wick_frac is None or c.body_frac is None for c in cs):
        return None
    if not all(c.upper_wick_frac >= 0.3 for c in cs):
        return None
    bodies = [c.body_frac for c in cs]
    if not all(bodies[k] >= bodies[k + 1] for k in range(len(bodies) - 1)):
        return None
    vols = df["volume"].iloc[i - STOPPING_MIN_BARS + 1: i + 1].tolist()
    if not all(vols[k] <= vols[k + 1] for k in range(len(vols) - 1)):
        return None
    if not _is_shooting_star(ctx):
        return None
    return VpaSignal(
        "topping_out_volume", i, "bearish", "validation", 0.6,
        "topping out volume — deep upper wicks on high rising volume with narrowing spreads, "
        "terminating in a shooting star; this is the distribution phase",
        span=STOPPING_MIN_BARS,
    )


CLIMAX_WINDOW = 5
CLIMAX_MIN_OCCURRENCES = 2


def selling_climax(df: pd.DataFrame, i: int, ctx: BarContext, contexts: list[BarContext]) -> VpaSignal | None:
    """NOTE THE NAMING: per Coulling this occurs at the TOP of a bullish
    trend, not the bottom — "a selling climax appears at the top of the
    bullish trend... many people refer to these events the other way round!"
    It is the insiders' final inventory clearance.

    Repetition is DEFINITIONAL, not a strengthener: "This happens two or
    three times on high volume", because "it takes time to sell large
    volumes quickly". A single deep-upper-wick bar is never a climax."""
    if ctx.trend != "up" or i < CLIMAX_WINDOW:
        return None
    count = 0
    for j in range(i - CLIMAX_WINDOW + 1, i + 1):
        c = contexts[j]
        if c.volume_grade is None or c.body_frac is None or c.upper_wick_frac is None:
            continue
        if c.body_frac <= NARROW_BODY and c.upper_wick_frac >= DEEP_WICK and c.volume_grade >= VolumeGrade.HIGH:
            count += 1
    if count < CLIMAX_MIN_OCCURRENCES:
        return None
    return VpaSignal(
        "selling_climax", i, "bearish", "validation", min(0.5 + 0.15 * count, 0.9),
        f"selling climax — {count} narrow-body/deep-upper-wick bars on high volume at the top of "
        "an up move; the insiders' last hurrah, the warehouse is being emptied",
        span=CLIMAX_WINDOW,
    )


def buying_climax(df: pd.DataFrame, i: int, ctx: BarContext, contexts: list[BarContext]) -> VpaSignal | None:
    """The mirror, at the BOTTOM of a bearish trend — public capitulation
    while insiders accumulate."""
    if ctx.trend != "down" or i < CLIMAX_WINDOW:
        return None
    count = 0
    for j in range(i - CLIMAX_WINDOW + 1, i + 1):
        c = contexts[j]
        if c.volume_grade is None or c.body_frac is None or c.lower_wick_frac is None:
            continue
        if c.body_frac <= NARROW_BODY and c.lower_wick_frac >= DEEP_WICK and c.volume_grade >= VolumeGrade.HIGH:
            count += 1
    if count < CLIMAX_MIN_OCCURRENCES:
        return None
    return VpaSignal(
        "buying_climax", i, "bullish", "validation", min(0.5 + 0.15 * count, 0.9),
        f"buying climax — {count} narrow-body/deep-lower-wick bars on high volume at the bottom of "
        "a down move; public capitulation into insider buying",
        span=CLIMAX_WINDOW,
    )


def test_of_supply(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """Ch5's test: price dips back into a region of recent heavy selling and
    finds none left. "The precise formation of the candle is not critical,
    but the body must be a narrow spread, with a deep lower wick. The colour
    of the body can be either bullish or bearish."

    Volume decides pass/fail and the two outcomes are opposite signals, so
    both are emitted. Requires a congestion zone to test INTO — Ch5 is
    explicit that a test without a preceding accumulation/distribution phase
    is not a test at all."""
    if ctx.zone is None or ctx.volume_grade is None:
        return None
    if ctx.body_frac is None or ctx.lower_wick_frac is None:
        return None
    if not (ctx.body_frac <= NARROW_BODY and ctx.lower_wick_frac >= DEEP_WICK):
        return None
    if ctx.volume_grade <= VolumeGrade.LOW:
        return VpaSignal(
            "test_of_supply_passed", i, "bullish", "validation", 0.65,
            "test of supply PASSED on low volume — price marked down into old selling and no "
            "sellers appeared; one of the most powerful signals in the method",
        )
    if ctx.volume_grade >= VolumeGrade.HIGH:
        return VpaSignal(
            "test_of_supply_failed", i, "bearish", "anomaly", 0.5,
            "test of supply FAILED on high volume — sellers returned in numbers; the mark-up is "
            "postponed and the market must be taken back into congestion to flush them out",
        )
    return None


def test_of_demand(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """The mirror: a rally back into a region of recent heavy buying that
    finds no demand left."""
    if ctx.zone is None or ctx.volume_grade is None:
        return None
    if ctx.body_frac is None or ctx.upper_wick_frac is None:
        return None
    if not (ctx.body_frac <= NARROW_BODY and ctx.upper_wick_frac >= DEEP_WICK):
        return None
    if ctx.volume_grade <= VolumeGrade.LOW:
        return VpaSignal(
            "test_of_demand_passed", i, "bearish", "validation", 0.65,
            "test of demand PASSED on low volume — price marked up into old buying and no buyers "
            "appeared; the insiders are clear to move the market lower, fast",
        )
    if ctx.volume_grade >= VolumeGrade.HIGH:
        return VpaSignal(
            "test_of_demand_failed", i, "bullish", "anomaly", 0.5,
            "test of demand FAILED on high volume — buyers flooded in; the mark-down is halted",
        )
    return None


ANOMALY_SEQUENCE_BARS = 3


def rising_price_falling_volume(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """Ch4's Fig 4.16 sequence, distilled: "Rising markets should be
    associated with rising volume, NOT falling volume." Three consecutive
    higher closes on three consecutive lower volumes is the canonical
    distribution warning — insiders have withdrawn and are marking prices up
    on no participation to trap buyers."""
    if i < ANOMALY_SEQUENCE_BARS:
        return None
    closes = df["close"].iloc[i - ANOMALY_SEQUENCE_BARS: i + 1].tolist()
    vols = df["volume"].iloc[i - ANOMALY_SEQUENCE_BARS: i + 1].tolist()
    rising_price = all(closes[k] < closes[k + 1] for k in range(len(closes) - 1))
    falling_vol = all(vols[k] > vols[k + 1] for k in range(len(vols) - 1))
    if rising_price and falling_vol:
        return VpaSignal(
            "rising_price_falling_volume", i, "bearish", "anomaly", 0.55,
            "rising price on falling volume — the move is unsupported; insiders have withdrawn "
            "and are marking prices higher to trap traders into weak positions",
            span=ANOMALY_SEQUENCE_BARS + 1,
        )
    return None


def falling_price_falling_volume(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """The mirror, Fig 4.17: "in a falling market we expect to see rising
    volume, NOT falling volume." Selling pressure draining out of a decline."""
    if i < ANOMALY_SEQUENCE_BARS:
        return None
    closes = df["close"].iloc[i - ANOMALY_SEQUENCE_BARS: i + 1].tolist()
    vols = df["volume"].iloc[i - ANOMALY_SEQUENCE_BARS: i + 1].tolist()
    falling_price = all(closes[k] > closes[k + 1] for k in range(len(closes) - 1))
    falling_vol = all(vols[k] > vols[k + 1] for k in range(len(vols) - 1))
    if falling_price and falling_vol:
        return VpaSignal(
            "falling_price_falling_volume", i, "bullish", "anomaly", 0.55,
            "falling price on falling volume — selling pressure is draining away; the decline is "
            "no longer supported by genuine participation",
            span=ANOMALY_SEQUENCE_BARS + 1,
        )
    return None


def trend_validated(df: pd.DataFrame, i: int, ctx: BarContext) -> VpaSignal | None:
    """Ch4's Fig 4.14/4.15: consecutive same-direction bars with widening
    spread AND rising volume. "Rising prices = rising volume." This is the
    low-risk continuation state, not a reversal signal.

    The wick condition is load-bearing and is why this checks it: only
    wick-less bars license the book's attribution "it can only be buying
    volume" — a long wick means the other side was present."""
    if i < 2:
        return None
    seg = df.iloc[i - 2: i + 1]
    spreads = (seg["high"] - seg["low"]).tolist()
    vols = seg["volume"].tolist()
    closes = seg["close"].tolist()
    opens = seg["open"].tolist()
    widening = all(spreads[k] < spreads[k + 1] for k in range(len(spreads) - 1))
    rising_vol = all(vols[k] < vols[k + 1] for k in range(len(vols) - 1))
    if not (widening and rising_vol):
        return None
    all_up = all(c > o for c, o in zip(closes, opens))
    all_down = all(c < o for c, o in zip(closes, opens))
    if not (all_up or all_down):
        return None
    return VpaSignal(
        "trend_validated", i, "bullish" if all_up else "bearish", "validation", 0.5,
        f"{'rising' if all_up else 'falling'} price on rising volume with widening spread — "
        "the move is genuine and insiders are participating; a low-risk continuation",
        span=3,
    )


# --------------------------------------------------------------------------
# Tier 5 — structural events (Ch7)
# --------------------------------------------------------------------------

def breakout(df: pd.DataFrame, i: int, ctx: BarContext, zone: CongestionZone | None, atr_val: float | None) -> VpaSignal | None:
    """Ch7's breakout, with its two required conditions.

    (1) "Clear water": a CLOSE beyond the boundary, not a tick — "if the
        market ticks a few points above or below, this is NOT in itself a
        signal". The book refuses to give a distance ("there is no hard and
        fast rule"), so an ATR-relative default stands in.
    (2) Volume: "the initial move higher up and through the ceiling level has
        to be accompanied by strong and rising volume".

    Condition 1 without condition 2 is the FAKE-OUT — "a break out from such
    a price area on low volume is a classic trap move by the insiders" — and
    is emitted as its own opposite-bias signal rather than as silence.
    """
    if zone is None or atr_val is None or atr_val <= 0 or ctx.volume_grade is None:
        return None
    if i < 1:
        return None
    close = df["close"].iloc[i]
    clear = atr_val * 0.5
    above = close > zone.ceiling + clear
    below = close < zone.floor - clear
    if not (above or below):
        return None
    # Fire only on the BAR THAT CROSSES. A breakout is an event, not a
    # state: without this, every subsequent bar that merely remains outside
    # the range re-reports the same break, flooding the evidence score with
    # dozens of duplicates of a single structural event.
    prev_close = df["close"].iloc[i - 1]
    already_out = (prev_close > zone.ceiling + clear) if above else (prev_close < zone.floor - clear)
    if already_out:
        return None
    strong = ctx.volume_grade >= VolumeGrade.ABOVE_AVERAGE
    direction = "bullish" if above else "bearish"
    if strong:
        return VpaSignal(
            "breakout_confirmed", i, direction, "validation", 0.65,
            f"confirmed breakout {'above' if above else 'below'} a congestion zone on "
            "above-average volume, with clear water on the close",
        )
    return VpaSignal(
        "breakout_fake", i, "bearish" if above else "bullish", "anomaly", 0.55,
        f"FAKE breakout {'above' if above else 'below'} congestion — clear water on the close but "
        "no volume behind it; a classic insider trap, expect price back inside the range",
    )
