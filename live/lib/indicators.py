"""Pure-numpy indicator library — replaces OpenAlice `alice analysis quant` scripts."""
import sys, time
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── in-process BTC vol cache (valid for 1 h, avoids repeated daily bar fetches) ──
_btc_vol_cache: dict = {}   # {"vol": float, "ts": float}
_BTC_VOL_TTL = 3600         # seconds


# ── bar fetching ─────────────────────────────────────────────────────────────

def fetch_bars(exchange, symbol: str, timeframe: str, count: int,
               retries: int = 3) -> dict:
    """Return OHLCV arrays for `symbol`. Retries on transient network errors."""
    for attempt in range(retries):
        try:
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
        except ValueError:
            raise
        except Exception as exc:
            if attempt >= retries - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            print(f"fetch_bars {symbol} {timeframe}: {exc}, retry {attempt+1} in {wait:.0f}s",
                  file=sys.stderr)
            time.sleep(wait)


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

def ema_array(close: np.ndarray, period: int) -> np.ndarray:
    """Return full EMA array (same length as close)."""
    close = close.astype(float)
    k = 2.0 / (period + 1)
    out = np.empty_like(close)
    out[0] = close[0]
    for i in range(1, len(close)):
        out[i] = close[i] * k + out[i - 1] * (1.0 - k)
    return out


def funding_rate(exchange, symbol: str) -> float:
    """Fetch current funding rate for symbol. Returns 0.0 on failure."""
    try:
        fr = exchange.fetch_funding_rate(symbol)
        return float(fr.get("fundingRate", 0.0))
    except Exception:
        return 0.0


def oi_change_pct(exchange, symbol: str) -> float:
    """Return 24h open interest % change. Positive = OI growing (new money entering).
    Returns 0.0 on failure (treat as neutral)."""
    try:
        hist = exchange.fetch_open_interest_history(symbol, "4h", limit=7)
        if len(hist) >= 7:
            old = float(hist[0].get("openInterestValue") or hist[0].get("openInterest", 0))
            cur = float(hist[-1].get("openInterestValue") or hist[-1].get("openInterest", 0))
            return (cur - old) / old if old > 0 else 0.0
    except Exception:
        pass
    return 0.0


def btc_realized_vol_ann(exchange) -> float:
    """Annualised 20-day realized volatility of BTC. Cached in-process for 1 h."""
    if _btc_vol_cache.get("ts", 0) + _BTC_VOL_TTL > time.time():
        return _btc_vol_cache["vol"]
    try:
        bars    = fetch_bars(exchange, "BTC/USDT:USDT", "1d", 22)
        log_ret = np.diff(np.log(bars["close"].astype(float)))[-20:]
        vol     = float(log_ret.std()) * (365 ** 0.5)
        _btc_vol_cache.update({"vol": vol, "ts": time.time()})
        return vol
    except Exception:
        return _btc_vol_cache.get("vol", 0.5)


def relative_strength_vs_btc(close_coin: np.ndarray, close_btc: np.ndarray, period: int = 20) -> float:
    """Return coin/BTC performance ratio over `period` bars.
    > 1.0 means coin outperforming BTC. Used as a momentum confirmation.
    """
    n = min(len(close_coin), len(close_btc), period + 1)
    if n < 2:
        return 1.0
    coin_ret = (close_coin[-1] - close_coin[-n]) / close_coin[-n] if close_coin[-n] > 0 else 0.0
    btc_ret  = (close_btc[-1]  - close_btc[-n])  / close_btc[-n]  if close_btc[-n]  > 0 else 0.0
    if btc_ret == 0:
        return 1.0
    return float((1 + coin_ret) / (1 + btc_ret))


def _scan_one(exchange, base: str, btc4h) -> tuple:
    """Fetch and compute all metrics for a single coin. Returns (base, metrics_dict) or (base, None)."""
    sym = f"{base}/USDT:USDT"
    try:
        b15    = fetch_bars(exchange, sym, "15m", 50)
        b1h    = fetch_bars(exchange, sym, "1h",  50)
        b4h    = fetch_bars(exchange, sym, "4h",  60)
        b1d    = fetch_bars(exchange, sym, "1d",  30)
        oi_chg = oi_change_pct(exchange, sym)
        fr     = funding_rate(exchange, sym)

        ema20_4h_arr   = ema_array(b4h["close"], 20)
        slope_lookback = 5
        ema20_4h_slope = (ema20_4h_arr[-1] - ema20_4h_arr[-1 - slope_lookback]) / ema20_4h_arr[-1]

        btc_corr  = correlation(btc4h["close"], b4h["close"]) if btc4h is not None else 0.0
        rs_vs_btc = relative_strength_vs_btc(b4h["close"], btc4h["close"], 20) if btc4h is not None else 1.0

        try:
            b1w          = fetch_bars(exchange, sym, "1w", 60)
            weekly_ema50 = ema(b1w["close"], 50)
            weekly_above = float(b1d["close"][-1]) > weekly_ema50
        except Exception:
            weekly_ema50 = float(b1d["close"][-1])
            weekly_above = True

        return base, {
            "price":          float(b4h["close"][-1]),
            "rsi_4h":         rsi(b4h["close"], 14),
            "rsi_1h":         rsi(b1h["close"], 14),
            "z8_4h":          zscore(b4h["close"], 8),
            "z20_4h":         zscore(b4h["close"], 20),
            "rvol_4h":        rvol(b4h["volume"], 20),
            "ema20_4h":       float(ema20_4h_arr[-1]),
            "ema20_4h_slope": float(ema20_4h_slope),
            "ema20_1d":       ema(b1d["close"], 20),
            "weekly_ema50":   float(weekly_ema50),
            "weekly_above":   weekly_above,
            "atr14_4h":       atr(b4h["high"], b4h["low"], b4h["close"], 14),
            "atr14_15m":      atr(b15["high"], b15["low"], b15["close"], 14),
            "btc_corr":       btc_corr,
            "rs_vs_btc":      rs_vs_btc,
            "oi_change_24h":  oi_chg,
            "funding_rate":   fr,
        }
    except Exception as e:
        print(f"indicators.scan {base}: {e}", file=sys.stderr)
        return base, None


def compute_scan(exchange, symbols: list, workers: int = 6) -> dict:
    """Compute all scan metrics for a list of BASE symbols.

    Uses a thread pool to fetch coins concurrently — reduces wall time from
    ~3 min (sequential, 40 coins) to ~30 s.  ccxt's rate limiter is per-object,
    so all threads share the same exchange instance and its built-in sleep.
    workers=6 is conservative; Bitget allows ~20 req/s.
    """
    try:
        btc4h = fetch_bars(exchange, "BTC/USDT:USDT", "4h", 60)
    except Exception as e:
        print(f"indicators: BTC bars failed: {e}", file=sys.stderr)
        btc4h = None

    result = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scan_one, exchange, base, btc4h): base for base in symbols}
        for fut in as_completed(futures):
            base, metrics = fut.result()
            if metrics is not None:
                result[base] = metrics
    return result


def compute_atr_map(exchange, bases: list) -> dict:
    """Return {BASE: atr14_4h} for open-position ATR trailing.
    4h ATR is more stable than 15m for multi-day hold positions.
    """
    result = {}
    for base in bases:
        try:
            b = fetch_bars(exchange, f"{base}/USDT:USDT", "4h", 30)
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
