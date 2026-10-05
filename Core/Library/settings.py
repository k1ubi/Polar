"""Centralised settings loaded from .env (via pydantic-settings)."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Bitget ──────────────────────────────────────────────────────────────
    bitget_api_key: str = Field(..., alias="BITGET_API_KEY")
    bitget_secret: str = Field(..., alias="BITGET_SECRET")
    bitget_passphrase: str = Field(..., alias="BITGET_PASSPHRASE")

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_chat_id: int = Field(..., alias="TELEGRAM_ADMIN_CHAT_ID")

    # ── Optional customer bot ────────────────────────────────────────────────
    telegram_customer_bot_token: str = Field("", alias="TELEGRAM_CUSTOMER_BOT_TOKEN")

    # ── Trading mode ─────────────────────────────────────────────────────────
    polar_live_trading: bool = Field(True, alias="POLAR_LIVE_TRADING")
    polar_live_trading_vpa: bool | None = Field(None, alias="POLAR_LIVE_TRADING_VPA")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def trading_mode(core: str | None = None) -> bool:
    """Return True if the given Core (or globally) is in live-trading mode."""
    s = get_settings()
    if core == "vpa" and s.polar_live_trading_vpa is not None:
        return s.polar_live_trading_vpa
    return s.polar_live_trading
