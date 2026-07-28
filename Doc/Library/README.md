# Library documentation

`Core/Library/` holds code that's **shared infrastructure, not a
strategy** — it never appears as a Core a user picks in isolation.
Anything here can be depended on by multiple Cores; nothing here decides
when to enter or exit a trade on its own.

- **[candlestick_trend.md](candlestick_trend.md)** — the 88-pattern
  TA-Lib+cm45t3r candlestick catalog with trend-context scoring. Started
  as its own Core; moved here once the decision was made to focus live
  effort on VPA and Weekend Hunter instead — the detection code is still
  fully working and available for any future Core that wants candlestick
  signals as an input, it's just not a strategy on its own anymore.
- **[shared_infrastructure.md](shared_infrastructure.md)** — the generic
  position store, Telegram delivery, and technical-indicator helpers used
  by whichever Core is actually live.
