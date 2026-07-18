from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class PricingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QuoteCalculation:
    provider_cost_som: int
    markup_som: int
    risk_buffer_som: int
    sale_price_som: int
    expires_at: datetime


def calculate_quote(
    *,
    provider_cost_som: int,
    fixed_markup_som: int,
    percentage_markup_bps: int,
    minimum_profit_som: int,
    risk_buffer_som: int,
    ttl_seconds: int,
    now: datetime | None = None,
) -> QuoteCalculation:
    values = (
        provider_cost_som,
        fixed_markup_som,
        percentage_markup_bps,
        minimum_profit_som,
        risk_buffer_som,
    )
    if any(value < 0 for value in values):
        raise PricingError("Pricing values must not be negative")
    percentage = (provider_cost_som * percentage_markup_bps + 9_999) // 10_000
    markup = max(fixed_markup_som + percentage, minimum_profit_som)
    sale_price = provider_cost_som + markup + risk_buffer_som
    if sale_price < provider_cost_som:
        raise PricingError("Sale price must not be below provider cost")
    created_at = now or datetime.now(UTC)
    return QuoteCalculation(
        provider_cost_som=provider_cost_som,
        markup_som=markup,
        risk_buffer_som=risk_buffer_som,
        sale_price_som=sale_price,
        expires_at=created_at + timedelta(seconds=ttl_seconds),
    )


def quote_is_expired(expires_at: datetime, *, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= current


def price_is_unsafe(*, current_provider_cost_som: int, sale_price_som: int) -> bool:
    return current_provider_cost_som > sale_price_som
