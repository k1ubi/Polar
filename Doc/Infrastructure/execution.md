# Live Execution — Bitget USDT-M Futures

How a Weekend Hunter signal becomes a real order, and every place that path
can refuse to act.

Before this existed, `Core/weekend_hunter/daemon.py` was signal-only: it
scored signals, wrote simulated positions to `data/positions.json`, and
pushed Telegram alerts that said *"no order placed."* That behaviour is
still the default and still what runs unless `POLAR_LIVE_TRADING` is
explicitly set.

## Layout

| File | Role |
|---|---|
| `Core/Library/broker.py` | Venue-agnostic interface. No HTTP, no Bitget vocabulary. |
| `Core/Library/bitget_futures.py` | The Bitget USDT-M implementation: v2 signing, contract specs, sizing, orders. |
| `Core/weekend_hunter/execution.py` | Mode gating, kill switch, reconciliation. The safety boundary. |
| `Core/weekend_hunter/daemon.py` | Unchanged responsibility — decides *what* to trade, not whether it may. |

The split exists for the same reason the rest of Polar is split: the venue
should be swappable without touching the strategy, exactly as the strategy
is swappable without touching the infrastructure.

## Why futures, not spot

The strategy emits `bearish` signals and a dynamic 5–20x leverage.
Bitget spot can express neither. All nine pairs were verified present on
`USDT-FUTURES` with `symbolStatus == "normal"` and max leverage between 50x
and 150x — comfortably above the strategy's 20x ceiling.

Note that market data still comes from Bitget's public **spot** endpoints
(`candlestick_trend.backtest.fetch_ohlcv`). Spot and perpetual prices track
each other closely but are not identical; signals are computed on spot
candles and executed against the perpetual mark price.

## Sizing: `$120` is margin, not exposure

This is the decision the whole system hinges on, and it was previously
ambiguous — `daemon.py` labelled positions `"(unleveraged)"` while
computing PnL as `leverage * raw_move`.

**Resolved: `position_size_usd()` returns the MARGIN posted. Real exposure
is `margin × final_leverage`.**

That is the interpretation the code already computed and, more importantly,
the one every risk gate in `positions.py` is dimensionally calibrated to.
Four concurrent stop-outs at 12x is roughly −$28, which trips the 5% daily
loss limit on a $500 account exactly as designed. Under a 1x reading the
same event is −$2.33, and the daily limit, the weekend limit and the 30%
emergency stop would effectively never fire.

Telegram alerts now show margin, leverage **and** notional exposure on
separate lines, because the margin figure alone is what made this ambiguous
in the first place.

## Capital scaling

The Pine source sizes in **absolute dollars** — `$120` base position, `$120`
min capital per trade, `$50` safety buffer — which silently assumes the
`initial_capital=500` it was written against.

On a smaller account those constants do not merely shrink returns, they
break the strategy outright. At 163 USDT, `can_afford_position()` requires
`120 + 50 = 170` and refuses **every** signal: the daemon runs forever and
never opens a position.

`positions.capital_scale()` rescales them by `initial_capital / 500`,
preserving the source's proportions (24% of capital per position, 10%
buffer) at whatever size the account actually is:

| | Pine @ 500 | scaled @ 163 |
|---|---|---|
| base position (margin) | 120.00 | 39.12 |
| min capital per trade | 120.00 | 39.12 |
| safety buffer | 50.00 | 16.30 |
| `can_afford` threshold | 170.00 | 55.42 |
| daily loss limit | 25.00 | 8.15 |
| emergency stop below | 350.00 | 114.10 |

The scale is **1.0 on paper** (`WALLET_USDT` is 500), so the paper track and
every pre-existing test are bit-for-bit unchanged. Only a live account of a
different size moves it.

It follows the *frozen* baseline, not current equity, so position size does
not ratchet up during a winning streak — matching the Pine constants it
replaces. **If you materially change the account's funding, delete
`data/live_baseline.json`** to re-baseline; it is never overwritten
automatically, because re-baselining on a drawn-down account would reset
the emergency stop and hand the strategy a fresh 30% to lose.

Note that the capital gates self-limit to **3 concurrent positions** at
maximum size rather than `MAX_CONCURRENT_POSITIONS = 4`. That is not a
scaling artifact — the Pine source does the same at 500 (4 × 156 = 624 >
500).

## The stop lives at the exchange

The single most important property here.

The daemon polls every `POLL_SECONDS = 300`. Weekend Hunter's ATR stops sit
**0.27%–0.59%** from entry on the 15m chart (measured live across all nine
pairs). Price can cross that distance entirely between two polls.

So every entry order carries `presetStopLossPrice` and
`presetStopSurplusPrice`. Bitget enforces both in real time. The poll loop's
job for a live position is to *discover* what the venue already did, not to
decide it:

```
signal ──► place-order (market, +SL, +TP attached) ──► venue holds protection
                                                              │
   every 300s: get_position() ──► flat? ──► history-position ──► record REAL pnl
```

