# Core: weekend_hunter

## What it is

A faithful Python port of **Weekend Hunter Ultimate v6.2 [PRODUCTION
READY]** (TradingView, Pine Script v6, author YOUNGPSI):
https://it.tradingview.com/script/1Y4pNLSm-Weekend-Hunter-Ultimate-v6-2/

"Faithful" here means line-by-line translation of the actual Pine source
Lorenzo provided — not a reconstruction from the page's prose description.
Every constant, threshold, and formula in `strategy.py` has a direct Pine
counterpart, called out in a comment. Code: `Core/weekend_hunter/`.

## Why "weekend" — the strategy's own thesis

The script trades specifically during weekend hours (Saturday/Sunday UTC,
with a "prime window" Sunday 00:00-08:00 UTC weighted higher), on the
theory that thinner weekend liquidity produces cleaner, more decisive
moves. `ENABLE_WEEKEND_ONLY` defaults to the Pine script's own default
(`true`) — on a weekday, `weekend_trading_active` is `False` and **no
signal will ever qualify, by design**. That's not a bug; running the
daemon Monday-Friday will correctly and silently produce nothing.

## Module map

| Module | What it does |
|---|---|
| `strategy.py` | The ported signal logic: weekend gating, multi-timeframe trend confluence, CVD divergence, the weighted signal-strength composite, dynamic 5-20x leverage, ATR-based TP/SL, trailing-stop ratchet, liquidation-proximity check |
| `positions.py` | Weekend Hunter's own $500 wallet (matching the Pine script's own `initial_capital` default) and risk gates (max concurrent positions, daily/weekend loss limits, consecutive-loss breaker, account emergency stop), built on `Core.Library.positions`'s generic store |
| `daemon.py` | The live loop: polls BTCUSDT/ETHUSDT on the daily chart (plus separate 4h/1h fetches for confluence), opens/manages/closes paper positions, pushes Telegram alerts |
| `telegram_bot.py` | The command listener: `/positions`, `/report`, `/weekendscalper`, `/pending`, `/statistics` |

## Two things that couldn't be translated 1:1

1. **CVD (cumulative volume delta)** — Pine's `var float cvd = 0.0`
   accumulates forever across the whole chart's history. This port
   computes it as a cumsum over whatever historical window is fetched
   instead. This is **mathematically exact**, not an approximation, for
   what the script actually does with it: the divergence check only ever
   compares `cvd` to `cvd[10]` (10 bars ago) — the sum of volume_delta
   over the last 10 bars, which is identical regardless of where the
   cumulative sum "started," as long as the window includes at least 10
   bars before the comparison point. Verified by
   `test_cvd_divergence_windowed_cumsum_is_exact`.
2. **TradingView's `strategy.exit(trail_points=..., trail_offset=...)`**
   trailing-stop mechanics are TradingView's own internal order-management
   behavior. Reimplemented as an explicit ratchet in
   `update_trailing_stop()` — activates once price moves
   `TRAILING_ACTIVATION_PCT` in favor, then trails
   `TRAILING_DISTANCE_PCT` behind the running extreme, only ever tightening.

## Sizing is absolute-dollar, not wallet-percent

The Pine script sizes positions in fixed USD ($120 base, ±multipliers for
confidence and per-symbol risk), not as a percentage of account equity —
`position_size_usd()` is a direct port of that. This only makes sense
against the wallet size the script assumes ($500), which is why
`positions.py` uses that figure rather than this project's usual 200 USDT
convention elsewhere.

## Data source

`daemon.py` reuses `Core.Library.candlestick_trend.backtest.fetch_ohlcv`
(Bitget's public REST API, no auth needed for market data) rather than
rebuilding an exchange-adapter layer — the old codebase's `ccxt`/platform
abstraction doesn't exist in this repo, and wasn't needed here.

## Running it

```
python3 -m Core.weekend_hunter.daemon         # pushes signals, manages positions
python3 -m Core.weekend_hunter.telegram_bot   # answers commands
```

Both need `POLAR_TELEGRAM_BOT_TOKEN` / `POLAR_TELEGRAM_CHAT_ID` in `.env`.
Run both together for the full experience — the daemon only pushes, the
bot only listens.

## Verification

`Core/weekend_hunter/tests/test_strategy.py` — 19 tests: weekend gating
(including the "must stay silent on a weekday" property), the CVD
windowed-cumsum equivalence proof, leverage/level/trailing-stop formulas,
position sizing, and every risk gate (available capital, max concurrent
positions, single-position-per-symbol, consecutive-loss counting **by PnL
sign, not status label** — a trailing stop can turn an "sl"-status exit
into a real win, and the streak counter has to know that — and the account
emergency stop).

Also dry-run against live BTCUSDT/ETHUSDT data (both the daily execution
chart and separate 4h/1h fetches) end-to-end with no exceptions.

## Calibration status

**This is a faithful port, not a validated strategy.** Nothing about
whether Weekend Hunter's specific thresholds (RSI(8), MACD(8,17,9),
`MIN_SIGNAL_STRENGTH=55`, the leverage/risk-profile table, etc.) actually
produce good outcomes has been checked here — that's a separate,
open question, same as every other Core in this project.
