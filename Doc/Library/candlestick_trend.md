# Core: candlestick_trend

## What it is

A self-contained (pandas-only) module that detects classic candlestick
patterns and grades each one's confidence against the trend that preceded
it, producing long/short signals. Code: `Core/Library/candlestick_trend/`.

## How it works

1. **`geometry.py`** — OHLC primitives (body/wick/tail length, engulfment,
   gaps). Ported from [cm45t3r/candlestick](https://github.com/cm45t3r/candlestick)
   (MIT) via a prior port already done in Polar's previous codebase.

2. **`patterns.py`** — 23 single/two/three-candle detectors (Doji, Hammer
   family, Marubozu, Spinning Top, Engulfing, Harami, Piercing Line, Dark
   Cloud Cover, Tweezers, Kicker, Morning/Evening Star, Three White
   Soldiers/Black Crows). Each detector answers "did this complete on the
   last bar of this window?" `scan()` walks the full series to answer that
   at every bar. The hammer family (Hammer/Hanging Man/Inverted
   Hammer/Shooting Star) additionally requires next-bar confirmation — the
   shape alone is a setup, not a signal — and uses a binary EMA(50)
   trend read purely to decide pattern *identity* (the same shape is a
   different pattern depending on what preceded it).

2b. **`patterns_talib.py`** — the rest of the TA-Lib `CDL*` catalog: ~45
   more patterns (Two Crows, Three Inside/Outside, Abandoned Baby, Advance
   Block, Belt Hold, Breakaway, Closing Marubozu, Counterattack, Doji
   Star, Dragonfly/Gravestone Doji, Gap/Tasuki/Side-by-Side-Gap
   continuation patterns, Harami Cross, High Wave, Hikkake (+ modified),
   Homing Pigeon, Identical Three Crows, In-Neck/On-Neck/Thrusting,
   Kicking By Length, Ladder Bottom, Long-Legged Doji, Long/Short Line,
   Matching Low, Mat Hold, Morning/Evening Doji Star, Rickshaw Man,
   Rise/Fall Three Methods, Separating Lines, Stalled Pattern, Stick
   Sandwich, Takuri, Tri-Star, Unique Three River), ported from
   [pandas_ta_classic](https://github.com/xgboosted/pandas-ta-classic)
   (MIT) — a pure-Python, Numba-accelerated reimplementation of TA-Lib's
   exact `CDL*` algorithms. Kept in its own module rather than merged into
   `patterns.py`, mirroring how the old codebase split `candlesticks.py`
   (cm45t3r-derived) from `candlesticks_talib.py` (TA-Lib-derived) — same
   reasoning: different source, different thresholding philosophy.

   TA-Lib's patterns compare a candle's body/shadow against an **adaptive
   trailing average** of that measure over the preceding N bars (10 for
   body settings, 5 for near/far/equal, or the bar's own value when N=0),
   not a fixed ratio of that same candle's own range — this is genuinely
   different from `patterns.py`'s style and is *why* this source was
   chosen over hand-picking more thresholds. That averaging logic lives in
   **`talib_thresholds.py`**, cached per `(id(df), setting)` and cleared at
   the start of every `scan()` — recomputing it from scratch at every bar
   was the dominant cost (~80s for 300 bars before caching; ~1s after).

   Patterns that can fire either direction (e.g. Three Inside, Belt Hold,
   Harami Cross) are split into `_bullish`/`_bearish` pairs, matching the
   existing `bullish_X`/`bearish_X` convention — `PATTERN_META` fixes one
   direction per pattern *name*, so a dual-direction detector can't be
   registered as a single entry. Two things NOT ported from TA-Lib's full
   catalog: `CDL_INSIDE` (a generic bar-containment flag, not a Nison
   reversal/continuation pattern) and `CDLKICKING` (a stricter, marubozu-
   shaped variant of what `patterns.py` already has as
   `bullish_kicker`/`bearish_kicker` — same market event; `kickingbylength`
   *is* ported since it's a meaningfully distinct variant).

3. **`trend.py`** — a continuous, signed trend measure (default: one-step
   % slope of a 10-period SMA of closes, `cm45t3r`'s own default method).
   This is separate from the binary identity gate above: it exists to
   *grade* confidence, not just gate pattern identity, so it needs a
   magnitude, not just a direction.

4. **`metadata.py`** — per-pattern type (`reversal` / `continuation` /
   `neutral`), direction, and base confidence.

5. **`signal.py`** — the actual decision layer: for a reversal pattern, the
   *expected* preceding trend is the opposite of its signal direction (a
   Hammer expects to have been preceded by a downtrend); for a continuation
   pattern, expected trend matches signal direction. `context_fit` scores
   how well the measured trend matches that expectation
   (`0.5 + 5 * expected_direction * measured_trend`, clamped to `[0, 1]`),
   and `effective_confidence = base_confidence * context_fit`. A signal
   fires only once `effective_confidence` clears a threshold (default
   `0.4` — `context_fit` maxes at 1.0, so a higher default would make
   patterns with base confidence below it mathematically unreachable
   regardless of trend strength; see `signal.py`'s comment).

## The strategy, in one line

Measure the trend, then trade patterns whose own definition is consistent
with that trend — continuation patterns *with* it, reversal patterns
*against* it — weighted by how strongly the measured trend matches what
each pattern expects to have preceded it. A strong, well-established trend
doesn't just permit trend-following patterns — it's exactly what maximizes
confidence in a *reversal* pattern signaling that trend is about to break.

## Pros

- Reuses three independently-built, already-vetted implementations
  (`cm45t3r/candlestick`, Polar's own prior port + calibration fixes, and
  `pandas_ta_classic`'s TA-Lib-parity port) rather than inventing pattern
  thresholds from scratch — 68 patterns total (23 + 45).
- Correctly distinguishes reversal from continuation expectations, instead
  of the naive "trade bullish patterns in a bullish trend" rule (which is
  backwards for most of the catalog — most classic candlestick patterns
  are reversal patterns).
- Graduated confidence (not a binary trend gate) means a pattern isn't
  simply allowed/disallowed — its confidence scales with how strong and
  well-aligned the trend actually is.
- No platform/exchange dependency — takes a plain OHLCV DataFrame, usable
  standalone in a backtest or wired into a live daemon later.

## Cons / open questions

- `sensitivity` (default `150.0`, not cm45t3r's own default of `5.0`) and
  `confidence_threshold` (default `0.4`) are empirically sanity-checked, not
  really calibrated. Against 1000 hours of real BTC/USDT 1h data,
  `sensitivity=5.0` (the JS library's own default) left `context_fit` within
  ~0.01 of neutral for almost every hit, because real hourly crypto trend
  magnitudes (sma-slope period 10) rarely exceed ~0.005 — far smaller than
  whatever price series cm45t3r's own default was tuned against. `150.0`
  was picked by sweeping 50-300 and choosing the point giving a believable
  signal frequency (~1/day) with a real confidence spread — **not** a
  real calibration against actual trade outcomes the way the old
  codebase's binary `_prior_trend` gates were (those came from live
  calibration data). This tuning is also coupled to
  `trend_method="sma-slope"` + `trend_period=10` specifically — changing
  either invalidates it and it would need to be redone.
- `type` (reversal/continuation/neutral) per pattern follows standard
  Nison candlestick theory, encoded here — it is not a verbatim copy of
  `cm45t3r/candlestick`'s own internal type table (that wasn't fully
  captured during research) or of `pandas_ta_classic`'s (which doesn't
  supply type/direction/confidence metadata at all — TA-Lib's raw output
  is just ±100/0), so a number of borderline calls (e.g. Marubozu/Closing
  Marubozu/Long Line as continuation, High Wave/Long-Legged Doji/Rickshaw
  Man/Short Line as neutral despite TA-Lib tagging some of these with a
  raw color-based direction) are judgment calls worth revisiting.
- No position sizing, stop-loss, or execution model yet — this module only
  emits `long`/`short` signals with a confidence score.
- Backtested for signal quality (forward returns + MFE/MAE, no trade-
  management assumptions — see `backtest.py` and its module docstring for
  why) across the top 15 USDT pairs (mcap > $10M) on Bitget, 4 timeframes,
  7-day windows. Not yet a P&L-validated system — that requires deciding
  on (and calibrating) an actual exit rule, which two prior attempts
  (fixed-bar hold, then 2:1 structural TP/SL) showed isn't settled yet.
