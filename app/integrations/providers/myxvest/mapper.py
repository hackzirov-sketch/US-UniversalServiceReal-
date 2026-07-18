from __future__ import annotations

from typing import Any

from app.integrations.providers.myxvest.exceptions import MyxvestInvalidResponseError
from app.integrations.providers.myxvest.schemas import ProviderServiceType, ProviderStatus


def map_status(value: object) -> ProviderStatus:
    normalized = str(value).strip().casefold()
    mapping = {
        "pending": ProviderStatus.PENDING,
        "new": ProviderStatus.PENDING,
        "processing": ProviderStatus.PROCESSING,
        "in_progress": ProviderStatus.PROCESSING,
        "completed": ProviderStatus.COMPLETED,
        "success": ProviderStatus.COMPLETED,
        "done": ProviderStatus.COMPLETED,
        "refunded": ProviderStatus.REFUNDED,
        "refund": ProviderStatus.REFUNDED,
        "failed": ProviderStatus.FAILED,
        "error": ProviderStatus.FAILED,
    }
    return mapping.get(normalized, ProviderStatus.UNKNOWN)


def map_service_type(value: object) -> ProviderServiceType:
    normalized = str(value).strip().casefold()
    mapping = {
        "stars": ProviderServiceType.STARS,
        "telegram_stars": ProviderServiceType.STARS,
        "buy_stars": ProviderServiceType.STARS,
        "premium": ProviderServiceType.PREMIUM,
        "telegram_premium": ProviderServiceType.PREMIUM,
        "buy_premium": ProviderServiceType.PREMIUM,
        "gift": ProviderServiceType.GIFT,
        "gifts": ProviderServiceType.GIFT,
        "buy_gift": ProviderServiceType.GIFT,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise MyxvestInvalidResponseError("Unknown provider service type") from exc


def require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MyxvestInvalidResponseError("Provider response must be a JSON object")
    return value
