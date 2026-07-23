from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.db.enums import ServiceType
from app.db.models import ManualProviderPrice
from app.db.session import session_factory
from app.services.bootstrap import bootstrap_defaults
from app.services.manual_pricing import (
    ManualPriceInput,
    PricingActor,
    create_manual_price,
    service_key_for,
)


def progressive_markup(provider_cost_som: int) -> int:
    """Return about one percent profit, constrained to 1–10,000 so‘m."""
    return max(1, min(10_000, (provider_cost_som + 50) // 100))


def catalog_prices() -> tuple[ManualPriceInput, ...]:
    premium = ((3, 159_587), (6, 212_827), (12, 385_857))
    gifts = (
        ("heart", "💝 Yurak", 2_900),
        ("bear", "🧸 Ayiqcha", 2_900),
        ("gift", "🎁 Sovg‘a", 4_900),
        ("rose", "🌹 Atirgul", 4_900),
        ("cake", "🎂 Tort", 9_700),
        ("flowers", "💐 Gullar", 9_700),
        ("rocket", "🚀 Raketa", 9_700),
        ("bottle", "🍾 Ichimlik", 9_700),
        ("trophy", "🏆 Kubok", 19_300),
        ("ring", "💍 Uzuk", 19_300),
        ("diamond", "💎 Olmos", 19_300),
    )
    rows = [
        ManualPriceInput(
            service_type=ServiceType.STARS,
            provider_cost_som=189,
            sale_price_som=190,
            display_name="Telegram Stars",
            min_quantity=50,
            max_quantity=10_000,
            duration_hours=None,
            source_note="Reference catalog pricing with progressive markup",
        )
    ]
    rows.extend(
        ManualPriceInput(
            service_type=ServiceType.PREMIUM,
            provider_cost_som=cost,
            sale_price_som=cost + progressive_markup(cost),
            display_name=f"Telegram Premium — {months} oy",
            premium_months=months,
            sort_order=months,
            duration_hours=None,
            source_note="Reference catalog pricing with progressive markup",
        )
        for months, cost in premium
    )
    rows.extend(
        ManualPriceInput(
            service_type=ServiceType.GIFT,
            provider_cost_som=cost,
            sale_price_som=cost + progressive_markup(cost),
            display_name=display_name,
            gift_name=gift_name,
            sort_order=index,
            duration_hours=None,
            source_note="Reference catalog pricing with progressive markup",
        )
        for index, (gift_name, display_name, cost) in enumerate(gifts, start=1)
    )
    return tuple(rows)


async def seed_catalog() -> tuple[int, int]:
    settings = get_settings()
    if not settings.superadmin_ids:
        raise RuntimeError("At least one SUPERADMIN_ID is required to seed catalog prices")
    actor_id = min(settings.superadmin_ids)
    actor = PricingActor(actor_id, True, True)
    created = 0
    skipped = 0
    async with session_factory.begin() as session:
        await bootstrap_defaults(
            session,
            initial_admin_ids=settings.initial_admin_ids,
            superadmin_ids=settings.superadmin_ids,
        )
        await session.flush()
        for price_input in catalog_prices():
            service_key = service_key_for(price_input)
            existing = await session.scalar(
                select(ManualProviderPrice.id).where(
                    ManualProviderPrice.service_key == service_key,
                    ManualProviderPrice.active.is_(True),
                )
            )
            if existing is not None:
                skipped += 1
                continue
            await create_manual_price(
                session,
                data=price_input,
                actor=actor,
                requires_superadmin_approval=False,
                min_profit_percent=settings.min_profit_percent,
                min_profit_som=settings.min_profit_som,
            )
            created += 1
    return created, skipped


async def _main() -> None:
    created, skipped = await seed_catalog()
    print(f"Catalog prices created: {created}; existing prices kept: {skipped}")


if __name__ == "__main__":
    asyncio.run(_main())