Consequences worth internalising:

- **TP/SL are never modeled for a live position.** The recorded exit price
  and PnL come from Bitget's `netProfit`, which is already net of the fees
  it actually charged. `TAKER_FEE_PCT`/`SLIPPAGE_PCT` are *not* applied on
  top — that would double-count.
- **Trailing stops are pushed to the venue** (`pos_loss` plan order) each
  time the local ratchet moves. A trailing stop that exists only in the poll
  loop protects nothing for the 300s until the next pass.
- **Only the strategy's own non-price exits** (weekend-end, flash crash,
  emergency stop, liquidation proximity) still close positions from the
  daemon side.

Paper positions — anything with `exchange_order_id is None`, which is every
position in an existing store — keep the old modeled behaviour untouched.

## Modes

`POLAR_LIVE_TRADING` in `.env`:

| Value | Behaviour |
|---|---|
| unset, `off`, `false`, or anything unrecognized | Signal only. Credentials not required, nothing is signed. |
| `dry-run` | Full path including credential checks, contract lookup and quantization. Logs the exact order, sends nothing. |
| `true` / `1` / `live` | Real orders. |

Unrecognized values fail **closed**. This module is imported by a daemon
systemd restarts automatically; a missing or corrupted config must stop
trading, never start it.

## Kill switch

```bash
# stop opening new positions (existing venue-side stops stay in force)
touch Core/weekend_hunter/data/HALT

# close everything at the venue, right now
python3 -m Core.weekend_hunter.execution --flatten

# check state
python3 -m Core.weekend_hunter.execution --status

# resume
python3 -m Core.weekend_hunter.execution --resume
```

`--flatten` reads positions from **the venue**, not the local store, so it
works even when the two have diverged — which is exactly when you want it.

The daemon also trips `HALT` itself when it cannot reconcile: if Bitget
reports flat but no matching closed trade is found, it refuses to invent a
PnL and halts instead. Every risk gate downstream reads those numbers.

## Capital baseline

`WALLET_USDT = 500.0` is the paper constant. Live, the risk gates measure
against **the account's equity when live trading first started**, frozen in
`data/live_baseline.json` and never overwritten — recomputing it on each
restart would make a 30% drawdown unreachable and silently disable the
emergency stop.

Live equity comes from the venue (`accountEquity`, including unrealized
PnL), so the emergency stop can trip while losing positions are still open.
Free margin comes from the venue too, rather than being estimated from the
local store — it accounts for fees already charged and anything else sharing
the account.

## Failure handling

| Situation | Behaviour |
|---|---|
| Order rejected | Nothing recorded locally, Telegram warning sent, signal skipped. |
| Fill not confirmed | `BrokerError`; position is **not** recorded. Reconcile before acting. |
| Signal stale (price already through the stop) | Refused before sending — avoids paying two fees to stop out instantly. |
| Account read fails | Opens nothing that pass. Exits still run; venue stops unaffected. |
| Bitget unreachable at startup in live mode | Daemon refuses to start rather than falling back to the paper wallet. |
| Venue flat, no matching trade | `HALT`. No PnL is guessed. |
| Trailing-stop update fails | Logged. Previous venue-side stop remains — protection at a worse level, never none. |

Idempotency: `clientOid` is derived deterministically from the position id,
so a retry after an ambiguous network failure is rejected by Bitget as a
duplicate rather than opening a second position on one signal.

## API key setup

Create the key in Bitget with **Trade permission only** — do not enable
Withdraw. This code never calls a withdrawal endpoint. Bind the key to this
host's IP.

## Verifying the live path before arming

`Core/weekend_hunter/tests/test_execution.py` covers signing, sizing,
quantization, mode gating, idempotency and reconciliation, with the broker
faked at the interface. Public endpoints were exercised live.

What unit tests **cannot** cover is the authenticated write path: a signed
POST with a body, the fill read-back, and whether `presetStopLossPrice`
actually registers at the venue. A dry-run does not cover it either — it
stops before `place-order`.

`Core/Library/smoke_test_bitget.py` closes that gap with one deliberately
minimal real order:

```bash
python3 -m Core.Library.smoke_test_bitget            # preview, sends nothing
python3 -m Core.Library.smoke_test_bitget --confirm  # ~6.50 USDT notional at 5x
```

Cost is roughly 1.30 USDT of margin and under a cent in fees. It drives the
broker directly, touching neither the strategy nor the position store, and
closes the position in a `finally` block so a mid-stage failure still
flattens.

Stage 5 is the one that matters: it reads the stop back via
`pending_tpsl()` and fails loudly if no plan order registered. If that
stage fails, **do not arm live trading** — positions would run unprotected
and nothing else in this codebase would detect it.

Run order: `execution.py --status` (free, signed GET) → smoke test
(`--confirm`) → `POLAR_LIVE_TRADING=true`.
