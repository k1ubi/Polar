# Core documentation

Documentation for each Core lives here: how it works, the strategy it
implements, and its pros/cons.

One doc per Core, added alongside the Core itself:

- **[vpa.md](vpa.md)** — Volume Price Analysis (Anna Coulling's method).
- **[weekend_hunter.md](weekend_hunter.md)** — faithful port of
  TradingView's Weekend Hunter Ultimate v6.2. The only Core with a live
  daemon + Telegram bot as of this writing.

`candlestick_trend` was a Core early on; it's now shared infrastructure —
see [`Doc/Library/README.md`](../Library/README.md).
