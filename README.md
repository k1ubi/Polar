<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d0221,50:2b0a3d,100:1a1a2e&height=200&section=header&text=POLAR&fontSize=80&fontColor=05D9E8&fontAlignY=38&animation=fadeIn&desc=north%20star%20trading%20system&descAlignY=58&descSize=18&descColor=FF2079" alt="POLAR" />

[![Typing SVG](https://readme-typing-svg.demolab.com/?font=Orbitron&size=20&duration=3000&pause=800&color=05D9E8&background=00000000&center=true&vCenter=true&width=750&lines=NORTH+STAR+TRADING+SYSTEM;MODULAR+%2F%2F+SWAPPABLE+%2F%2F+STRATEGY-AGNOSTIC;PHASE%3A+SCAFFOLD+--+NO+ACTIVE+CORE)](https://github.com/k1ubi/Polar)

![status](https://img.shields.io/badge/STATUS-STANDBY-00fff9?style=for-the-badge&labelColor=0d0221)
![phase](https://img.shields.io/badge/PHASE-SCAFFOLD-ff2079?style=for-the-badge&labelColor=0d0221)
![core](https://img.shields.io/badge/CORE-NONE_ACTIVE-f6019d?style=for-the-badge&labelColor=0d0221)
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
[32m[ OK ][0m Core/                mounted (skeleton, standby)
[32m[ OK ][0m Doc/Infrastructure/  mounted
[32m[ OK ][0m Doc/Core/            mounted
[33m[WARN][0m no active Core loaded -- STANDBY
```

## `// 0x01` ARCHITECTURE

Polar has exactly one moving part: **which Core is loaded.** Everything else
is built not to care which strategy is running underneath it.

```text
$ tree -L 2
.
├── Core/                   # swappable strategy modules — one folder per saved strategy
├── Doc/
│   ├── Infrastructure/      # infra diagrams, tech stack, infra docs
│   └── Core/                # per-core docs: how it works, strategy, pros/cons
├── README.md
└── .gitignore
```

| Module | Role |
|---|---|
| `Core/` | Every strategy Polar has run, saved as its own subfolder. Exactly one drives live trading at a time. |
| `Doc/Infrastructure/` | Diagrams, tech stack, infrastructure documentation. |
| `Doc/Core/` | Per-Core documentation — mechanism, strategy, pros/cons. |

## `// 0x02` CORE PROTOCOL

> Switching strategies means switching which `Core/` subfolder is active —
> not rewriting the system around it. This is the design contract, not yet
> a built interface.

```text
$ ls Core/
# (empty — no Core has been migrated into this repo yet)
```

## `// 0x03` STATUS

| Field | Value |
|---|---|
| Repo phase | `SCAFFOLD` |
| Active Core | `NONE` |
| Cores migrated | `0` |
| Docs populated | `0` |

## `// 0x04` DOCS

- [`Doc/Infrastructure/`](./Doc/Infrastructure) — infra diagrams, tech stack, infra docs
- [`Doc/Core/`](./Doc/Core) — how each Core works, its strategy, pros/cons

<br>

<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:1a1a2e,50:2b0a3d,100:0d0221&height=120&section=footer" alt="" />

![footer-typing](https://readme-typing-svg.demolab.com/?font=Fira+Code&size=14&duration=1500&pause=500&color=05D9E8&background=00000000&center=true&vCenter=true&width=320&lines=%3E+connection+maintained_)

</div>
