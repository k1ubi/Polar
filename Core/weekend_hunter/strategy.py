"""Weekend Hunter Ultimate v6.2 [PRODUCTION READY] — faithful Python port.

Source: TradingView, Pine Script v6, author YOUNGPSI —
https://it.tradingview.com/script/1Y4pNLSm-Weekend-Hunter-Ultimate-v6-2/

This is a line-by-line translation of the real `strategy()` script (the
actual Pine source, not a reconstruction from the page's prose
description) — every constant, threshold, and formula below has a direct
Pine Script counterpart, called out in comments.

Two things could not be translated 1:1, and are explicitly flagged:

1. CVD (cumulative volume delta): Pine's `var float cvd = 0.0` accumulates
   forever across the whole chart's history. This module computes it as a
   cumsum over whatever historical window is fetched instead — but this is
   mathematically EXACT for what the script actually uses it for: the
   divergence check only ever compares `cvd` to `cvd[10]` (10 bars ago),
   i.e. the sum of volume_delta over the last 10 bars, which is identical
   regardless of where the cumulative sum "started" (as long as the fetch
   window includes at least 10 bars before the comparison point).
2. TradingView's `strategy.exit(..., trail_points=..., trail_offset=...)`
   trailing-stop mechanics are TradingView's own internal order-management
   behavior (activates a trailing stop once price moves
   trailing_activation_percent in favor, then trails
   trailing_distance_percent behind the running extreme) — reimplemented
   here as an explicit ratchet in `update_trailing_stop()`, matching the
   same activation/distance semantics, since there's no equivalent broker
   primitive to just call.

Everything else (signal_strength scoring, per-symbol leverage risk
profiles, ATR-based SL/TP with leverage adjustment, weekend gating, loss
circuit breakers, flash-crash/liquidation-proximity emergency exits) is a
direct translation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from Core.Library.indicators import compute_atr, compute_macd, compute_rsi

# ========================================
# Weekend-only configuration (Pine: "WEEKEND-ONLY CONFIGURATION")
# ========================================
ENABLE_WEEKEND_ONLY = True  # Pine: enable_weekend_only input, default true
OPTIMAL_SATURDAY_START_HOUR = 0  # Pine: optimal_saturday_start input, default 0
OPTIMAL_SUNDAY_END_HOUR = 23  # Pine: optimal_sunday_end input, default 23
PRIME_SUNDAY_WINDOW_ENABLED = True  # Pine: prime_sunday_window input, default true


def weekend_status(now: datetime) -> tuple[bool, bool, bool]:
    """Returns (is_weekend, in_prime_window, weekend_trading_active).
    `now` should be UTC (Pine's dayofweek/hour() are UTC on most crypto
    feeds unless the chart timezone is changed, which this script doesn't
    do).

    IMPORTANT: with ENABLE_WEEKEND_ONLY at its Pine default (True), on any
    non-Saturday/Sunday day `weekend_trading_active` is False and NO
    signal will ever qualify — this matches the source exactly, it is not
    a bug. Set ENABLE_WEEKEND_ONLY=False to test outside weekend hours (a
    deviation from the source, not the default)."""
    is_saturday = now.weekday() == 5  # Python Monday=0..Sunday=6; Pine dayofweek.saturday
    is_sunday = now.weekday() == 6
    is_weekend = is_saturday or is_sunday
    hour = now.hour

    in_prime_window = PRIME_SUNDAY_WINDOW_ENABLED and is_sunday and 0 <= hour <= 8
    in_weekend_hours = (is_saturday and hour >= OPTIMAL_SATURDAY_START_HOUR) or \
                       (is_sunday and hour <= OPTIMAL_SUNDAY_END_HOUR)
    weekend_trading_active = (is_weekend and in_weekend_hours) if ENABLE_WEEKEND_ONLY else True
    return is_weekend, in_prime_window, weekend_trading_active


# ========================================
# Position & capital management (Pine: "POSITION & CAPITAL MANAGEMENT")
# ========================================
MAX_CONCURRENT_POSITIONS = 4  # Pine: max_concurrent_positions
MIN_CAPITAL_PER_TRADE = 120.0  # Pine: min_capital_per_trade
SAFETY_BUFFER = 50.0  # Pine: safety_buffer

# ========================================
# Aggressive 65% win-rate configuration (Pine defaults, all inputs)
# ========================================
MAX_DAILY_LOSS_PCT = 5.0  # Pine: max_daily_loss input, default 5.0
MAX_WEEKEND_LOSS_PCT = 15.0  # Pine: max_weekend_loss input, default 15.0
MAX_CONSECUTIVE_LOSSES_ALLOWED = 4  # Pine: max_consecutive_losses_allowed

# ========================================
# Dynamic leverage system (Pine: "DYNAMIC LEVERAGE SYSTEM 5-20x")
# ========================================
MIN_LEVERAGE = 5.0
MAX_LEVERAGE = 20.0

# Pine: symbol_risk_profile — EXACT coin-specific table from the source.
# Bitget symbols are "BTCUSDT" (no slash) — matches syminfo.ticker format.
SYMBOL_RISK_PROFILE = {
    "BTCUSDT": 0.9, "ETHUSDT": 0.85, "LINKUSDT": 0.75, "XRPUSDT": 0.75,
    "DOGEUSDT": 0.6, "SOLUSDT": 0.7, "AVAXUSDT": 0.7, "1000PEPEUSDT": 0.5,
    "TONUSDT": 0.65, "POLUSDT": 0.7,
}
DEFAULT_SYMBOL_RISK_PROFILE = 0.5  # Pine's trailing ": 0.5" fallback

# ========================================
# Profit protection / trailing (Pine: "PROFIT PROTECTION SYSTEM")
# ========================================
USE_TRAILING = True
TRAILING_ACTIVATION_PCT = 2.5  # Pine: trailing_activation_percent input, default 2.5
TRAILING_DISTANCE_PCT = 1.5  # Pine: trailing_distance_percent input, default 1.5

# ========================================
# Signal generation thresholds (Pine defaults, all inputs)
# ========================================
RSI_PERIOD = 8  # Pine: ta.rsi(close, 8) -- NOT the usual 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 8, 17, 9  # Pine: ta.macd(close, 8, 17, 9) -- NOT the usual 12/26/9
BB_PERIOD, BB_STD_DEV = 20, 2.5  # Pine: ta.bb(close, 20, 2.5) -- NOT the usual 2.0
ATR_PERIOD = 14
MIN_SIGNAL_STRENGTH = 55.0  # Pine: min_signal_strength input, default 55.0
HIGH_CONFIDENCE_THRESHOLD = 75.0  # Pine: high_confidence_threshold input, default 75.0
BASE_ATR_MULT_SL = 1.5  # Pine: base_atr_mult_sl input, default 1.5
BASE_ATR_MULT_TP = 4.5  # Pine: base_atr_mult_tp input, default 4.5
FLASH_CRASH_THRESHOLD = 0.08  # Pine: flash_crash_detected, 8% move over 4 bars
ACCOUNT_EMERGENCY_STOP_PCT = 0.30  # Pine: strategy.equity < initial_capital * 0.7


def compute_bollinger_bands(close: pd.Series, period: int = BB_PERIOD, std_dev: float = BB_STD_DEV) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = close.rolling(period).mean()
    std = close.rolling(period).std()
    return middle + std_dev * std, middle, middle - std_dev * std  # upper, middle, lower


def compute_cvd(df: pd.DataFrame) -> pd.Series:
    """Pine: bull_volume/bear_volume/volume_delta/cvd — see module
    docstring for why a windowed cumsum is exact here, not approximate."""
    bull_volume = df["volume"].where(df["close"] > df["open"], 0.0)
    bear_volume = df["volume"].where(df["close"] < df["open"], 0.0)
    volume_delta = bull_volume - bear_volume
    return volume_delta.cumsum()


def cvd_divergence(close: pd.Series, cvd: pd.Series, lookback: int = 10) -> tuple[bool, bool]:
    """Pine: bearish_divergence/bullish_divergence, exact 10-bar lookback
    comparison (deliberately a simple fixed-lookback check, matching the
    source exactly, not a generic divergence algorithm)."""
    if len(close) <= lookback:
        return False, False
    price_higher = close.iloc[-1] > close.iloc[-1 - lookback]
    cvd_lower = cvd.iloc[-1] < cvd.iloc[-1 - lookback]
    bearish_divergence = price_higher and cvd_lower

    price_lower = close.iloc[-1] < close.iloc[-1 - lookback]
    cvd_higher = cvd.iloc[-1] > cvd.iloc[-1 - lookback]
    bullish_divergence = price_lower and cvd_higher
    return bullish_divergence, bearish_divergence


@dataclass(frozen=True)
class WeekendHunterSignal:
    direction: str  # "bullish" | "bearish"
    signal_strength: float
    final_leverage: float
    notes: str
    entry_price: float


def _pine_ticker(symbol: str) -> str:
    """"BTC/USDT" -> "BTCUSDT" -- matches Pine's syminfo.ticker format."""
    return symbol.replace("/", "")


