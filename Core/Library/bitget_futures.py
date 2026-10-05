"""Bitget USDT-M futures execution — the `Core.Library.broker.Broker`
implementation that turns a Polar signal into a real order.

Market data already comes from Bitget's public SPOT endpoints
(`candlestick_trend.backtest.fetch_ohlcv`). This module talks to the
authenticated MIX (futures) v2 API, which is a different product with a
different symbol universe — all 9 of Weekend Hunter's pairs were verified
present and `symbolStatus == "normal"` on USDT-FUTURES, with max leverage
between 50x and 150x, comfortably above the strategy's 20x ceiling.

Why futures rather than spot: the strategy emits `bearish` signals and a
dynamic 5-20x leverage. Spot can express neither.

Credentials come from `.env` (never committed — see .gitignore):

    BITGET_API_KEY=...
    BITGET_SECRET=...
    BITGET_PASSPHRASE=...          # the passphrase set when creating the key

The key needs "Trade" permission on futures. Bind it to your IP in Bitget's
key settings; do NOT grant Withdraw. This module never calls a withdrawal
endpoint and never needs one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import requests

from Core.Library.broker import BrokerError, BrokerPosition, ClosedTrade, Fill

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN = "USDT"
# Isolated, deliberately: it caps each position's loss at its own posted
# margin. Under `crossed`, one runaway position can consume the entire
# account's equity as collateral, which would make every per-position risk
# figure in Core/weekend_hunter/positions.py meaningless.
MARGIN_MODE = "isolated"
TIMEOUT = 15


def _now_ms() -> str:
    return str(int(time.time() * 1000))


class BitgetFutures:
    """Thin, explicit REST client. No ccxt — Polar has avoided that
    dependency throughout (see backtest.py's docstring), and the surface
    needed here is small enough that a wrapper would obscure more than it
    saves."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
    ):
        self.api_key = api_key or os.getenv("BITGET_API_KEY")
        self.api_secret = api_secret or os.getenv("BITGET_SECRET")
        self.passphrase = passphrase or os.getenv("BITGET_PASSPHRASE")
        if not (self.api_key and self.api_secret and self.passphrase):
            raise BrokerError(
                "BITGET_API_KEY / BITGET_SECRET / BITGET_PASSPHRASE not set — see .env"
            )
        self._session = requests.Session()
        self._contracts: dict[str, dict] = {}

    # ---------- signing ----------

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        """Bitget: BASE64( HMAC_SHA256( secret, timestamp + METHOD + requestPath + queryString + body ) ).

        `request_path` must already include the "?..." query string when
        there is one, and `body` is the exact JSON string that gets sent —
        re-serializing it differently anywhere between here and the request
        produces a valid-looking signature that the venue rejects.
        """
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode()

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        query = ""
        if params:
            query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        request_path = f"{path}?{query}" if query else path
        # Serialize ONCE and send this exact string — see _sign().
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        timestamp = _now_ms()

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(timestamp, method, request_path, body_str),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

        try:
            resp = self._session.request(
                method, f"{BASE_URL}{request_path}", headers=headers,
                data=body_str if body_str else None, timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise BrokerError(f"{method} {path} failed to reach Bitget: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise BrokerError(f"{method} {path} returned non-JSON (HTTP {resp.status_code})") from exc

        # Bitget signals application errors in the body with a non-"00000"
        # code and HTTP 200, so status_code alone is not a success check.
        if payload.get("code") != "00000":
            raise BrokerError(
                f"{method} {path} rejected by Bitget: code={payload.get('code')} msg={payload.get('msg')}"
            )
        return payload.get("data")

    # ---------- contract specs ----------

    def contract(self, symbol: str) -> dict:
        """Per-symbol trading rules, cached for the process lifetime.

        Needed because Bitget sizes futures orders in BASE COIN with a
        per-symbol step, not in USDT. PEPEUSDT for instance has
        sizeMultiplier=1000 and volumePlace=0, so a size of 1500 is invalid
        — it must be a whole multiple of 1000.
        """
        if not self._contracts:
            data = self._public_get("/api/v2/mix/market/contracts", {"productType": PRODUCT_TYPE})
            self._contracts = {c["symbol"]: c for c in data}
        contract = self._contracts.get(symbol)
        if contract is None:
            raise BrokerError(f"{symbol} is not listed on Bitget {PRODUCT_TYPE}")
        if contract.get("symbolStatus") != "normal":
            raise BrokerError(f"{symbol} is not tradable right now (status={contract.get('symbolStatus')})")
        return contract

    def _public_get(self, path: str, params: dict) -> dict:
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        try:
            resp = self._session.get(f"{BASE_URL}{path}?{query}", timeout=TIMEOUT)
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise BrokerError(f"GET {path} failed: {exc}") from exc
        if payload.get("code") != "00000":
            raise BrokerError(f"GET {path} rejected: {payload.get('msg')}")
        return payload.get("data")

    def quantize_size(self, symbol: str, notional_usdt: float, price: float) -> str:
        """USDT of exposure -> a base-coin size string the venue will accept.

        Rounds DOWN, always. Rounding up would silently take more risk than
        the strategy asked for, and can push the order past the free margin
        the risk gates just checked against.
        """
        contract = self.contract(symbol)
        step = Decimal(contract["sizeMultiplier"])
        min_size = Decimal(contract["minTradeNum"])
        places = int(contract["volumePlace"])

        raw = Decimal(str(notional_usdt / price))
        steps = (raw / step).to_integral_value(rounding="ROUND_DOWN")
        size = steps * step

        if size < min_size:
            raise BrokerError(
                f"{symbol}: {notional_usdt:.2f} USDT at {price} is {raw} base, "
                f"below the {min_size} minimum order size"
            )
        min_notional = float(contract.get("minTradeUSDT") or 0)
        if float(size) * price < min_notional:
            raise BrokerError(
                f"{symbol}: order notional {float(size) * price:.2f} USDT is below "
                f"the venue minimum of {min_notional} USDT"
            )
        return f"{size:.{places}f}"

    def round_price(self, symbol: str, price: float) -> str:
        """Stops and targets must respect the symbol's tick precision — an
        over-precise trigger price is rejected outright, which on an entry
        order means the position opens with NO stop attached."""
        places = int(self.contract(symbol)["pricePlace"])
        # Truncate rather than round: a rounded stop can land a hair further
        # from entry than intended, and for a 0.3% stop that is material.
        factor = 10 ** places
        return f"{math.floor(price * factor) / factor:.{places}f}"

    # ---------- account ----------

    def account_equity_usdt(self) -> float:
        data = self._request("GET", "/api/v2/mix/account/account", params={
            "symbol": "BTCUSDT", "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
        })
        return float(data["accountEquity"])

    def available_usdt(self) -> float:
        data = self._request("GET", "/api/v2/mix/account/account", params={
            "symbol": "BTCUSDT", "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
        })
        return float(data["available"])

    def set_position_mode_one_way(self) -> None:
        """One-way mode, so a symbol has at most one position and `side`
        alone determines direction.

        In hedge mode the same symbol can hold a long AND a short at once,
        and orders need a `tradeSide` open/close qualifier. Weekend Hunter
        enforces pyramiding=0 (one position per symbol) in
        positions.risk_gate_blocked(), so one-way matches the strategy's own
        model and removes a whole class of "closed the wrong leg" bugs.
        """
        self._request("POST", "/api/v2/mix/account/set-position-mode", body={
            "productType": PRODUCT_TYPE, "posMode": "one_way_mode",
        })

    def set_leverage(self, symbol: str, leverage: float) -> None:
        contract = self.contract(symbol)
        max_lever = float(contract.get("maxLever") or 0)
        if max_lever and leverage > max_lever:
            raise BrokerError(f"{symbol}: {leverage}x exceeds the venue maximum of {max_lever}x")
        self._request("POST", "/api/v2/mix/account/set-leverage", body={
            "symbol": symbol, "productType": PRODUCT_TYPE,
            "marginCoin": MARGIN_COIN, "leverage": str(int(leverage)),
        })

    def set_margin_mode(self, symbol: str) -> None:
        self._request("POST", "/api/v2/mix/account/set-margin-mode", body={
            "symbol": symbol, "productType": PRODUCT_TYPE,
            "marginCoin": MARGIN_COIN, "marginMode": MARGIN_MODE,
        })

    # ---------- orders ----------

    def open_position(
        self, symbol: str, direction: str, notional_usdt: float, leverage: float,
        stop_loss: float, take_profit: float, client_oid: str,
    ) -> Fill:
        price = self.mark_price(symbol)

        # Refuse an entry whose stop is already breached. Weekend Hunter's
        # stops sit ~0.3-0.6% from the signal bar's close, and this runs up
        # to 300s after that bar -- price can already be through the level.
        # Sending it anyway either gets rejected by the venue or opens a
        # position that stops out instantly, paying both fees for nothing.
        is_long = direction == "bullish"
        if (is_long and price <= stop_loss) or (not is_long and price >= stop_loss):
            raise BrokerError(
                f"{symbol}: mark price {price} is already through the stop {stop_loss} — "
                f"signal is stale, not opening"
            )
        if (is_long and price >= take_profit) or (not is_long and price <= take_profit):
            raise BrokerError(
                f"{symbol}: mark price {price} has already passed the target {take_profit} — "
                f"signal is stale, not opening"
            )

        size = self.quantize_size(symbol, notional_usdt, price)

        # Order of operations matters: margin mode and leverage must be set
        # BEFORE the order, because Bitget applies whatever the symbol is
        # currently configured with. Setting them after would open the
        # position at a stale leverage — possibly a much higher one left
        # over from a previous trade.
        self.set_margin_mode(symbol)
        self.set_leverage(symbol, leverage)

        body = {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "marginMode": MARGIN_MODE,
            "marginCoin": MARGIN_COIN,
            "size": size,
            "side": "buy" if direction == "bullish" else "sell",
            "orderType": "market",
            "clientOid": client_oid,
            # The stop and target ride along with the entry. If this order
            # fills, protection exists at the venue immediately — there is
            # no window where the position is naked waiting for the next
            # daemon poll. See Core.Library.broker for why that matters.
            "presetStopLossPrice": self.round_price(symbol, stop_loss),
            "presetStopSurplusPrice": self.round_price(symbol, take_profit),
        }
        data = self._request("POST", "/api/v2/mix/order/place-order", body=body)
        order_id = data["orderId"]

        # A market order's response carries no fill price; read it back so
        # the stored entry is the REAL one, not the signal's intended price.
        filled = self._order_detail(symbol, order_id)
        return Fill(
            order_id=order_id, symbol=symbol,
            side=body["side"], size_base=filled["size_base"],
            avg_price=filled["avg_price"],
            notional_usdt=filled["size_base"] * filled["avg_price"],
        )

    def _order_detail(self, symbol: str, order_id: str, attempts: int = 5) -> dict:
        """Poll briefly for the fill. A market order is normally filled by
        the time the response lands, but 'normally' is not 'always', and an
        entry price of 0 would corrupt every downstream PnL and R-multiple."""
        for attempt in range(attempts):
            data = self._request("GET", "/api/v2/mix/order/detail", params={
                "symbol": symbol, "productType": PRODUCT_TYPE, "orderId": order_id,
            })
            filled_size = float(data.get("baseVolume") or 0)
            avg_price = float(data.get("priceAvg") or 0)
            if filled_size > 0 and avg_price > 0:
                return {"size_base": filled_size, "avg_price": avg_price}
            time.sleep(0.4 * (attempt + 1))
        raise BrokerError(
            f"{symbol}: order {order_id} placed but no fill confirmed — "
            f"reconcile against the venue before acting on this position"
        )

    def update_stop_loss(self, symbol: str, direction: str, stop_loss: float) -> None:
        """Replace the position-level stop. `pos_loss` is position-scoped, so
        this supersedes the stop attached at entry rather than stacking a
        second one on top of it."""
        self._request("POST", "/api/v2/mix/order/place-tpsl-order", body={
            "symbol": symbol, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
            "planType": "pos_loss",
            "triggerPrice": self.round_price(symbol, stop_loss),
            "triggerType": "mark_price",
            "holdSide": "long" if direction == "bullish" else "short",
        })

    def pending_tpsl(self, symbol: str) -> list[dict]:
        """The stop/target plan orders currently live at the venue for this
        symbol.

        Exists to VERIFY protection rather than assume it. `open_position`
        attaches the stop via `presetStopLossPrice`; if Bitget were ever to
        ignore that field, positions would run naked and nothing else in
        this codebase would notice. Read it back instead.
        """
        data = self._request("GET", "/api/v2/mix/order/orders-plan-pending", params={
            "symbol": symbol, "productType": PRODUCT_TYPE, "planType": "profit_loss",
        })
        return (data or {}).get("entrustedList") or []

    def get_position(self, symbol: str) -> BrokerPosition | None:
        data = self._request("GET", "/api/v2/mix/position/single-position", params={
            "symbol": symbol, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
        })
        for row in data or []:
            size = float(row.get("total") or 0)
            if size <= 0:
                continue
            liq = row.get("liquidationPrice")
            return BrokerPosition(
                symbol=symbol,
                direction="bullish" if row.get("holdSide") == "long" else "bearish",
                size_base=size,
                entry_price=float(row.get("openPriceAvg") or 0),
                leverage=float(row.get("leverage") or 0),
                unrealized_pnl_usdt=float(row.get("unrealizedPL") or 0),
                liquidation_price=float(liq) if liq else None,
            )
        return None

    def close_position(self, symbol: str, direction: str) -> Fill:
        data = self._request("POST", "/api/v2/mix/order/close-positions", body={
            "symbol": symbol, "productType": PRODUCT_TYPE,
            "holdSide": "long" if direction == "bullish" else "short",
        })
        succeeded = (data or {}).get("successList") or []
        if not succeeded:
            failure = (data or {}).get("failureList") or []
            raise BrokerError(f"{symbol}: close rejected by Bitget: {failure}")
        order_id = succeeded[0].get("orderId")
        filled = self._order_detail(symbol, order_id)
        return Fill(
            order_id=order_id, symbol=symbol,
            side="sell" if direction == "bullish" else "buy",
            size_base=filled["size_base"], avg_price=filled["avg_price"],
            notional_usdt=filled["size_base"] * filled["avg_price"],
        )

    def recent_closed_trades(self, symbol: str, since_iso: str) -> list[ClosedTrade]:
        since_ms = int(datetime.fromisoformat(since_iso).timestamp() * 1000)
        data = self._request("GET", "/api/v2/mix/position/history-position", params={
            "symbol": symbol, "productType": PRODUCT_TYPE, "startTime": since_ms,
            "endTime": _now_ms(),
        })
        trades = []
        for row in (data or {}).get("list", []):
            # Bitget spells this `uTime` (capital T) on history-position;
            # `utime` silently yields 0 and dates every close to 1970.
            closed_ms = int(row.get("uTime") or 0)
            trades.append(ClosedTrade(
                symbol=row.get("symbol", symbol),
                direction="bullish" if row.get("holdSide") == "long" else "bearish",
                entry_price=float(row.get("openAvgPrice") or 0),
                exit_price=float(row.get("closeAvgPrice") or 0),
                # netProfit is Bitget's own realized figure NET of the fees
                # it actually charged. Polar's modeled TAKER_FEE_PCT /
                # SLIPPAGE_PCT must NOT be subtracted again on top of this.
                realized_pnl_usdt=float(row.get("netProfit") or 0),
                closed_at=datetime.fromtimestamp(closed_ms / 1000, timezone.utc).isoformat(),
                reason="unknown",
            ))
        return trades

    def mark_price(self, symbol: str) -> float:
        data = self._public_get("/api/v2/mix/market/symbol-price", {
            "symbol": symbol, "productType": PRODUCT_TYPE,
        })
        return float(data[0]["markPrice"])
