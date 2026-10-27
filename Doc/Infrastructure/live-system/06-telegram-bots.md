# 06 Telegram bots

Both bots are Python programs built on `python-telegram-bot` 22.8 and run as systemd user services
(`polar-bot.service`, `polar-user-bot.service`). Both restart on failure.

## Owner bot (`@polar_aibot`)

File: `/home/kyubi/polar/bot/polar-bot.py`.

It answers one Telegram chat only, Lorenzo's. Messages from any other chat are ignored.

### Menu

The persistent keyboard at the bottom of the chat has six buttons:

| Button | Command | Shows |
|---|---|---|
| 📊 Positions | `/positions` | Open positions: side, entry, quantity, stop level, take-profit level, score. |
| 📋 Report | `/report` | The last 30 closed trades, win rate and summed result. |
| 👁 Monitor | `/monitor` | Current regime, system status (pause, circuit breaker, open requests) and pending entry orders. |
| 📈 Metrics | `/metrics` | Win rate, average win and loss, win/loss ratio, and results split by regime, session, score and direction. |
| ⚙️ Services | `/services` | State of the timers and services, and when each last ran. A job that has not run recently is marked red. |
| Decisions ⚖️ | `/decisions` | Every position Polar is asking about, with the answer buttons. |

Other commands: `/start` (shows the keyboard), `/resume` (clears a pause), and the older
`/approve` and `/reject` (kept working, but the buttons are the normal way).

### Decisions

Lists each position that has a close request, or that Lorenzo already answered. Each card shows the
entry, current price, profit or loss, stop and take-profit levels, the reason for the request, how
long it has waited, and whether Close would be full or partial. The buttons are:

- **🔴 Close**: asks for a second tap before anything is written.
- **🟢 Leave**: keeps the position, quiet for 6 hours.
- **🔕 Leave and don't ask me again**: asks for a second tap, then mutes that position.

The same three buttons appear on the guardian's alert messages. A tap on an old message whose
request no longer exists does nothing and says so.

The bot never closes anything. A confirmed Close writes `close-approvals.json`; the guardian does
the rest on its next run, within about 6 minutes
([04 Risk and safety](04-risk-and-safety.md#closing-a-position)).

### Shared files

The bot writes to the same state files as the scripts, under the same lock (`.state.lock`), and
only changes the keys it needs. This is why a tap that arrives while the guardian is mid-run is
not lost.

## Customer bot

File: `/home/kyubi/polar/bot/polar-user-bot.py`. Handle: `@polar_user_bot`.

Lets other people copy Polar's trades on their own Bitget accounts. At the time of writing nobody is
registered, and it has not been tried on a real customer account.

### Commands

| Command | Does |
|---|---|
| `/start` | Explains what the bot does. |
| `/register` | Shows the risk terms. The customer must type `I AGREE`, then send an API key, secret and passphrase one after another. Messages carrying a credential are deleted from the chat afterwards. `/cancel` stops the process. |
| `/status` | Lists that customer's copied trades. |
| `/unregister` | Removes the customer's credentials. Open positions are left as they are. |

The terms tell the customer that stops are not on the exchange, that a losing trade stays open
until the owner approves closing it, and to create an API key with trade permission only (no
withdrawals) and an IP whitelist.

### How a trade is copied

The bot checks the signals file every 30 seconds.

1. **Entry.** It copies a trade only after the main account's entry has filled and was recorded as
   an `entry` signal. A signal older than 60 minutes is ignored. No copy is opened while the system
   is paused or the circuit breaker is on, or if the state files cannot be read.
2. **Size.** Risk is 1.5% of the customer's balance, using the same stop distance as the main trade.
   The position's value is capped at 1.5 times the balance. If the exchange's minimum lot would risk
   more than 3% of the balance, the trade is skipped for that customer.
3. **Order.** A limit order at the entry price, valid for 45 minutes. After it fills, the customer's
   own take-profit order is placed.
4. **Exit.** A customer's copy is closed only when the main account's close was approved by Lorenzo,
   or the main account took a partial. Only the quantity this bot opened for that customer is
   touched. A position the customer opened themselves is never touched.

Copies are tracked in `customers/copies.json`. Credentials are in `customers/customers.json`.
Neither file is copied to GitHub.

### Not yet verified

The order parameters for a customer account (`tdMode: cross`, `posSide`) have only been tested
against a simulated exchange. The first real customer should start with a small balance.
