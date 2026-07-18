from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_username(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    normalized = normalized.strip()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("Telegram username must contain 5-32 letters, digits or underscores")
    return normalized


class PremiumMonths(IntEnum):
    THREE = 3
    SIX = 6
    TWELVE = 12


class ProviderServiceType(StrEnum):
    STARS = "STARS"
    PREMIUM = "PREMIUM"
    GIFT = "GIFT"


class ProviderStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderResponseSchema(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProviderBalance(ProviderResponseSchema):
    balance_som: int = Field(ge=0)
    currency: str | None = None
    provider_name: str | None = None
    total_orders: int | None = Field(default=None, ge=0)
    total_spent_som: int | None = Field(default=None, ge=0)


class ProviderService(ProviderResponseSchema):
    external_service_id: str
    service_type: ProviderServiceType
    name: str
    provider_price_som: int | None = Field(default=None, ge=0)
    min_quantity: int | None = Field(default=None, ge=1)
    max_quantity: int | None = Field(default=None, ge=1)
    active: bool = True
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    required_params: list[str] = Field(default_factory=list)


class ProviderOrderResult(ProviderResponseSchema):
    provider_order_id: str
    status: ProviderStatus
    charged_amount_som: int | None = Field(default=None, ge=0)
    duplicate: bool = False


class ProviderOrderStatus(ProviderResponseSchema):
    provider_order_id: str
    status: ProviderStatus
    refunded_amount_som: int | None = Field(default=None, ge=0)


class ProviderApiError(ProviderResponseSchema):
    code: str
    message: str


class PurchaseRequest(StrictSchema):
    username: str
    idempotency_key: str = Field(min_length=12, max_length=255)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return normalize_username(value)


class StarsPurchaseRequest(PurchaseRequest):
    quantity: int = Field(ge=50, le=1_000_000)


class PremiumPurchaseRequest(PurchaseRequest):
    months: PremiumMonths


class GiftPurchaseRequest(PurchaseRequest):
    gift_name: str | None = Field(default=None, min_length=1, max_length=128)
    gift_id: str | None = Field(default=None, min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def resolve_legacy_gift_identifier(self) -> GiftPurchaseRequest:
        if not self.gift_name and not self.gift_id:
            raise ValueError("gift_name is required")
        if self.gift_name and self.gift_id and self.gift_name != self.gift_id:
            raise ValueError("gift_name and legacy gift_id conflict")
        return self

    @property
    def resolved_gift_name(self) -> str:
        return self.gift_name or self.gift_id or ""
