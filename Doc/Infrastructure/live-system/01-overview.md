# 01 Overview

## What Polar does

Polar trades a fixed list of 20 crypto coins on Bitget, as USDT-margined perpetual futures, at 10x
leverage. The loop is:

1. Every 2 hours it works out whether the market is bullish, bearish or neutral.
2. Every 15 minutes it scores the 20 coins. If one scores 7 or more out of 8 in the direction the
   market allows, it sends a limit order.
3. Every 5 minutes it checks what is open. When an entry fills, it places a take-profit order. When
   something needs a decision, it asks Lorenzo on Telegram.
4. Every 30 minutes and every hour, other scripts check the account's health and compare Polar's
   records with what Bitget actually shows.

A second bot lets customers copy the same trades on their own Bitget accounts. No customer is
registered at the moment.

## The rules that never change

These are checked in the code, not just written down.

1. **Nothing closes without Lorenzo.** No script closes, partly closes or reduces a position on its
   own. A stop being crossed, a regime change or a profit target only creates a request. The close
   command exists in one place in the code (`execute_approved()` in `guardian.py`) and runs only
   for a position that Lorenzo approved with a tap. Details in [04](04-risk-and-safety.md).
2. **Silence is not a yes.** If a request is not answered within 30 minutes, Polar stops opening
   new trades and keeps asking. It does not close anything.
3. **Risk per trade is about 1.5% of the account.** The size of each order is worked out from that.
4. **At most 5 positions open, and at most 2 in the same direction.**
5. **Failures stop trading, they don't start it.** If a state file is unreadable, the scanner does
   nothing. If the Sonnet review fails or times out, the entry is cancelled.

## What the system is made of

| Part | What it is |
|---|---|
| OpenAlice | Open-source trading runtime. Polar uses it as the way to read market data and talk to Bitget. |
| Seven scheduled scripts | Plain Python, started by systemd timers. They contain all the trading logic. No AI model decides anything in them, except two narrow uses of Claude Sonnet 5.5 (see below). |
| Two Telegram bots | One for Lorenzo (menus, close decisions), one for customers (copy trading). |
| State folder | A set of small JSON files where the scripts leave notes for each other. |
| Bitget | The exchange. The account is `bitget-bf693fe4`, sub-account `derivatives`. |

## Where an AI model is used

Only in two places, both with a tight job:

- **Entry review (scanner).** After a coin passes every rule, Claude Sonnet 5.5 gets the setup and
  can only answer APPROVE or VETO. It cannot change the order. Any error or unreadable answer counts
  as a veto.
- **Audit explanations (audit).** If the hourly audit finds a mismatch between Polar's records and
  Bitget, Sonnet writes a four-line explanation for Lorenzo. It places nothing.

OpenAlice also has a weekly "performance-reporter" job that uses a model. It is the only OpenAlice
scheduled job still enabled.

## History in two lines

Polar was first built as Python daemons (VPA and Weekend Hunter strategies, see the older
documents). In October 2026 it was rebuilt on OpenAlice, first as scheduled model-driven agents
(their instruction files are still in the OpenAlice workspace, switched off) and then as the plain
scripts described here, so that routine checks no longer need a model session to run.
