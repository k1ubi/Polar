# Library: shared infrastructure

Three small modules, ported from the prior Polar codebase's `polar/delivery/`
and `polar/patterns/{atr,momentum,confirmation}.py`, generalized to not
assume a single shared wallet.

## `positions.py` — generic paper-position store

A JSON-file-backed store for a signal's lifecycle (alert → open → TP/SL/
manual-exit). Every function takes an explicit `path`, so each Core keeps
its own file and its own wallet rather than sharing one global store —
that's a deliberate change from an earlier version of this design (in the
prior codebase) where multiple strategies wrote to the same file and it
became impossible to compare their performance honestly.

Key behavior worth knowing about, because it's caused a real bug before:
- `available_balance_usdt()` subtracts margin already committed to open
  positions from the wallet figure. Sizing a new position against the raw
  wallet total (ignoring what's already committed) is the classic bug —
  it lets a bad streak of concurrent open positions commit many times the
  actual wallet.
- `consecutive_losses()` and `peak_equity()`/drawdown both key off the
  realized PnL **sign**, not the `status` field (`"tp"`/`"sl"`). Once a
  trailing or breakeven stop exists, an `"sl"`-status exit can still be a
  real win — the stop trailed into profit before eventually getting hit.
  `status` means "closed via the stop level," not "closed at a loss."

Used by: `Core/weekend_hunter/positions.py` today; any future live Core
should use this rather than building its own store.

## `telegram.py` — notifier + formatting primitives

`TelegramNotifier` (send/edit/long-poll/register-commands) plus
`row_block()` (label/value pairs as an aligned monospace table — Telegram
has no real table markup) and `fmt_time()`/`fmt_price()`.

Deliberately does **not** include per-Core message content (what a signal
or close alert actually says) — that lives in each Core's own daemon
(e.g. `Core/weekend_hunter/daemon.py`'s `format_signal_message`), since
different Cores have genuinely different things worth saying.

No `/status` button/command here, unlike the version this was ported
from — that queried `systemctl` for a fixed set of services on a specific
Raspberry Pi deployment, which doesn't generalize. Add a Core- or
deployment-appropriate equivalent once this project has a fixed target to
monitor.

## `candlestick_trend/backtest.py::top_n_crypto_usdt_pairs()` — universe selection

Picks Bitget USDT spot pairs by 24h volume, restricted to base assets above
a CoinGecko market-cap floor. Originally built only for candlestick_trend's
own backtest (top 15 pairs, mcap > $10M — that call's defaults are
untouched); now shared with VPA's live universe (all pairs above a floor,
not just a top-N shortlist — pass `n=None`).

Exclusions, layered:
- Bitget's tokenized-stock products (`baseCoin` prefixed `r`, e.g.
  rNVDA/rTSLA) and a hardcoded `STABLECOINS` ticker set — always on.
- **Category-based, always on**: CoinGecko's own `stablecoins` and
  `tokenized-gold` categories, cross-referenced by each ticker's matched
  CoinGecko `id` (not re-matched by ticker string). This exists because the
  hardcoded ticker set alone missed real cases caught by live testing:
  `USD1`, `USDS`, `RLUSD`, `GHO` (new stablecoins) and `XAUT`/`PAXG`
  (gold-pegged, non-directional). A ticker named confusingly close to
  "stable" isn't enough to get excluded by itself — `STABLE` (a real L1
  token, "Stable" chain, USDT-as-gas) stayed in because it's tagged
  `Stable Ecosystem`, not `stablecoins`, on CoinGecko.
- **Category-based, opt-in via `exclude_meme_coins=True`**: CoinGecko's
  `meme-token` category, plus a small explicit `KNOWN_MEME_CATEGORY_GAPS`
  supplement for coins verified (by direct `/coins/<id>` lookup) to carry no
  meme-related category tag at all — e.g. `jelly-my-jelly` comes back
  tagged only `Solana Ecosystem`/`Pump.fun Ecosystem`/`Binance Alpha
  Spotlight` despite being a well-known joke coin. Market cap alone doesn't
  filter memes out: DOGE/SHIB/PEPE/BONK/WIF/TRUMP/FLOKI all clear a $50M+
  floor on genuine trading volume. Defaults to `False` — candlestick_trend's
  backtest never asked for this; it's VPA-specific.

Ticker-collision caveat (documented, not fully eliminated): CoinGecko match
picks the largest-market-cap coin sharing a given ticker, so an obscure
Bitget listing can in principle still borrow an unrelated coin's cap. Raising
`coingecko_pages` (more CoinGecko coverage) and the $50M+ floor (VPA's
choice, vs. candlestick_trend's $10M) both reduce how often this matters in
practice — verified live: at `min_market_cap=50_000_000, coingecko_pages=4`,
the resulting ~188-pair universe was manually spot-checked clean of the
meme/junk contamination that showed up at the $10M floor.

## `indicators.py` — RSI, MACD, ATR

Standard technical indicators, Wilder-smoothed where that's the
conventional definition (RSI, ATR — both use an EMA with `alpha=1/period`,
not a plain rolling mean). Used by `Core/weekend_hunter/strategy.py`
today. Generic on purpose — not tied to any one Core's parameters (period
lengths are caller-supplied, e.g. Weekend Hunter's RSI(8) rather than the
usual RSI(14)).
