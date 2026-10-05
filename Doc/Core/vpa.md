# Core: VPA (Volume Price Analysis)

A faithful implementation of the method in Anna Coulling's *A Complete
Guide To Volume Price Analysis* (2013), the copy in
`KnowledgeBase/Trading/`. Code: `Core/vpa/`.

## The thesis in one line

Volume is effort, price movement is result; when the two agree the move is
genuine and continues, and when they disagree something is being hidden and
a change is coming. Coulling's own framing: *"we are only ever looking for
two things. Whether the price has been validated by the volume, or whether
there is an anomaly with the price... Validation or anomaly. Nothing else."*

## Module map

| Module | Book source | What it does |
|---|---|---|
| `volume.py` | Ch4 | The graded volume ladder (low → very high) as a **percentile rank** against the instrument's own trailing history |
| `spread.py` | Ch4, Ch12 | Candle spread (high−low, explicitly *not* open−close) graded against ATR; close position within range |
| `structure.py` | Ch7, Ch8 | Isolated pivots, dynamic trend, congestion zones, support/resistance levels |
| `signals.py` | Ch4–Ch7 | The signal catalogue — 20+ detectors across 5 tiers |
| `vap.py` | Ch9 | Volume-at-price histogram, high/low volume nodes, time-normalised intensity |
| `analysis.py` | Ch10, Ch11 | The integrated read, setup construction, the three-timeframe frame |
| `positions.py` | — | VPA's own $200 wallet (this project's usual convention — the book gives no capital figure) and risk gates, on `Core.Library.positions`'s generic store |
| `daemon.py` | — | The live loop: scans a market-cap-filtered universe on the 15M/1H/4H frame, opens/manages/closes paper positions, pushes Telegram alerts |

## The signal catalogue

**Tier 1 — the master classifier.** `effort_vs_result`: Wyckoff's third
law as a bar-over-bar sign agreement between Δspread and Δvolume. Runs on
every bar and emits validation *and* anomaly readings, because a run of
validations is what licenses "this trend is genuine."

**Tier 2 — the spread × volume matrix (Ch4, Figs 4.10–4.13).** All four
cells: wide+high and narrow+low are validations; wide+low is a trap
("a big result from little effort"); narrow+high is absorption. The last
one flips meaning with trend position — bearish at a top, bullish at a
bottom — which is the clearest case in the book of why context is a
required input rather than a refinement.

