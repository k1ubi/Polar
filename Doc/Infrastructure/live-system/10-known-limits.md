# 10 Known limits

Open problems and design trade-offs as of 2026-10-27, most important first. Nothing here is hidden
in the code; each item was found by reading it or by the tests.

## Security

1. **Bot tokens are written inside the scripts.** Both Telegram tokens are in the Python files and
   in two shell scripts, and the nightly backup copies them to the private GitHub repository. Anyone
   with access to the machine or the repository could control the bots. Fix: create new tokens in
   BotFather, store them in a file readable only by the owner, and load them from there. Not done,
   waiting on a decision from Lorenzo.
2. **Customer API keys are stored as plain text** in `customers/customers.json`, readable only by the
   owner (file mode 600). They are excluded from the backup. Encrypting them would be the next step
   before real customers join.
3. **This documentation does not contain any secret**, and the scripts are not in this repository,
   for the reason in point 1.

## Trading logic

4. **Stops are not exchange orders.** A position can lose more than the planned stop distance while
   a request waits for Lorenzo. A pause starts after 30 minutes without an answer, but the position
   stays open. This follows from the rule that nothing closes without his tap, and it was accepted.
5. **No limit on total exposure.** Each trade is sized by its own risk; there is no cap on the total
   position value. The audit warns above 2.5x equity. A cap (total up to 1.5x equity, 0.75x per
   position) has been proposed and not approved.
6. **The news check fails open.** If the news feed cannot be read, the scanner trades as if there were
   no bad news. The scanner prints no warning when it happens.
7. **The trading-hours window is a guess.** The scanner blocks 09:00-12:59 UTC. This does not match
   the sessions Lorenzo has described, and the choice has not been revisited.
8. **The regime has no hysteresis.** The score can cross a threshold and come back at the next
   2-hour run. A switch straight to the opposite regime creates a close request for every position
   held the other way.
9. **Sample size.** 11 closed trades in the log. Nothing about the strategy's edge can be concluded.
10. **The average recorded loss (-2.48%) is above the 1.5% maximum stop distance.** Not yet explained.
11. **Funding costs and fees** are not part of the recorded results.
12. **Leverage is assumed to be 10x** by the sizing code. It is a setting on the Bitget account, not
    something Polar sets. If it changes there, the margin math in the scanner is off.

## Operations

13. **The weekly performance report is still a model job inside OpenAlice** and has an open issue
    that is not fixed. Everything else is plain scripts.
14. **Everything runs on one machine.** If it is off, nothing is watching positions. Take-profit orders
    stay on Bitget, but nothing creates close requests and no messages are sent.
15. **No automatic alert for a dead timer.** The Services screen shows it when asked, but nothing
    sends a message if, say, the guardian timer stops. The hourly audit compares files with Bitget
    and would not notice.
16. **Time zones.** The machine clock is CAT (UTC+2) while the trading rules use UTC. Log times and
    rule times differ by two hours, which is easy to misread.

## Customer bot

17. **Not tested with a real customer.** The order parameters for a customer's account (`tdMode: cross`,
    `posSide`) were checked only against a simulator. Start with one customer and a small balance.
18. **Customers share the signal timing, not the price.** Their limit order is at the main account's
    entry price and may not fill. After 45 minutes it is cancelled.
19. **A customer's loss exposure follows Lorenzo's tap too.** A customer's losing position stays open
    until he approves closing the main one. The terms shown at registration say so.

## Upgrades

20. **OpenAlice is on version 0.94.1** and the upgrade needs Lorenzo's sudo password. The
    smoke test covers the Polar scripts, not every OpenAlice feature.
21. **The Sonnet model ID** (`claude-sonnet-5-5`) is written in the scripts. If Anthropic retires it,
    the review will fail closed, meaning no new entries, until the ID is updated.
