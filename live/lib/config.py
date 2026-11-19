"""Shared configuration loader. Reads live/.env on first import.
All credentials come from environment variables; this file contains no secrets.
"""
import os
from pathlib import Path


def _load_env():
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

# Telegram — required at runtime; empty string safe for dry-runs (tg() checks LIVE flag)
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT_ID", "")

# ── Strategy parameters ───────────────────────────────────────────────────────
# Single source of truth — scanner.py and guardian.py import from here.
STRATEGY = {
    # Entry
    "score_threshold":     7,      # min score to pass to AI reviewer (max 8)
    "leverage":            3,      # futures leverage (keep in sync with Bitget account)
    "risk_per_trade":      0.015,  # fraction of equity risked per trade (1.5%)
    "max_risk_pct":        2.0,    # hard cap on risk % after lot rounding
    "max_positions":       5,      # total open + pending cap
    "max_per_direction":   2,      # max longs or shorts at once
    "min_free_margin":     15,     # USD — skip entry if free margin below this
    # Stop / TP
    "atr_sl_mult":         1.5,    # stop distance = ATR × this
    "sl_min":              0.015,  # minimum stop distance (1.5%)
    "sl_max":              0.06,   # maximum stop distance (6%)
    "tp1_r":               1.0,    # TP1 at 1×R (50% of position)
    "tp2_r":               2.5,    # TP2 at 2.5×R (remaining 50%)
    # Volatility / sizing
    "btc_vol_threshold":   0.80,   # annualised BTC vol above which we scale down
    "btc_vol_scale":       0.50,   # size multiplier in high-vol regime
    "drawdown_threshold":  0.08,   # 7-day drawdown above which we scale down
    "drawdown_scale":      0.50,   # size multiplier when in drawdown
    "corr_total_cap":      1.5,    # sum of portfolio BTC-corr above which we skip new entries
    # Exits
    "entry_timeout_hours": 24,     # cancel unfilled entry after this
    "funding_long_exit":   0.001,  # exit long if funding > 0.1%/8h
    "funding_short_exit": -0.0005, # exit short if funding < -0.05%/8h
    "time_stop_hours":     40,     # time-based exit trigger
    "time_stop_loss_r":    0.3,    # only time-stop if down > 0.3R
}
