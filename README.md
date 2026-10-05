<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d0221,50:2b0a3d,100:1a1a2e&height=200&section=header&text=POLAR&fontSize=80&fontColor=05D9E8&fontAlignY=38&animation=fadeIn&desc=north%20star%20trading%20system&descAlignY=58&descSize=18&descColor=FF2079" alt="POLAR" />

[![Typing SVG](https://readme-typing-svg.demolab.com/?font=Orbitron&size=20&duration=3000&pause=800&color=05D9E8&background=00000000&center=true&vCenter=true&width=750&lines=NORTH+STAR+TRADING+SYSTEM;MODULAR+%2F%2F+SWAPPABLE+%2F%2F+STRATEGY-AGNOSTIC;PHASE%3A+LIVE+--+WEEKEND_HUNTER+ACTIVE)](https://github.com/k1ubi/Polar)

![status](https://img.shields.io/badge/STATUS-LIVE-00fff9?style=for-the-badge&labelColor=0d0221)
![phase](https://img.shields.io/badge/PHASE-ACTIVE-ff2079?style=for-the-badge&labelColor=0d0221)
![core](https://img.shields.io/badge/CORE-WEEKEND__HUNTER-f6019d?style=for-the-badge&labelColor=0d0221)
![access](https://img.shields.io/badge/ACCESS-PRIVATE-05d9e8?style=for-the-badge&labelColor=0d0221)

</div>

<br>

```
                            *
                          \ | /
                      -----✦-----   N
                          / | \
                            *
```

## `// 0x00` IDENTITY

Polar is named for its north pole: the strategy it follows. Every other part
of the system exists to serve that one needle — trade signals, execution,
documentation — and, like a compass, the needle can be swapped without
touching the instrument around it.

```ansi
[36;1mPOLAR-OS v0.1 :: boot sequence[0m
[32m[ OK ][0m Core/vpa/             mounted
[32m[ OK ][0m Core/weekend_hunter/  mounted, daemon + telegram_bot live
[32m[ OK ][0m Core/Library/         mounted (shared infra: candlestick_trend, positions, telegram, indicators)
[32m[ OK ][0m Doc/Infrastructure/   mounted
[32m[ OK ][0m Doc/Core/             mounted
[32m[ OK ][0m Doc/Library/          mounted
```

## `// 0x01` ARCHITECTURE

Polar has exactly one moving part: **which Core is loaded.** Everything else
is built not to care which strategy is running underneath it.

```text
$ tree -L 2
.
├── Core/
│   ├── vpa/                 # Volume Price Analysis (Anna Coulling's method)
│   ├── weekend_hunter/       # Weekend Hunter Ultimate v6.2 port -- LIVE (daemon + telegram bot)
│   └── Library/              # shared infra, not a strategy: candlestick_trend, positions, telegram, indicators
├── Doc/
│   ├── Infrastructure/      # infra diagrams, tech stack, infra docs
│   ├── Core/                # per-core docs: how it works, strategy, pros/cons
│   └── Library/              # docs for shared infrastructure
├── README.md
├── requirements.txt
└── .gitignore
```

| Module | Role |
|---|---|
| `Core/` | Every strategy Polar has run, saved as its own subfolder. Exactly one drives live trading at a time. |
| `Core/Library/` | Shared infrastructure other Cores depend on — never a strategy on its own. |
| `Core/Library/broker.py` | Venue-agnostic order execution — swappable exchange, same as the swappable needle. |
| `Doc/Infrastructure/` | Diagrams, tech stack, infrastructure documentation. |
| `Doc/Core/` | Per-Core documentation — mechanism, strategy, pros/cons. |
| `Doc/Library/` | Documentation for shared infrastructure. |

## `// 0x02` CORE PROTOCOL

> Switching strategies means switching which `Core/` subfolder is active —
> not rewriting the system around it.

```text
$ ls Core/
Library/  vpa/  weekend_hunter/
```

## `// 0x03` EXECUTION

> Polar can place real orders on Bitget USDT-M futures. It does not, unless
> told to.

`POLAR_LIVE_TRADING` in `.env` gates everything: unset or unrecognized means
signal-only (the default, and how every Core has run to date), `dry-run`
logs real sizing without sending, `true` arms it. Stops and targets are
attached at the exchange with the entry order — the 300s poll loop is a
reconciler, not the thing standing between a position and a loss.

```bash
touch Core/weekend_hunter/data/HALT                  # stop opening
python3 -m Core.weekend_hunter.execution --flatten   # close everything now
python3 -m Core.weekend_hunter.execution --status    # mode, equity, halt state
```

Full detail, including the margin-vs-notional resolution and the failure
matrix: [`Doc/Infrastructure/execution.md`](./Doc/Infrastructure/execution.md).

## `// 0x04` STATUS

| Field | Value |
|---|---|
| Repo phase | `LIVE` |
| Active Core | `weekend_hunter` (daemon + telegram bot) |
| Execution | `Bitget USDT-M futures` — built, tested, **not yet armed** |
| Trades placed | `0` — weekend-gated, and no live order has ever been sent |
| Cores built | `2` (`vpa`, `weekend_hunter`) + 1 shared library (`candlestick_trend`) |
| Docs populated | `5` |

## `// 0x05` DOCS

- [`Doc/Infrastructure/execution.md`](./Doc/Infrastructure/execution.md) — live order execution, kill switch, failure modes
- [`Doc/Infrastructure/`](./Doc/Infrastructure) — infra diagrams, tech stack, infra docs
- [`Doc/Core/`](./Doc/Core) — how each Core works, its strategy, pros/cons
- [`Doc/Library/`](./Doc/Library) — shared infrastructure (candlestick_trend, positions, telegram, indicators)

<br>

<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:1a1a2e,50:2b0a3d,100:0d0221&height=120&section=footer" alt="" />

![footer-typing](https://readme-typing-svg.demolab.com/?font=Fira+Code&size=14&duration=1500&pause=500&color=05D9E8&background=00000000&center=true&vCenter=true&width=320&lines=%3E+connection+maintained_)

</div>
