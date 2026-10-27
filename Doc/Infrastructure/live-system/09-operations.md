# 09 Operations

Commands are run on `kyubi-kali` as user `kyubi`.

## Is everything running?

```bash
systemctl --user is-active openalice polar-bot polar-user-bot   # three lines: active
systemctl --user list-timers --no-pager                         # seven polar-* timers
openalice status                                                # runtime, version, uptime
```

In Telegram, **⚙️ Services** shows the same, plus when each job last ran. **👁 Monitor** adds
pause, circuit breaker and open requests.

## Reading logs

```bash
journalctl --user -u polar-guardian --since "-1h" --no-pager
journalctl --user -u polar-scanner  --since "-1h" --no-pager
journalctl --user -u polar-bot -f                                # follow live
openalice logs                                                   # OpenAlice runtime, redacted
```

Replace the unit name with `polar-regime`, `polar-portfolio`, `polar-audit`, `polar-user-bot`.

## Dry runs

Running a script by hand without `--live` changes nothing:

```bash
python3 /home/kyubi/polar/bin/guardian.py        # prints what it would do
python3 /home/kyubi/polar/bin/scanner.py         # shows each coin's score, places no order
python3 /home/kyubi/polar/bin/regime.py --dry
python3 /home/kyubi/polar/bin/portfolio.py --dry
python3 /home/kyubi/polar/bin/audit.py --dry
```

## Restarting

```bash
systemctl --user restart polar-bot            # after editing polar-bot.py
systemctl --user restart polar-user-bot
systemctl --user restart openalice            # rarely needed; the guardian skips runs while it is down
systemctl --user start polar-guardian.service # run the guardian once now
```

Restarting a bot loses nothing: all state is in files.

## Stopping all new trades

Two ways, both leave open positions alone:

- Stop the scanner: `systemctl --user stop polar-scanner.timer`. Start it again with `start`.
- Let the system do it: a pause (unanswered request) or the circuit breaker stops entries by itself.

There is no command that closes positions in bulk, on purpose.

## Backups

`polar-backup.timer` runs `/home/kyubi/openalice-backup/backup.sh` every day at 03:00 CAT. It copies:

- OpenAlice configuration, without account keys, login data or the sealing key,
- OpenAlice's workspaces, including the switched-off agent instructions,
- the Polar scripts, bots, tools, tests and state folder (`polar-live/`),
- the HEAD commit of the `Polar` repository (`polar-source/`, which includes this documentation),
- the systemd unit files,

then commits and pushes to the private GitHub repository `OpenAliceBackup`, using a dedicated SSH
key. It skips `customers.json` and `copies.json`, the SSH keys and every credential file.

The Polar scripts in the backup still contain the two bot tokens
([10 Known limits](10-known-limits.md)).

To restore on a new machine: clone `OpenAliceBackup` and run `setup.sh`, then add the Bitget keys
and OpenAlice login by hand under `~/.openalice/data/config/` and start the services.

## Upgrading OpenAlice

The Monday check sends a Telegram message when a newer stable version exists. To upgrade:

```bash
bash ~/polar/bin/upgrade-openalice.sh
```

It stops if entry orders are pending, takes a backup, upgrades through npm (this asks for sudo),
restarts OpenAlice, runs a smoke test against the Polar scripts and goes back to the old version by
itself if the test fails. It never touches positions or orders.

## Tests

The tests are in `/home/kyubi/polar/tests/`. They use a fake broker and fake Telegram and a throwaway
state folder, never the real account or the real state.

```bash
cd /home/kyubi/polar/tests
python3 -I stress_guardian.py      # 18 checks
python3    stress_scanner.py       # 6 checks (needs the user's Python packages, so no -I)
python3    stress_userbot.py       # 18 checks
python3 -I stress_decisions.py     # 17 checks
```

Each prints PASS or FAIL per check. Run them after changing `guardian.py`, `scanner.py` or either bot.

They cover, among others: a position that briefly disappears from the broker's answer, a partly
filled entry, a retried partial close, stale or mismatched approvals, failing market data, exit
price accuracy, lot-size rounding, the Decisions flow, and the customer copy rules.

## Common situations

| Situation | What to do |
|---|---|
| Telegram says **PAUSED** | Answer the waiting requests in Decisions, or `/resume`. |
| **CIRCUIT BREAKER** message | Nothing. It resets by itself after 12 hours if drawdown is back under 10%. |
| **AUDIT** message listing mismatches | Read it. Typical cause: a position closed or opened on Bitget by hand. The audit changes nothing. |
| A job shows red in Services | `journalctl --user -u <job>` and look for the last error. |
| No entries for days | Check `regime.json`. NEUTRAL, the 09:00-12:59 UTC window, a pause, a breaker or 5 open positions all mean "no entries" and are normal. |
| `alice` or `traderhub` "not found" in a shell | They are not on the PATH. Use `openalice exec alice ...` and `openalice exec traderhub ...`. |