**Tier 3 — the premier candles (Ch6).** Shooting star, hammer, hanging
man, long-legged doji. Volume *grades* the shooting star ("there is never
an anomaly with a shooting star, only ever a validation") but is a hard
*requirement* for the hanging man. The long-legged doji is the one premier
candle with a genuine volume anomaly: on low volume it inverts from
reversal signal to stop-hunt warning, and the correct response is to stand
aside.

**Tier 4 — multi-bar campaigns (Ch5, Ch6).** Stopping volume, topping-out
volume, selling/buying climax, the four test variants, and the
rising-price-falling-volume anomaly sequences.

**Tier 5 — structural events (Ch7).** Breakout (clear-water close *plus*
volume) and its inverse, the fake-out (clear water, no volume) — which
carries the *opposite* bias to the break direction.

## Two conventions that cause sign errors if missed

1. **Coulling inverts the climax naming, and says so explicitly**: *"a
   selling climax appears at the top of the bullish trend, whilst the buying
   climax appears at the bottom of a bearish trend... many people refer to
   these events the other way round!"* Implemented her way, and pinned by
   `test_climax_naming_follows_the_book_not_convention`.
2. **A signal's bias is about future price, not candle colour.** A
   narrow-spread *up* bar on high volume is bearish. An up bar on low
   volume is bearish. Tested explicitly.

## How the pieces combine — and why it scores rather than gates

Ch11's precedence, implemented literally: context → anomalies (primary) →
candles (corroborating only, never primary) → next-bar confirmation → the
low-volume test as the highest-quality trigger. VAP modulates confidence,
size and stop placement only. Multi-timeframe modulates risk and holding
period only.

**The critical architectural decision** is that `analysis.py` accumulates
*weighted evidence* rather than AND-ing filters. The obvious reading of
"use all the concepts in the book" is to require them all simultaneously.
Polar's predecessor built exactly that and measured it out-of-sample across
two independent windows; that codebase's `DECISIONS.md` records the result
plainly: the full AND-stack got **monotonically worse with each layer
added** (39.1% → 36.6% → 31.8% win rate; +9.51% → −15.13% return), and VPA
effort-vs-result used as a hard entry filter *"helped window 2, gutted
window 1."*

The failure is structural, not a tuning problem: each condition is
individually noisy, so ANDing them multiplies noise into the selection
while shrinking the sample toward nothing. Evidence-scoring means adding a
detector adds information instead of subtracting sample. Only two hard
gates remain, and both are places the book itself states a precondition: a
test requires a prior congestion phase, and a climax requires repetition.

## Trade construction (Ch11)

- **Stops are always market-derived**, never a fixed percentage, in the
  book's own priority order: opposite side of the congestion zone → last
  pivot low/high → beyond the signal candle's wick. Each is *"the market
  setting this level for us."*
- **Cause and effect sizes the expectation**: `expected_bars` comes from
  the congestion zone's duration, since *"the longer a market consolidates
  in a narrow range, then the more dramatic the resulting price action."*
- **Counter-trend trades are classified, not forbidden.** Ch8 permits them
  but requires they be recognised as higher-risk and shorter-held, so they
  get a halved expected duration rather than a veto.
- **Confirmation defaults to on** (Principle No 2: never act on the signal
  bar). `require_confirmation=False` is Ch10's "aggressive" fork, which the
  book presents honestly as roughly break-even.

## Going live — the daemon (`daemon.py`)

**Universe**: every Bitget USDT spot pair with a CoinGecko market cap above
$50M, excluding meme/joke coins and stablecoin/gold-pegged tokens — Lorenzo's
own instruction ("The Core must operate on >10mln market cap crypto pairs",
later revised to ">50mln, avoid meme coins/meme token"), not a book
requirement (Coulling's method is instrument-agnostic; the universe
constraint is this project's own risk preference). Implemented via
`Core.Library.candlestick_trend.backtest.top_n_crypto_usdt_pairs(n=None,
min_market_cap=50_000_000, exclude_meme_coins=True)` — see
Doc/Library/shared_infrastructure.md for the exclusion mechanics. This is
CoinGecko-rate-limited and slow (minutes), so it's cached to disk
(`data/universe_cache.json`) and refreshed once a day, not every poll.

**Timeframes**: `analyse_multi_timeframe`'s own design is used directly —
only the FAST chart (15M) is fetched from the exchange; 1H (middle,
"my primary trading chart") and 4H (slow, sets the bias) are resampled from
it internally, so all three frames stay perfectly aligned rather than
drifting from three independent fetches. Setups are on the 1H chart, as
`build_setups()` already resolves.

**Exit rule — no invented take-profit.** A position closes on whichever
comes first:
1. **Stop hit** — `VpaSetup.stop_price`, the book's own market-derived level.
2. **Cause/effect duration expires** — Ch11's `expected_bars` (a congestion
   zone's size setting how long to expect the move to run) is a HOLDING
   PERIOD expectation, not a price target (the doc already says so:
   "Multi-timeframe modulates risk and holding period only"). Using it as a
   timed exit is the direct, non-invented reading of that number, not a
   fabricated formula. `Position.expires_at` (a new, Core-agnostic field on
   the shared store — see `Core.Library.positions`) holds this deadline;
   `daemon.py`'s `check_open_positions` closes at market on expiry.

No fixed TP is ever set (`Position.tp = None` for every VPA position) —
inventing one the book doesn't supply would be worse than being honest that
there isn't one. `Core/weekend_hunter/telegram_bot.py`'s `_tp_sl_row` renders
this case explicitly ("no fixed target") rather than showing a fabricated
number.

**Sizing**: risk-based, not fixed-dollar (unlike Weekend Hunter, which
matches its Pine source's absolute-dollar sizing). `VpaSetup.risk_fraction`
is exactly the input the book leaves for a position sizer to use ("you will
have to judge the risk on the trade") — `positions.position_size_usd()`
risks a fixed 2% of the $200 wallet per trade via that fraction, capped at
50% of available capital so a very tight stop can't imply an absurd size.
No leverage (1x always) — the book never discusses leveraged trading.

**Scan cost**: `isolated_pivots`/`congestion_zones` are per-bar Python loops,
not vectorised, so a full multi-timeframe read costs ~4s/symbol measured on
real hardware — threading barely helps (GIL-bound), so the daemon uses a
process pool (`SCAN_WORKERS = min(4, os.cpu_count())`) to keep a ~190-pair
scan well under the 15-minute poll interval.

**Telegram**: `Core/weekend_hunter/telegram_bot.py` (kept in place rather
than moved — it already anticipated this exact extension in its own prior
docstring) now reads both Cores' stores. `/positions` and `/report` are
combined views; `/vpa` is the VPA-only view (mirroring `/weekendhunter`);
`/pending` reads `Core/vpa/data/pending.json`, written every scan cycle —
a "pending" entry is the final middle-timeframe bar's own read: it has a
qualifying bias/conviction but, being the last bar, has no next bar yet to
confirm it (Principle No 2 — never act on the signal bar itself).

## Verification

`Core/vpa/tests/test_vpa.py` — 29 tests. The load-bearing one is
`test_analysis_is_causal`: re-running on a truncated prefix must reproduce
an identical prefix of reads. Verified non-vacuous (246 signals across 179
bars exercised) and verified to actually detect violations (perturbing a
future bar changes zero past reads). This project has already shipped one
look-ahead bug — the forming-bar leak — so causality is checked
mechanically, not by inspection.

`Core/vpa/tests/test_daemon.py` — 14 tests covering `positions.py`'s risk
gates and sizing math, plus the pure scan-result helpers that decide what a
live cycle acts on: `_latest_setup` (only the just-closed bar's setup is
actionable — an older one's entry price has already passed) and
`_update_pending` (records/clears the final bar's own unconfirmed read).

Also dry-run against live data: `top_n_crypto_usdt_pairs` benchmarked at
~4s/symbol for a full 15M/1H/4H read (measured on real Bitget data), full
`run_once()` exercised end-to-end against a small real-symbol sample with a
fake notifier (no live Telegram sends) with zero exceptions, and the
combined `/positions`/`/report`/`/vpa`/`/pending` Telegram handlers
smoke-tested against isolated temp stores covering both a Weekend-Hunter-style
position (fixed TP/SL) and a VPA one (no TP, `expires_at` set) rendering
correctly side by side.

## Calibration status — read before trusting any number

**Nothing here has been backtested.** The module is verified to implement
the book correctly and to run correctly on real data; whether Coulling's
method has an edge on crypto is a separate and completely open question.

Every numeric threshold is a **reasoned default, not a book value**. The
book supplies essentially no numbers — Coulling grades volume and spread by
eye and argues explicitly that the method resists automation: *"it is not a
technique that lends itself easily to automation... A software program does
not have any subjectivity in its decision making. Hence it can never
work."* That is a stated accuracy ceiling, not a reason not to build, but
it does mean the thresholds below are the single largest source of
uncertainty:

| Constant | Value | Where |
|---|---|---|
| Volume ladder cutoffs | 25 / 60 / 85 / 95 percentile | `volume.py` |
| Spread wide / narrow | 1.3 / 0.6 × ATR | `spread.py` |
| Narrow body / deep wick | 0.30 / 0.45 of range | `signals.py` |
| Level tolerance | 0.25 × ATR | `structure.py` |
| Breakout clear water | 0.5 × ATR | `structure.py` |
| Min congestion span | 10 bars | `structure.py` |
| Tier weights | anomaly 1.0, candle 0.5 | `analysis.py` |

Percentile ranking (rather than a multiple of a moving average) is a
deliberate choice for the volume ladder: it self-normalises across regimes,
and it is robust to the distortion Ch10's Coffee example warns about, where
a few extreme bars wreck a mean-based baseline.

## Known gaps

- **Delta volume is not implemented.** Ch12 describes bid/ask delta as the
  next generation, and unlike the book's forex examples, crypto venues
  *do* expose per-trade aggressor side. That would turn the book's central
  inference — *"if this were buying volume then the market would be rising
  fast; it isn't, so it must be selling volume"* — from an inference into a
  direct measurement. The single highest-value extension available.
- **Selectivity is high**: ~2 setups per 800 hourly bars on BTC at the
  default `min_conviction=0.35`. That is arguably right for a method whose
  own worked example is a single trade, but it is a tunable, not a law.
  Confirmed live at daemon scale too (0 setups across a 10-symbol sample in
  one scan cycle during dry-run verification) — not a bug, just the method's
  own selectivity.
- No target-price formula, only a market-derived stop and a cause/effect
  holding-period expectation (the book declines to give one — *"you will
  have to judge the risk on the trade"*). `daemon.py` treats this honestly
  rather than inventing a TP: see "Going live" above.
