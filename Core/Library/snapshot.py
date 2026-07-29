"""Capture the system's verified state to a JSON baseline.

Written so that "it worked on 2026-07-29" is a checkable claim rather than a
memory. Two distinct uses:

1. **Regression baseline.** Re-run after any change and diff. If the capital
   scale, a risk-gate threshold or a position size moved and you did not
   intend it, the diff says so immediately — these are the numbers that
   decide how much real money goes into a trade, and none of them should
   drift silently.
2. **Offline test fixtures.** The captured Bitget contract specs (tick size,
   step size, minimums, max leverage) let tests exercise real sizing rules
   with no network and no credentials.

    python3 -m Core.Library.snapshot                    # print
    python3 -m Core.Library.snapshot --save            # write both files

Writes:
    Doc/Infrastructure/baseline.json                    # the full snapshot
    Core/weekend_hunter/tests/fixtures/bitget_contracts.json

The account section is omitted unless credentials are configured, so this
stays runnable on a fresh checkout. No balances or secrets are written to
the contracts fixture — that one is committed; `baseline.json` records
equity and belongs to whatever your .gitignore policy is (Core/*/data/ is
already excluded, Doc/ is not).
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from Core.Library.broker import BrokerError  # noqa: E402
from Core.Library.candlestick_trend.backtest import fetch_ohlcv  # noqa: E402
from Core.Library.indicators import compute_atr  # noqa: E402
from Core.weekend_hunter import daemon as wh_daemon  # noqa: E402
from Core.weekend_hunter import execution  # noqa: E402
from Core.weekend_hunter import positions as store  # noqa: E402
from Core.weekend_hunter import strategy as wh  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_PATH = ROOT / "Doc" / "Infrastructure" / "baseline.json"
FIXTURES_PATH = ROOT / "Core" / "weekend_hunter" / "tests" / "fixtures" / "bitget_contracts.json"


@dataclass
class _Sig:
    """Minimal stand-in for a WeekendHunterSignal — position_size_usd only
    reads signal_strength."""

    signal_strength: float
    final_leverage: float


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


def collect(include_market: bool = True) -> dict:
    scale = store.capital_scale()
    snapshot: dict = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git": {"head": _git("rev-parse", "--short", "HEAD"),
                "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(_git("status", "--porcelain"))},
        "mode": {"trading_mode": execution.trading_mode(),
                 "halted": execution.halted(),
                 "weekend_only": wh.ENABLE_WEEKEND_ONLY},
        "capital": {
            "pine_design_capital": store.PINE_DESIGN_CAPITAL,
            "paper_wallet_usdt": store.WALLET_USDT,
            "initial_capital_usdt": store.initial_capital_usdt(),
            "capital_scale": scale,
        },
        # The numbers that gate real money. Any unintended movement here is
        # exactly what this baseline exists to catch.
        "risk_gates": {
            "min_capital_per_trade": store.min_capital_per_trade(),
            "safety_buffer": store.safety_buffer(),
            "can_afford_threshold": store.min_capital_per_trade() + store.safety_buffer(),
            "max_concurrent_positions": store.MAX_CONCURRENT_POSITIONS,
            "max_consecutive_losses": store.MAX_CONSECUTIVE_LOSSES,
            "daily_loss_limit_usdt": store.initial_capital_usdt() * store.DAILY_LOSS_LIMIT_PCT,
            "weekend_loss_limit_usdt": store.initial_capital_usdt() * store.WEEKEND_LOSS_LIMIT_PCT,
            "emergency_stop_below_usdt": store.initial_capital_usdt() * (1 - store.MAX_DRAWDOWN_PCT),
        },
        "leverage": {"min": wh.MIN_LEVERAGE, "max": wh.MAX_LEVERAGE},
        "position_sizes_usdt_margin": {
            sym: {
                "normal": wh.position_size_usd(_Sig(60.0, 12.0), sym, scale),
                "high_confidence": wh.position_size_usd(_Sig(80.0, 12.0), sym, scale),
            }
            for sym in wh_daemon.SYMBOLS
        },
        "store": {
            "open_positions": len(store.open_positions()),
            "closed_positions": len(store.closed_positions()),
            "realized_pnl_usdt": store.total_realized_pnl(),
        },
    }

    if execution.trading_mode() != execution.MODE_OFF:
        try:
            broker = execution.get_broker()
            snapshot["account"] = {
                "venue": "bitget-usdt-futures",
                "equity_usdt": broker.account_equity_usdt(),
                "available_usdt": broker.available_usdt(),
                "open_at_venue": {
                    s: (lambda p: None if p is None else
                        {"direction": p.direction, "size_base": p.size_base,
                         "entry": p.entry_price, "leverage": p.leverage})(broker.get_position(s))
                    for s in wh_daemon.SYMBOLS
                },
            }
        except BrokerError as exc:
            snapshot["account"] = {"error": str(exc)}

    if include_market:
        # Live ATR stop/target distances. These drive how tight the venue-side
        # stops sit, which is the single biggest difference between the paper
        # track and real fills -- worth recording alongside everything else.
        market: dict = {}
        for sym in wh_daemon.SYMBOLS:
            try:
                df = fetch_ohlcv(sym, wh_daemon.EXEC_TIMEFRAME, 300)
                atr = float(compute_atr(df, period=wh.ATR_PERIOD).iloc[-1])
                px = float(df["close"].iloc[-1])
                tp, sl = wh.compute_levels("bullish", px, atr, 12.0, 0.7)
                market[sym] = {
                    "price": px, "atr": atr,
                    "stop_distance_pct": (px - sl) / px * 100,
                    "target_distance_pct": (tp - px) / px * 100,
                }
            except Exception as exc:  # noqa: BLE001
                market[sym] = {"error": str(exc)}
        snapshot["market_at_capture"] = market

    return snapshot


def collect_contracts() -> dict:
    """Bitget contract specs for the traded universe — the offline fixture."""
    from Core.Library.bitget_futures import BitgetFutures

    client = BitgetFutures.__new__(BitgetFutures)  # public endpoint only, no credentials
    client._contracts = {}
    client._session = __import__("requests").Session()
    data = client._public_get("/api/v2/mix/market/contracts", {"productType": "USDT-FUTURES"})
    by_symbol = {c["symbol"]: c for c in data}
    keep = ("symbol", "minTradeNum", "volumePlace", "pricePlace", "sizeMultiplier",
            "maxLever", "minTradeUSDT", "symbolStatus")
    return {
        "_note": "Captured from Bitget USDT-FUTURES. Offline fixture for sizing tests.",
        "_captured_at": datetime.now(timezone.utc).isoformat(),
        "contracts": {
            s: {k: by_symbol[s][k] for k in keep if k in by_symbol[s]}
            for s in wh_daemon.SYMBOLS if s in by_symbol
        },
    }


def main() -> int:
    snapshot = collect()
    contracts = collect_contracts()

    if "--save" in sys.argv:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
        FIXTURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURES_PATH.write_text(json.dumps(contracts, indent=2, sort_keys=True))
        print(f"wrote {BASELINE_PATH.relative_to(ROOT)}")
        print(f"wrote {FIXTURES_PATH.relative_to(ROOT)}")
    else:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
