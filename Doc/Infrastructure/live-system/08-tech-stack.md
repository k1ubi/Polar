# 08 Tech stack

Versions are what is installed on 2026-10-27.

## Machine

| | |
|---|---|
| Host | `kyubi-kali`, a personal machine |
| OS | Kali GNU/Linux Rolling, kernel 7.1.5 |
| Init and scheduling | systemd 262, user units (no root needed for Polar itself) |
| Clock | CAT (UTC+2). Trading rules use UTC. |

## Software

| Piece | Version | Role |
|---|---|---|
| Python | 3.14.7 | All of Polar's scripts and both bots |
| ccxt | 4.5.88 | Direct exchange library. Used for lot sizes and price precision (`amount_to_precision`), and by the customer bot to place orders on customers' accounts |
| python-telegram-bot | 22.8 | Both Telegram bots |
| OpenAlice | 0.94.1 (stable channel) | Trading runtime. Installed globally with npm; runs on Node 24 with a Bun provider. Web UI on `127.0.0.1:47331` (local only) |
| Claude Code CLI | 2.1.303 | Used as `claude -p --model claude-sonnet-5-5 --effort high --tools ""` for the entry review and the audit explanation. It is given text only, with no tools and no saved session |
| Graphviz | 14.1.2 | Draws the diagrams in this folder |
| git, rsync, curl, bash | 2.53.0, 3.5.1, system | Backup and update scripts |
| pytest | 9.1.1 | Installed, but the test files are plain scripts (see [09](09-operations.md#tests)) |

The standard library handles the rest: `json`, `subprocess`, `urllib` for Telegram calls from the
scripts, `fcntl` for the state lock, `decimal` for lot-safe quantities.

## External services

| Service | Used for | Access |
|---|---|---|
| Bitget | Trading and market data. USDT-M perpetuals only. Account `bitget-bf693fe4`, sub-account `derivatives` | API key with trade permission, held by OpenAlice |
| Deribit | Option chains for the regime signals, read through TraderHub | no key from Polar's side |
| Telegram Bot API | Two bots | one token per bot |
| Anthropic API (through Claude Code) | Sonnet 5.5 for two narrow jobs | Claude Code login on the machine |
| GitHub | Backup (`OpenAliceBackup`) and this documentation (`Polar`). Both private | SSH deploy key for the backup; stored Git credentials for `Polar` |
| RSS feeds | News headlines for the scanner's news gate, collected by OpenAlice | none |

Bitget has no options. Polar does not trade options anywhere.

## OpenAlice commands Polar uses

Everything goes through `openalice exec <tool> ...`. The scripts parse the JSON each command prints.

| Tool and command | Used by | Purpose |
|---|---|---|
| `alice analysis quant --script ...` | scanner, regime, guardian, portfolio | Candles and indicators in one call. The script is OpenAlice's own small expression language (`bars(...)`, `rsi`, `ema`, `atr`, `zscore`, `rvol`, `correlation`) |
| `alice rss grep --pattern COIN` | scanner | Recent headlines |
| `traderhub board get --board term-structure` | regime | BTC futures curve and annualised basis |
| `traderhub crypto options --symbol BTC` | regime | Deribit chain: open interest, delta, implied volatility |
| `alice-uta account info` / `account portfolio` | scanner, guardian, portfolio, audit, bot | Equity, positions, liquidation price |
| `alice-uta order list` / `order trades` | scanner, guardian, audit | Resting orders and real fills |
| `alice-uta order place` / `order cancel` | scanner, guardian | Entry and take-profit orders; cancellations |
| `alice-uta position close` | guardian, in one function only | The close |
| `alice-uta git status` / `git push` | scanner, guardian | See what is staged; send staged orders to Bitget |

The "git" in `alice-uta` is OpenAlice's way of working: an order is first staged like a commit, and
`push` sends it to the exchange. Polar pushes right after placing. Before an approved close, the guardian checks
`git status` and refuses to go on if anything else is already staged, because `push` would send
that too.

## Languages and layout of the code

About 2,800 lines of Python and shell in `bin/` and `bot/`, plus about 1,000 lines of tools and tests.

```
/home/kyubi/polar/
├── bin/        regime.py scanner.py guardian.py portfolio.py audit.py gate.py (unused)
│               check-update.sh upgrade-openalice.sh
├── bot/        polar-bot.py polar-user-bot.py
├── state/      JSON state (see 07)
├── customers/  customer credentials and copy records (not backed up)
├── tools/      backfill_exits.py (one-off repair tool, dry-run by default)
└── tests/      harness.py stress_guardian.py stress_scanner.py stress_userbot.py stress_decisions.py
```

Each script is a single file with its own copy of the small helpers (Telegram sender, JSON
readers, OpenAlice call wrapper). A change to one script cannot affect another, at the price of
some duplicated code.
