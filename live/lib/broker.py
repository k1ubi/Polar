"""ccxt Bitget USDT-M wrapper.

Returns dicts shaped like the former OpenAlice CLI so guardian/scanner/portfolio/audit
need only minimal changes. Credentials come from live/.env or environment variables:
  BITGET_API_KEY, BITGET_SECRET, BITGET_PASSPHRASE
"""
import os, sys, time
from pathlib import Path
import ccxt

SRC = "bitget-bf693fe4"   # logical source id — kept for aliceId key compatibility
_EX = None


def _load_env():
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def with_retry(fn, *args, retries: int = 3, base_delay: float = 2.0, **kwargs):
    """Retry a ccxt call on transient network errors with exponential back-off."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            if attempt >= retries - 1:
                raise
            wait = base_delay * (2 ** attempt)
            print(f"broker: transient error ({exc}), retry {attempt+1}/{retries-1} in {wait:.0f}s",
                  file=sys.stderr)
            time.sleep(wait)


def _ex() -> ccxt.bitget:
    global _EX
    if _EX is None:
        _load_env()
        _EX = ccxt.bitget({
            "apiKey": os.environ["BITGET_API_KEY"],
            "secret": os.environ["BITGET_SECRET"],
            "password": os.environ["BITGET_PASSPHRASE"],
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })
        _EX.load_markets()
    return _EX


# ── normalisation helpers ────────────────────────────────────────────────────

def _markets():
    return _ex().markets


def _contract_qty(symbol: str, contracts: float) -> float:
    """Convert ccxt `contracts` to coin quantity (multiply by contractSize)."""
    cs = float((_markets().get(symbol) or {}).get("contractSize") or 1)
    return contracts * cs


def _normalise_position(p: dict) -> dict:
    contracts = float(p.get("contracts") or 0)
    if contracts == 0:
        return None
    sym = p["symbol"]                    # 'BTC/USDT:USDT'
    base = sym.split("/")[0]             # 'BTC'
    qty = _contract_qty(sym, contracts)
    notional = abs(float(p.get("notional") or qty * float(p.get("markPrice") or 0)))
    return {
        "aliceId": f"{SRC}|{sym}",
        "symbol": base,
        "secType": "CRYPTO_PERP",
        "side": p["side"],               # 'long' / 'short'
        "quantity": str(qty),
        "avgCost": str(p.get("entryPrice") or 0),
        "marketPrice": str(p.get("markPrice") or 0),
        "marketValue": str(notional),
        "unrealizedPnL": str(p.get("unrealizedPnl") or 0),
        "risk": {
            "leverage": str(int(float(p.get("leverage") or 1))),
            "liquidationPrice": str(p.get("liquidationPrice") or 0),
            "marginMode": str(p.get("marginMode") or "cross"),
        },
    }


def _normalise_order(o: dict) -> dict:
    return {
        "orderId": o["id"],
        "aliceId": f"{SRC}|{o['symbol']}",
        "symbol": o["symbol"].split("/")[0],
        "action": "BUY" if o["side"] == "buy" else "SELL",
        "orderType": "LMT" if o["type"] == "limit" else o["type"].upper(),
        "totalQuantity": str(o.get("amount") or 0),
        "lmtPrice": str(o.get("price") or 0),
        "status": o.get("status", "open"),
    }


# ── public API ────────────────────────────────────────────────────────────────

def account_info() -> dict:
    ex = _ex()
    bal = ex.fetch_balance({"type": "swap", "productType": "USDT-FUTURES"})
    usdt = bal.get("USDT", {})
    net_liq = float(usdt.get("total") or 0)
    # unrealizedPnL from raw info if available, otherwise sum positions
    upnl = 0.0
    try:
        info = bal.get("info", {})
        data = info.get("data") or info
        if isinstance(data, list):
            data = data[0] if data else {}
        upnl = float(data.get("unrealizedPL") or data.get("unrealizedPnl") or 0)
    except Exception:
        pass
    return {
        "netLiquidation": str(net_liq),
        "unrealizedPnL": str(upnl),
        "initMarginReq": "0",
    }


def account_portfolio() -> list:
    raw = _ex().fetch_positions()
    out = []
    for p in raw:
        n = _normalise_position(p)
        if n:
            out.append(n)
    return out


def order_list(symbols: list = None) -> list:
    ex = _ex()
    try:
        if symbols:
            raw = []
            for s in symbols:
                raw.extend(ex.fetch_open_orders(s))
        else:
            raw = ex.fetch_open_orders()
    except Exception as e:
        print(f"broker.order_list: {e}", file=sys.stderr)
        return []
    return [_normalise_order(o) for o in raw]


def place_entry_order(symbol: str, action: str, price: float, qty: float,
                      comment: str = "") -> str:
    """Open a new position (not reduce-only). Returns order id."""
    side = "buy" if action == "BUY" else "sell"
    result = _ex().create_order(symbol, "limit", side, qty, price, params={
        "marginCoin": "USDT",
        "marginMode": "crossed",
    })
    return result["id"]


def place_tp_order(symbol: str, action: str, price: float, qty: float,
                   comment: str = "") -> str:
    """Place a reduce-only take-profit limit order. Returns order id."""
    side = "buy" if action == "BUY" else "sell"
    result = _ex().create_order(symbol, "limit", side, qty, price, params={
        "reduceOnly": True,
        "marginCoin": "USDT",
    })
    return result["id"]


def cancel_order(order_id: str, symbol: str, comment: str = "") -> None:
    _ex().cancel_order(str(order_id), symbol)


def close_position(symbol: str, qty: float = None, comment: str = "") -> None:
    """Close a position (full or partial) at market with reduceOnly."""
    ex = _ex()
    positions = ex.fetch_positions([symbol])
    pos = next((p for p in positions if float(p.get("contracts") or 0) > 0), None)
    if not pos:
        return
    side = "buy" if pos["side"] == "short" else "sell"
    if qty is None:
        qty = _contract_qty(symbol, float(pos["contracts"]))
    ex.create_order(symbol, "market", side, qty, params={
        "reduceOnly": True,
        "marginCoin": "USDT",
    })


def fetch_my_trades(symbol: str, limit: int = 100) -> list:
    """Return normalised fills for a symbol (OpenAlice order-trades shape)."""
    try:
        trades = _ex().fetch_my_trades(symbol, limit=limit)
    except Exception:
        return []
    out = []
    for t in trades:
        out.append({
            "orderId": t.get("order"),
            "timestamp": t.get("datetime", ""),
            "side": "BUY" if t["side"] == "buy" else "SELL",
            "price": str(t.get("price") or 0),
            "quantity": str(t.get("amount") or 0),
            "contract": {"aliceId": f"{SRC}|{t['symbol']}"},
            "source": "fill",
        })
    return out
