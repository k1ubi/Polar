# Polar

Polar is named for its north pole: the strategy it follows. Everything in the
system points toward maximizing winning operations, the way a compass needle
points north — and, like a compass, the needle (strategy) can be swapped
without changing the rest of the instrument.

## Modular design

- **`Core/`** — every strategy Polar has run, each as its own saved,
  swappable subfolder. Only one Core drives live trading at a time; switching
  strategies means switching which Core is active, not rewriting the system
  around it.
- **`Doc/`**
  - **`Doc/Infrastructure/`** — infrastructure diagrams, tech stack, and
    infrastructure documentation.
  - **`Doc/Core/`** — documentation for each Core: how it works, the
    strategy it implements, and its pros/cons.

This repo starts as a skeleton — Cores are added incrementally.
