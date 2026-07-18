from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def parse_telegram_ids(value: object) -> frozenset[int]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, (set, frozenset, list, tuple)):
        parts = list(value)
    elif isinstance(value, str):
        parts = value.split(",")
    else:
        raise ValueError("Telegram ID list must be a comma-separated string")

    parsed: set[int] = set()
    for position, raw in enumerate(parts, start=1):
        text = str(raw).strip()
        if not text:
            raise ValueError(f"Telegram ID at position {position} is empty")
        if not text.isascii() or not text.isdecimal():
            raise ValueError(f"Telegram ID at position {position} must be numeric")
        telegram_id = int(text)
        if telegram_id <= 0:
            raise ValueError(f"Telegram ID at position {position} must be positive")
        parsed.add(telegram_id)
    return frozenset(parsed)


TelegramIds = Annotated[frozenset[int], NoDecode, BeforeValidator(parse_telegram_ids)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost/universal_service"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    redis_url: str = "redis://localhost:6379/0"
    secret_key: SecretStr | None = None
    session_encryption_key: SecretStr | None = None
    user_webapp_url: str = ""
    admin_webapp_url: str = ""
    telegram_auth_max_age_seconds: int = Field(default=300, ge=60, le=3600)
    web_session_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    web_rate_limit_per_minute: int = Field(default=60, ge=10, le=1000)
    max_receipt_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    cors_allowed_origins: str = ""
    trusted_hosts: str = "*"
    telegram_bot_token: SecretStr | None = None
    payment_card_encryption_key: SecretStr | None = None
    payment_review_chat_id: int | None = None
    payment_review_notify_superadmins: bool = True
    payment_review_notify_admins: bool = True
    superadmin_ids: TelegramIds = frozenset()
    initial_admin_ids: TelegramIds = frozenset()
    button_custom_emoji_ids: dict[str, str] = Field(default_factory=dict)

    myxvest_base_url: str = ""
    myxvest_api_key: SecretStr | None = None
    myxvest_enabled: bool = False
    myxvest_read_only_enabled: bool = True
    myxvest_purchase_enabled: bool = False
    myxvest_timeout_seconds: float = Field(default=20, gt=0, le=120)
    myxvest_balance_sync_seconds: int = Field(default=60, ge=15)
    myxvest_status_poll_seconds: int = Field(default=30, ge=10)
    myxvest_quote_ttl_seconds: int = Field(default=300, ge=30)
    myxvest_min_balance_alert_som: int = Field(default=50_000, ge=0)
    myxvest_max_retries: int = Field(default=3, ge=1, le=8)
    myxvest_max_concurrency: int = Field(default=5, ge=1, le=50)
    pricing_requires_superadmin_approval: bool = False
    min_profit_percent: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    min_profit_som: int = Field(default=0, ge=0)
    expected_alembic_head: str = "20260718_0010"
    backup_verified_at: datetime | None = None
    secrets_rotated_after_compromise: bool = False
    maintenance_mode: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env.casefold() == "production"

    @field_validator("button_custom_emoji_ids")
    @classmethod
    def validate_button_custom_emoji_ids(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_key, raw_id in value.items():
            key = raw_key.strip().casefold()
            emoji_id = str(raw_id).strip()
            if not key or not emoji_id.isascii() or not emoji_id.isdecimal():
                raise ValueError("Button custom emoji IDs must use non-empty keys and numeric IDs")
            normalized[key] = emoji_id
        return normalized

    @field_validator("database_url")
    @classmethod
    def normalize_database_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        return value

    @model_validator(mode="after")
    def validate_runtime_requirements(self) -> Settings:
        if self.is_production and not self.superadmin_ids:
            raise ValueError("SUPERADMIN_IDS must not be empty in production")
        if self.is_production:
            for name, secret in (
                ("SECRET_KEY", self.secret_key),
                ("SESSION_ENCRYPTION_KEY", self.session_encryption_key),
            ):
                if secret is None or len(secret.get_secret_value()) < 32:
                    raise ValueError(f"{name} must contain at least 32 characters in production")
        if self.myxvest_enabled:
            if not self.myxvest_base_url.strip():
                raise ValueError("MYXVEST_BASE_URL is required when Myxvest is enabled")
            if self.myxvest_api_key is None or not self.myxvest_api_key.get_secret_value():
                raise ValueError("MYXVEST_API_KEY is required when Myxvest is enabled")
        if self.myxvest_purchase_enabled and not self.myxvest_enabled:
            raise ValueError("MYXVEST_ENABLED must be true when purchases are enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
