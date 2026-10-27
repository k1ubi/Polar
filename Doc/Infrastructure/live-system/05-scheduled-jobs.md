# 05 Scheduled jobs

Every job is a systemd user unit pair: a `.service` that runs a script once and a `.timer` that
starts it. Files are in `~/.config/systemd/user/`. The scripts are in `/home/kyubi/polar/bin/`.

| Timer | Interval | Script | Reads | Writes | Model used |
|---|---|---|---|---|---|
| `polar-guardian` | 5 min after the previous run started (about every 6 min in practice) | `guardian.py --live` | Bitget, state files, `regime.json` | `open-positions.json`, `pending-orders.json`, `close-requests.json`, `close-approvals.json`, `pause.json`, `last-prices.json`, `trade-signals.jsonl`, `metrics-cache.json`; Bitget orders | none |
| `polar-scanner` | 15 min | `scanner.py --live` | gate files, Bitget, candles, news | `pending-orders.json`, `trade-signals.jsonl`; Bitget entry orders | Sonnet 5.5 (entry review) |
| `polar-portfolio` | 30 min | `portfolio.py` | Bitget account, candles | `portfolio-history.jsonl`, `circuit-breaker.json`, `metrics-cache.json`, `correlation-advisory.json` | none |
| `polar-regime` | 2 h | `regime.py` | BTC candles, futures curve, Deribit options | `regime.json` | none |
| `polar-audit` | 1 h | `audit.py` | state files, Bitget | Telegram message only | Sonnet 5.5, only when something is wrong |
| `polar-backup` | daily, 03:00 CAT | `openalice-backup/backup.sh` | everything below | commits and pushes to GitHub | none |
| `polar-update-check` | Mondays 09:00 CAT | `check-update.sh` | OpenAlice release info | Telegram message once per new version | none |

After a reboot, the recurring timers start their first run within 1 to 5 minutes.

Two switches exist on every script: `--live` makes it act (guardian, scanner), and without it the
script prints what it would do and changes nothing. `portfolio.py`, `regime.py` and `audit.py` take
`--dry` for the same purpose.

## guardian.py

Manages positions. Full behaviour in [03 Strategy](03-strategy.md#step-5-managing-the-position-guardianpy-every-5-minutes)
and [04 Risk and safety](04-risk-and-safety.md#closing-a-position). In short:

- sees fills and places the take-profit,
- cancels entry orders older than 45 minutes,
- creates close requests and sends them with buttons,
- executes closes that Lorenzo approved,
- records every closed trade with its real exit price,
- pauses new entries when a request goes unanswered for 30 minutes.

One bad position cannot block the others: each is handled inside its own error guard, and the run
reports the errors at the end.

## scanner.py

Looks for new entries. Full behaviour in [03 Strategy](03-strategy.md#step-2-the-gates-scannerpy-every-15-minutes).
It scans the coins in chunks of 7 through one `alice analysis quant` call per chunk. A chunk that
fails is skipped, not retried.

## portfolio.py

- Appends equity to a 7-day history and works out the drop from the 24-hour peak (circuit breaker).
- Counts longs and shorts. A difference of 3 or more sends a "high delta" warning.
- Measures each open position's 15-minute correlation with Bitcoin. Two positions in the same
  direction above 0.9 send a warning. The scanner reads the same numbers (`correlation-advisory.json`).

## regime.py

Computes the market regime ([03 Strategy](03-strategy.md#step-1-the-market-regime-regimepy-every-2-hours)).
If the futures curve or option data cannot be fetched, those signals count as 0 and the regime is
decided from what is left. A change of regime sends a Telegram message.

## audit.py

Compares Polar's files with Bitget and sends a message if they disagree
([04](04-risk-and-safety.md#checks-that-run-on-their-own)).

## backup.sh

Copies the OpenAlice configuration (without credentials), the workspace, the Polar scripts and bots,
the state folder and the systemd units into the `OpenAliceBackup` repository, commits and pushes.
See [09 Operations](09-operations.md#backups).

## check-update.sh and upgrade-openalice.sh

The weekly check asks OpenAlice whether a newer stable release exists and tells Lorenzo once per
version. It changes nothing. Upgrading is done by hand with `bash ~/polar/bin/upgrade-openalice.sh`.
That script refuses to run while entry orders are pending, makes a backup, upgrades, restarts,
smoke-tests the Polar scripts, and rolls back by itself if a test fails.

## Switched-off pieces

- `polar-gate.timer` is disabled. It used to switch OpenAlice's model-driven jobs on and off. Its
  script `gate.py` is still there, with nothing left to switch.
- OpenAlice's own scheduled jobs (`alpha-scanner`, `position-guardian`, `regime-agent`,
  `portfolio-manager` and others) are set to done, so none of them runs. Only `performance-reporter`
  (weekly, model-written report) is still on.