def generate_signal(
    df_exec: pd.DataFrame, df_4h: pd.DataFrame, df_1h: pd.DataFrame, symbol: str, now: datetime,
) -> WeekendHunterSignal | None:
    """`df_exec` is the execution-timeframe OHLCV (verified against the
    DAILY chart for BTC/ETH); `df_4h`/`df_1h` are SEPARATE fetches at
    those literal timeframes — Pine's `request.security(syminfo.tickerid,
    "240"/"60", ...)` pulls real 4h/1h data regardless of chart timeframe,
    which can't be derived by resampling an execution timeframe coarser
    than 4h/1h (e.g. daily)."""
    min_bars = max(BB_PERIOD, 20, 35) + 15
    if len(df_exec) < min_bars or len(df_4h) < 55 or len(df_1h) < 35:
        return None

    is_weekend, in_prime_window, weekend_trading_active = weekend_status(now)

    close = df_exec["close"]
    volume = df_exec["volume"]

    # --- Multi-timeframe confluence ---
    htf_4h_close = df_4h["close"].iloc[-1]
    htf_4h_ema50 = df_4h["close"].ewm(span=50).mean().iloc[-1]
    htf_4h_trend = 1 if htf_4h_close > htf_4h_ema50 else -1

    htf_1h_macd, htf_1h_signal = compute_macd(df_1h["close"], 12, 26, 9)
    htf_1h_momentum = 1 if htf_1h_macd.iloc[-1] > htf_1h_signal.iloc[-1] else -1

    current_ema20 = close.ewm(span=20).mean().iloc[-1]
    current_trend = 1 if close.iloc[-1] > current_ema20 else -1

    trend_alignment = (htf_4h_trend + htf_1h_momentum + current_trend) / 3.0

    # --- Volatility regime ---
    atr_series = compute_atr(df_exec, period=ATR_PERIOD)
    atr_value = atr_series.iloc[-1]
    if pd.isna(atr_value) or atr_value <= 0:
        return None
    atr_percentage = (atr_value / close.iloc[-1]) * 100
    volatility_regime = "Low" if atr_percentage < 2.0 else "Medium" if atr_percentage < 5.0 else "High"
    volatility_safety = {"Low": 1.0, "Medium": 0.7, "High": 0.4}[volatility_regime]

    # --- Volume / liquidity ---
    volume_sma20 = volume.rolling(20).mean().iloc[-1]
    current_liquidity_ratio = volume.iloc[-1] / volume_sma20 if volume_sma20 > 0 else 0
    volume_surge = volume.iloc[-1] > (volume_sma20 * 1.5)
    volume_sma100 = volume.rolling(100).mean().iloc[-1] if len(volume) >= 100 else volume.mean()
    min_volume_threshold = volume_sma100 * 0.1
    valid_volume = volume.iloc[-1] > min_volume_threshold
    volume_confirmation = volume_surge and current_liquidity_ratio > 0.7 and valid_volume

    # --- CVD divergence ---
    cvd = compute_cvd(df_exec)
    bullish_divergence, bearish_divergence = cvd_divergence(close, cvd, lookback=10)

    # --- Primary indicators ---
    rsi_series = compute_rsi(close, RSI_PERIOD)
    rsi = rsi_series.iloc[-1]
    rsi_prev = rsi_series.iloc[-2]

    # --- Signal strength (weighted composite, NOT a confluence count) ---
    trend_score = trend_alignment * 40
    momentum_bullish = rsi < 30 and rsi > rsi_prev
    momentum_bearish = rsi > 70 and rsi < rsi_prev
    momentum_score = 30 if momentum_bullish else -30 if momentum_bearish else 0
    volume_score = (20 if volume_confirmation else 0) + (10 if bullish_divergence else -10 if bearish_divergence else 0)
    weekend_score = 10.0 if is_weekend else 0.0
    if in_prime_window:
        weekend_score *= 1.5

    signal_strength = trend_score + momentum_score + volume_score + weekend_score
    if is_weekend:
        signal_strength *= 1.2
    if in_prime_window:
        signal_strength *= 1.3

    # --- Dynamic leverage ---
    signal_confidence = abs(signal_strength) / 100
    ticker = _pine_ticker(symbol)
    symbol_risk_profile = SYMBOL_RISK_PROFILE.get(ticker, DEFAULT_SYMBOL_RISK_PROFILE)
    overall_safety = (volatility_safety + signal_confidence) / 2 * symbol_risk_profile
    dynamic_leverage = MIN_LEVERAGE + (MAX_LEVERAGE - MIN_LEVERAGE) * overall_safety
    final_leverage = round(max(MIN_LEVERAGE, min(MAX_LEVERAGE, dynamic_leverage)), 1)

    # --- Entry conditions ---
    strong_bullish_signal = signal_strength > MIN_SIGNAL_STRENGTH and trend_alignment > 0
    strong_bearish_signal = signal_strength < -MIN_SIGNAL_STRENGTH and trend_alignment < 0

    if len(close) > 4:
        flash_crash_detected = abs((close.iloc[-1] - close.iloc[-5]) / close.iloc[-5]) > FLASH_CRASH_THRESHOLD
    else:
        flash_crash_detected = False

    if strong_bullish_signal and weekend_trading_active and not flash_crash_detected:
        direction = "bullish"
    elif strong_bearish_signal and weekend_trading_active and not flash_crash_detected:
        direction = "bearish"
    else:
        return None

    high_confidence = abs(signal_strength) > HIGH_CONFIDENCE_THRESHOLD
    notes = (
        f"signal_strength={signal_strength:.1f} (trend={trend_score:+.1f} momentum={momentum_score:+.0f} "
        f"volume={volume_score:+.0f} weekend={weekend_score:+.1f}), leverage={final_leverage}x, "
        f"vol_regime={volatility_regime}, {'high-confidence' if high_confidence else 'standard'}"
    )
    return WeekendHunterSignal(
        direction=direction, signal_strength=signal_strength, final_leverage=final_leverage,
        notes=notes, entry_price=float(close.iloc[-1]),
    )


