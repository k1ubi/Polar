"""Pure-numpy indicator library — replaces OpenAlice `alice analysis quant` scripts."""
import sys
import numpy as np


# ── bar fetching ─────────────────────────────────────────────────────────────

def fetch_bars(exchange, symbol: str, timeframe: str, count: int) -> dict:
    """Return OHLCV arrays for `symbol`. Raises on failure."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=count)
    if not ohlcv:
        raise ValueError(f"no OHLCV data for {symbol} {timeframe}")
    arr = np.array(ohlcv, dtype=float)
    return {
        "open":   arr[:, 1],
        "high":   arr[:, 2],
        "low":    arr[:, 3],
        "close":  arr[:, 4],
        "volume": arr[:, 5],
    }


# ── indicators (return a single float: the LAST bar's value) ────────────────

def rsi(close: np.ndarray, period: int = 14) -> float:
    delta = np.diff(close.astype(float))
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    if len(gains) < period:
        return 50.0
    ag = gains[:period].mean()
    al = losses[:period].mean()
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def ema(close: np.ndarray, period: int) -> float:
    close = close.astype(float)
    k = 2.0 / (period + 1)
    e = close[0]
    for c in close[1:]:
        e = c * k + e * (1.0 - k)
    return float(e)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        period: int = 14) -> float:
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    if len(tr) < period:
        return float(tr.mean()) if len(tr) else 0.0
    a = tr[:period].mean()
    for t in tr[period:]:
        a = (a * (period - 1) + t) / period
    return float(a)


def zscore(close: np.ndarray, period: int) -> float:
    w = close[-period:].astype(float)
    mu, sigma = w.mean(), w.std()
    return float((close[-1] - mu) / sigma) if sigma > 0 else 0.0


def rvol(volume: np.ndarray, period: int) -> float:
    avg = volume[-period - 1:-1].astype(float).mean()
    return float(volume[-1]) / avg if avg > 0 else 1.0


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    return float(np.corrcoef(a[-n:].astype(float), b[-n:].astype(float))[0, 1])


# ── composite computations ────────────────────────────────────────────────────

def compute_scan(exchange, symbols: list) -> dict:
    """Compute all scan metrics for a list of BASE symbols.

    Returns {BASE: {price, rsi_15m, rsi_1h, z8_15m, z20_1h, rvol,
                     ema20_4h, atr14_15m, btc_corr}}
    Symbols that fail (no data, rate-limited) are silently omitted.
    """
    result = {}
    try:
        btc15 = fetch_bars(exchange, "BTC/USDT:USDT", "15m", 50)
    except Exception as e:
        print(f"indicators: BTC bars failed: {e}", file=sys.stderr)
        btc15 = None

    for base in symbols:
        sym = f"{base}/USDT:USDT"
        try:
            b15 = fetch_bars(exchange, sym, "15m", 50)
            b1h = fetch_bars(exchange, sym, "1h",  50)
            b4h = fetch_bars(exchange, sym, "4h",  30)
            btc_corr = correlation(btc15["close"], b15["close"]) if btc15 is not None else 0.0
            result[base] = {
                "price":      float(b15["close"][-1]),
                "rsi_15m":    rsi(b15["close"], 14),
                "rsi_1h":     rsi(b1h["close"], 14),
                "z8_15m":     zscore(b15["close"], 8),
                "z20_1h":     zscore(b1h["close"], 20),
                "rvol":       rvol(b15["volume"], 20),
                "ema20_4h":   ema(b4h["close"], 20),
                "atr14_15m":  atr(b15["high"], b15["low"], b15["close"], 14),
                "btc_corr":   btc_corr,
            }
        except Exception as e:
            print(f"indicators.scan {base}: {e}", file=sys.stderr)
    return result


def compute_atr_map(exchange, bases: list) -> dict:
    """Return {BASE: atr14_15m} for open-position ATR trailing."""
    result = {}
    for base in bases:
        try:
            b = fetch_bars(exchange, f"{base}/USDT:USDT", "15m", 30)
            result[base] = atr(b["high"], b["low"], b["close"], 14)
        except Exception as e:
            print(f"indicators.atr_map {base}: {e}", file=sys.stderr)
    return result


def compute_correlations(exchange, bases: list) -> dict:
    """Return {BASE: btc_correlation_15m_60bars} for portfolio advisory."""
    result = {}
    try:
        btc = fetch_bars(exchange, "BTC/USDT:USDT", "15m", 60)
    except Exception as e:
        print(f"indicators: BTC corr bars failed: {e}", file=sys.stderr)
        return result
    for base in bases:
        try:
            b = fetch_bars(exchange, f"{base}/USDT:USDT", "15m", 60)
            result[base] = correlation(btc["close"], b["close"])
        except Exception as e:
            print(f"indicators.corr {base}: {e}", file=sys.stderr)
    return result
