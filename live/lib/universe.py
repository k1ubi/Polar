"""Dynamic universe builder — replaces the hardcoded 20-coin list in scanner.py.

Professional approach: scan ALL perpetuals on Bitget, filter by liquidity,
rank by open interest + 30-day momentum. The coins worth trading self-select.
"""
import sys
from pathlib import Path

# ── constants ──────────────────────────────────────────────────────────────────

MIN_VOLUME_24H_USD  = 15_000_000   # skip anything too illiquid for a clean fill
MIN_OI_USD          = 5_000_000    # minimum open interest (position size signal)
MAX_DAILY_ATR_PCT   = 0.08         # skip hyper-volatile coins (ATR > 8% daily)
MIN_DAILY_ATR_PCT   = 0.008        # skip dead coins (ATR < 0.8% daily)
TOP_N               = 40           # keep the top N coins by OI after filtering
FORCE_INCLUDE       = ["BTC", "ETH", "SOL", "BNB"]  # always in universe

# Stables, indexes, and leveraged tokens — never trade these
_EXCLUDE = {"USDC", "USDT", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "PYUSD",
            "DEFI", "ALTSEASON", "SHIT", "REKT",  # Bitget index perps
            "BTCDOM", "ETHBTC"}


def build(exchange, state_dir=None) -> list:
    """Return sorted list of BASE symbols to scan.

    Priority: FORCE_INCLUDE first, then top-OI liquid coins.
    Falls back to a minimal default if the API call fails.
    """
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"universe: market fetch failed ({e}), using fallback", file=sys.stderr)
        return _fallback()

    candidates = []
    for sym, mkt in markets.items():
        # USDT-M perpetuals only
        if not (mkt.get("swap") and mkt.get("quote") == "USDT"
                and mkt.get("settle") == "USDT" and mkt.get("active")):
            continue
        base = mkt["base"]
        if base in _EXCLUDE or any(x in base for x in ("UP", "DOWN", "3L", "3S", "BULL", "BEAR")):
            continue

        t = tickers.get(sym, {})
        vol24  = float(t.get("quoteVolume") or t.get("baseVolume", 0) or 0)
        price  = float(t.get("last") or t.get("close") or 0)
        if price <= 0 or vol24 < MIN_VOLUME_24H_USD:
            continue

        try:
            oi_data = exchange.fetch_open_interest(sym)
            oi_usd  = float(oi_data.get("openInterestValue") or oi_data.get("openInterest", 0) or 0)
            if oi_usd == 0:
                oi_usd = vol24 * 0.1  # rough estimate if OI endpoint missing
        except Exception:
            oi_usd = vol24 * 0.1

        if oi_usd < MIN_OI_USD:
            continue

        candidates.append({"base": base, "oi_usd": oi_usd, "vol24": vol24, "price": price})

    # Momentum boost: 30d return (coins with positive momentum rank higher)
    mom_map = {}
    for c in candidates[:60]:  # only fetch momentum for shortlisted ones
        try:
            bars = exchange.fetch_ohlcv(f"{c['base']}/USDT:USDT", "1d", limit=31)
            if len(bars) >= 31:
                mom_map[c["base"]] = (bars[-1][4] - bars[0][4]) / bars[0][4]
        except Exception:
            pass

    def rank_key(c):
        mom  = mom_map.get(c["base"], 0.0)
        return c["oi_usd"] * (1.0 + max(0, mom))  # OI * (1 + positive momentum)

    candidates.sort(key=rank_key, reverse=True)

    # Build final list: force-includes first, then fill to TOP_N
    result = [b for b in FORCE_INCLUDE if b in {c["base"] for c in candidates}]
    seen   = set(result)
    for c in candidates:
        if len(result) >= TOP_N:
            break
        if c["base"] not in seen:
            result.append(c["base"])
            seen.add(c["base"])

    if not result:
        return _fallback()

    print(f"universe: {len(result)} coins (OI+momentum ranked)", file=sys.stderr)
    return result


def _fallback() -> list:
    return ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
            "LINK", "SUI", "NEAR", "ARB", "APT", "INJ", "OP", "HYPE",
            "AAVE", "WIF", "TIA", "SEI"]