def position_size_usd(signal: WeekendHunterSignal, symbol: str) -> float:
    """Pine: base_position_usd/desired_position_usd/pair_multiplier/
    final_position_usd — absolute-dollar sizing, not wallet-percent."""
    high_confidence = abs(signal.signal_strength) > HIGH_CONFIDENCE_THRESHOLD
    position_multiplier = 1.3 if high_confidence else 1.0
    base_position_usd = 120.0
    desired_position_usd = base_position_usd * position_multiplier

    ticker = _pine_ticker(symbol)
    pair_multiplier = 0.5 if ticker == "1000PEPEUSDT" else 1.0 if ticker in ("BTCUSDT", "ETHUSDT") else 0.8
    return desired_position_usd * pair_multiplier


def compute_levels(direction: str, entry_price: float, atr_value: float, final_leverage: float, signal_confidence: float) -> tuple[float, float]:
    """Pine: leverage_adjustment/atr_mult_sl/atr_mult_tp/stop_distance/
    tp_distance/long_stop/long_tp/short_stop/short_tp."""
    leverage_adjustment = 1.0 + (final_leverage - 5) / 50
    atr_mult_sl = BASE_ATR_MULT_SL / leverage_adjustment
    atr_mult_tp = BASE_ATR_MULT_TP * (1.0 + signal_confidence * 0.2)

    stop_distance = atr_value * atr_mult_sl
    tp_distance = atr_value * atr_mult_tp

    if direction == "bullish":
        return entry_price + tp_distance, entry_price - stop_distance
    return entry_price - tp_distance, entry_price + stop_distance


def update_trailing_stop(direction: str, entry_price: float, current_price: float, current_sl: float) -> float:
    """Reimplementation of TradingView's trail_points/trail_offset
    mechanics — see module docstring. Only ratchets in the favorable
    direction, never loosens."""
    is_long = direction == "bullish"
    profit_pct = ((current_price - entry_price) / entry_price * 100) if is_long else ((entry_price - current_price) / entry_price * 100)
    if profit_pct < TRAILING_ACTIVATION_PCT:
        return current_sl
    trail_distance = current_price * TRAILING_DISTANCE_PCT / 100
    candidate_sl = current_price - trail_distance if is_long else current_price + trail_distance
    if is_long:
        return max(current_sl, candidate_sl)
    return min(current_sl, candidate_sl)


def liquidation_proximity_pct(final_leverage: float) -> float:
    """Pine: liq_distance = 100 / final_leverage * 0.8."""
    return 100 / final_leverage * 0.8
