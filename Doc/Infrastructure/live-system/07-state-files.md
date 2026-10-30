# 07 State files

Folder: `/home/kyubi/Polar/live/state/`. All files are JSON unless noted. Scripts write through a
temporary file and rename it, so a reader never sees half a file.

## Who changes what

Several programs write to the same files. `.state.lock` is an empty file used as a lock. A writer
takes the lock, reads the file as it is now, changes only the keys it owns, and saves. This stops a
slow guardian run from erasing an approval tapped in the meantime.

## Files

| File | Written by | Read by | Holds |
|---|---|---|---|
| `regime.json` | regime | scanner, guardian, bot | `regime`, `score`, `confidence`, the five signals, the numbers behind them, `updated_at` |
| `pause.json` | guardian, bot | scanner, guardian, customer bot, bot | `paused`, `reason`, `since` (or `resumed_at`) |
| `circuit-breaker.json` | portfolio | scanner, customer bot, bot | `halted`, `drawdown_pct`, `peak_24h`, `net_liq`, `halted_at` |
| `pending-orders.json` | scanner (adds), guardian (removes) | guardian, scanner, audit, bot | One entry per unfilled limit order: symbol, side, limit price, quantity, planned stop and take-profit, score, regime, `order_id`, `placed_at` |
| `open-positions.json` | guardian | scanner, guardian, audit, bot | One entry per open position: entry price, quantity, stop level, take-profit level, stop distance, ATR, score, regime, `entered_at`, `tp_order_id`, flags such as `breakeven_done` and `partial_tp_done` |
| `close-requests.json` | guardian, bot | guardian, bot | One entry per coin: side, reason, price, `requested_at`, `last_alert_at`, and if answered `rejected` / `rejected_at` and `muted` / `muted_at` |
| `close-approvals.json` | bot | guardian | One entry per approved coin: quantity and time of the tap. Removed once used. |
| `trade-signals.jsonl` | scanner, guardian | customer bot, bot (reports), metrics | Append-only log, one JSON object per line. See below. |
| `trade-signals.jsonl.bak` | by hand | | A copy kept before an earlier repair. Not read by anything. |
| `metrics-cache.json` | guardian, portfolio | bot | Win rate, averages, breakdowns by regime, session, score, direction and outcome; portfolio delta and long/short counts |
| `portfolio-history.jsonl` | portfolio | portfolio | Equity every 30 minutes, kept for 7 days |
| `correlation-advisory.json` | portfolio | scanner | Each open position's correlation with BTC |
| `last-prices.json` | guardian | guardian | Last seen price per coin, used as a fallback for exit pricing |
| `last-processed-signal.txt` | customer bot | customer bot | How many `entry` lines of the log it has handled |
| `last-processed-close.txt` | customer bot | customer bot | How many close lines it has handled |
| `update-notified.txt` | update check | update check | Last OpenAlice version announced (created on first use) |

## trade-signals.jsonl

One line per event. The field `type` tells which:

| `type` | Written when | Main fields |
|---|---|---|
| `order` | the scanner sends an entry order | symbol, side, limit price, quantity, score, regime |
| `entry` | the guardian sees it filled | symbol, side, entry price, quantity, score, regime, stop and take-profit levels |
| `skipped` | an entry is dropped | symbol, `reason` (`news_gate`, `reviewer_veto`, `entry_expired`, `entry_not_open`, `risk_N pct`, `order_error`) |
| `partial_close` | an approved partial close executes | symbol, side, quantity, fraction, new take-profit level, price |
| `close` | a position is gone | symbol, side, entry and exit price, `pnl_pct`, `outcome`, regime, score, times |

`outcome` is one of `TP_HIT`, `MANUAL_APPROVED` (closed after Lorenzo's tap), `CLOSED_EXTERNAL`
(closed on Bitget by something other than Polar), `CLOSED_UNKNOWN` (gone, and no fill could be
found), or `SL_HIT` in older rows. `pnl_pct` is the price move from entry to exit, not the account
return.

The customer bot reads this file to know what to copy. Older rows have no `type` and are skipped.

## Outside this folder

| Path | Holds |
|---|---|
| `/home/kyubi/Polar/live/customers/customers.json` | Registered customers and their API credentials (file mode 600, not encrypted) |
| `/home/kyubi/Polar/live/customers/copies.json` | Trades copied for customers |
| `/home/kyubi/.openalice/` | OpenAlice's own data: config, the Bitget account credentials (files the backup skips), workspaces, logs |
