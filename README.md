# Polar

Automated cryptocurrency futures trading system running on Bitget USDT-M perpetuals.

Polar is a rule-based, multi-agent system: a scanner shortlists entries, a Sonnet reviewer vetoes bad setups, a guardian monitors open positions, and an AI regime agent classifies the macro environment. No position is ever closed without explicit Telegram approval from the owner.

---

## Architecture

```
scanner.py     (every 15 min)   → scans 20 perps, scores setups, places ENTRY limit orders
guardian.py    (every 5 min)    → watches open positions: cancel stale entries, place TP,
                                   trailing, send close-approval requests to Telegram
portfolio.py   (every 15 min)   → equity history, 10% drawdown circuit breaker,
                                   delta & BTC-correlation advisory
regime.py      (every 1 h)      → BTC 4h/1d trend + futures basis + options skew → regime.json
audit.py       (daily)          → cross-checks state files against broker; Sonnet explains issues

polar-bot.py                    → Lorenzo's Telegram control bot (@polar_aibot)
polar-user-bot.py               → customer copy-trading bot (@polar_user_bot)

lib/broker.py                   → ccxt Bitget USDT-M wrapper
lib/indicators.py               → pure-numpy RSI, EMA, ATR, z-score, rvol, correlation
lib/config.py                   → loads live/.env, exposes TG_TOKEN / TG_CHAT
```

State is stored as JSON files in `live/state/`. Everything is append-only or atomic-replace; no database required.

---

## Safety rules

- **No close without approval.** `execute_approved()` in guardian.py is the only place a position is closed. It fires only when the symbol appears in `close-approvals.json`, written exclusively by Lorenzo's Telegram tap. Regime flips, 1R partials, and stop events all create an *approval request* — silence is never approval.
- **No Bitget plan orders.** `--order-type STP` and `--stop-loss` both map to Bitget plan orders that open opposite-side positions when no matching position exists. Never use them.
- **Leverage capped at 3×.** Configured in `LEVERAGE = 3` in scanner.py and on the Bitget account itself.
- **10% drawdown circuit breaker.** portfolio.py halts new entries automatically; resets after 12 h.
- **Sonnet review gate.** Every shortlisted entry passes through `review()` in scanner.py before an order is placed. The reviewer can VETO on news risk, correlation, or bad setup.

---

## Repository layout

```
Polar/
├── live/
│   ├── .env.example        # copy to .env, fill in credentials — NEVER commit .env
│   ├── bin/                # runnable scripts (scanner, guardian, portfolio, regime, audit)
│   ├── lib/                # shared modules (broker, indicators, config)
│   ├── bot/                # Telegram bots
│   ├── state/              # runtime JSON (gitignored)
│   ├── tests/              # stress harnesses
│   └── tools/              # one-off maintenance scripts
├── Doc/
│   └── Infrastructure/live-system/   # architecture docs and diagrams
└── github-backup/          # OpenAlice issue snapshots (gitignored — contains old tokens)
```

---

## Setup

### 1. Prerequisites

```bash
pip install ccxt python-telegram-bot aiohttp numpy
```

### 2. Credentials

```bash
cp live/.env.example live/.env
```

Edit `live/.env`:

```
TG_BOT_TOKEN=<your @polar_aibot token from @BotFather>
TG_CHAT_ID=<your Telegram numeric user ID>
TG_USER_BOT_TOKEN=<your @polar_user_bot token>  # only needed for copy-trading

BITGET_API_KEY=<Bitget API key>
BITGET_SECRET=<Bitget API secret>
BITGET_PASSPHRASE=<Bitget API passphrase>
```

Bitget API key requires **Read + Trade (Futures)** scopes only — no withdrawal permission.

### 3. Bitget account

- Set account leverage to **3×** (cross-margin, USDT-M perpetuals)
- Minimum recommended free margin: **$50**

### 4. State directory

```bash
mkdir -p live/state
echo '{}' > live/state/open-positions.json
echo '{}' > live/state/pending-orders.json
echo '{"paused": true}' > live/state/pause.json
echo '{"halted": false}' > live/state/circuit-breaker.json
```

### 5. Start the bots

```bash
# Control bot (keep running in background / systemd)
python3 live/bot/polar-bot.py

# Then resume via Telegram /resume command, or:
echo '{"paused": false}' > live/state/pause.json
```

---

## Running manually

All scripts are safe to run dry (no orders placed, no Telegram sent):

```bash
python3 live/bin/scanner.py              # dry-run: prints scores, places nothing
python3 live/bin/scanner.py --live       # live: places ENTRY limit orders

python3 live/bin/guardian.py             # checks open positions, sends Telegram approvals
python3 live/bin/guardian.py --live      # same, but executes approved closes

python3 live/bin/portfolio.py --dry      # equity snapshot, no writes
python3 live/bin/regime.py --dry         # regime score, no writes
python3 live/bin/audit.py --dry          # state/broker comparison, no Telegram
```

---

## Scheduled jobs (cron / systemd timers)

| Job | Schedule | Command |
|-----|----------|---------|
| Scanner | Every 15 min | `python3 live/bin/scanner.py --live` |
| Guardian | Every 5 min | `python3 live/bin/guardian.py --live` |
| Portfolio | Every 15 min | `python3 live/bin/portfolio.py` |
| Regime | Every 1 h | `python3 live/bin/regime.py` |
| Audit | Daily 06:00 UTC | `python3 live/bin/audit.py` |

---

## Contributing

1. Fork the repo and create a feature branch.
2. Run the stress tests before opening a PR:
   ```bash
   python3 live/tests/harness.py
   python3 live/tests/stress_scanner.py
   python3 live/tests/stress_guardian.py
   ```
3. Never commit `live/.env` or any file containing real API keys or Telegram tokens.
4. The guardian's close-approval rule is a hard invariant — do not add any code path that calls `close_position()` outside of `execute_approved()`.

---

## Documentation

Detailed architecture documentation is in [`Doc/Infrastructure/live-system/`](Doc/Infrastructure/live-system/README.md):

- [01 — Overview](Doc/Infrastructure/live-system/01-overview.md)
- [02 — Architecture](Doc/Infrastructure/live-system/02-architecture.md)
- [03 — Strategy](Doc/Infrastructure/live-system/03-strategy.md)
- [04 — Risk & Safety](Doc/Infrastructure/live-system/04-risk-and-safety.md)
- [05 — Scheduled Jobs](Doc/Infrastructure/live-system/05-scheduled-jobs.md)
- [06 — Telegram Bots](Doc/Infrastructure/live-system/06-telegram-bots.md)
- [07 — State Files](Doc/Infrastructure/live-system/07-state-files.md)
- [08 — Tech Stack](Doc/Infrastructure/live-system/08-tech-stack.md)
- [09 — Operations](Doc/Infrastructure/live-system/09-operations.md)
- [10 — Known Limits](Doc/Infrastructure/live-system/10-known-limits.md)
