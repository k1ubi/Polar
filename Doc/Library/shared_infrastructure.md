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

## `indicators.py` — RSI, MACD, ATR

Standard technical indicators, Wilder-smoothed where that's the
conventional definition (RSI, ATR — both use an EMA with `alpha=1/period`,
not a plain rolling mean). Used by `Core/weekend_hunter/strategy.py`
today. Generic on purpose — not tied to any one Core's parameters (period
lengths are caller-supplied, e.g. Weekend Hunter's RSI(8) rather than the
usual RSI(14)).
