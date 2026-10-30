# Polar live system: documentation

Written 2026-10-27, describing what runs on the machine `kyubi-kali` today.

Polar is an automated trading system for Bitget USDT-margined perpetual futures. It looks at the
market every few minutes, opens positions when its rules say so, and watches them. It never closes
a position by itself: closing always needs a tap from Lorenzo on Telegram.

This folder documents the current system, which runs on OpenAlice and plain Python scripts. The
older documents one level up (`../execution.md`, `../../Core/`) describe the earlier VPA and Weekend
Hunter version of Polar. They are kept for history and do not describe what is running now.

## Where to start

| If you want to... | Read |
|---|---|
| Know what the system is and what it will never do | [01 Overview](01-overview.md) |
| See how the parts connect | [02 Architecture](02-architecture.md) |
| Understand how it picks and manages trades | [03 Strategy](03-strategy.md) |
| Know what protects the account | [04 Risk and safety](04-risk-and-safety.md) |
| Find out what each timer and script does | [05 Scheduled jobs](05-scheduled-jobs.md) |
| Use or change the Telegram bots | [06 Telegram bots](06-telegram-bots.md) |
| Look up a file in the state folder | [07 State files](07-state-files.md) |
| See the software, versions and services in use | [08 Tech stack](08-tech-stack.md) |
| Check, restart, back up, upgrade, test | [09 Operations](09-operations.md) |
| Read what is still unfinished or risky | [10 Known limits](10-known-limits.md) |

## Diagrams

The images are generated from the `.dot` files with Graphviz. To change one, edit the `.dot` file
and run the command in [02 Architecture](02-architecture.md#redrawing-the-diagrams).

| Diagram | Source | Shown in |
|---|---|---|
| Architecture | [architecture.dot](diagrams/architecture.dot) | 02 |
| One trade, signal to exit | [trade-lifecycle.dot](diagrams/trade-lifecycle.dot) | 02, 03 |
| Closing a position | [close-approval.dot](diagrams/close-approval.dot) | 04 |

## Words used in these documents

| Term | Meaning here |
|---|---|
| Perpetual (perp) | A futures contract with no expiry date. Polar trades the USDT-margined ones on Bitget. |
| Regime | Polar's verdict on the market: BULLISH, BEARISH or NEUTRAL. Decides which direction is allowed. |
| Long / short | Betting the price goes up / down. |
| Score | A number from 0 to 8 given to each coin by the scanner. 7 or more is needed to trade. |
| ATR | Average True Range: the typical size of recent candle moves. Used to set the stop distance. |
| Stop level | The price at which Polar asks Lorenzo whether to close. It is not an order on the exchange. |
| TP | Take-profit: a limit order on the exchange that closes the position at a profit. |
| 1R | A price move in the trade's favour equal to the stop distance. |
| Request | A message sent to Lorenzo asking whether to close a position. |
| Staged / push | OpenAlice prepares orders first and only sends them to the exchange on `git push`. |
| CAT | Central Africa Time, UTC+2. The machine's clock. |

## Not in this repository

The scripts themselves live in `/home/kyubi/Polar/live` on the machine, not in this repository. They are
copied every night into the private `OpenAliceBackup` repository (see [09 Operations](09-operations.md#backups)).
They are not stored here because several of them still contain the Telegram bot tokens
(see [10 Known limits](10-known-limits.md)).
