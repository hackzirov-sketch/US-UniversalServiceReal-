import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.enums import ManualPriceStatus, OrderStatus, PriceSource, ProviderState, ServiceType
from app.db.models import (
    AdminPermission,
    AuditLog,
    ManualProviderPrice,
    Order,
    PricingRule,
    Provider,
    ProviderService,
    User,
)
from app.services.manual_pricing import (
    MANAGE_PRICING,
    ManualPriceBelowCostError,
    ManualPriceInput,
    ManualPriceValidationError,
    PriceTemporarilyUnavailableError,
    PricingActor,
    PricingPermissionError,
    create_manual_price,
    create_price_quote,
    list_active_manual_prices,
    pricing_actor,
)
from app.services.provider import ProviderWorkflow


async def seed_pricing(sessions, *, with_api_stars: bool = False):
    async with sessions.begin() as session:
        provider = Provider(
            code="MYXVEST", name="Myxvest", enabled=True, status=ProviderState.AVAILABLE
        )
        user = User(telegram_id=10001, available_balance_som=1_000_000)
        admin = User(telegram_id=20001, is_admin=True)
        session.add_all([provider, user, admin])
        await session.flush()
        if with_api_stars:
            session.add_all(
                [
                    ProviderService(
                        provider_id=provider.id,
                        external_service_id="buy_stars",
                        service_type=ServiceType.STARS,
                        name="Telegram Stars",
                        provider_price_som=189,
                        min_quantity=50,
                        max_quantity=10_000,
                        active=True,
                        raw_metadata={},
                        synced_at=datetime.now(UTC),
                    ),
                    PricingRule(
                        service_type=ServiceType.STARS,
                        enabled=True,
                        fixed_markup_som=0,
                        percentage_markup_bps=1000,
                        minimum_profit_som=0,
                        risk_buffer_som=0,
                    ),
                ]
            )
        return provider.id, user.id, admin.id


def superadmin() -> PricingActor:
    return PricingActor(telegram_id=90001, is_superadmin=True, can_manage_pricing=True)


def stars_input(**changes) -> ManualPriceInput:
    values = {
        "service_type": ServiceType.STARS,
        "provider_cost_som": 189,
        "sale_price_som": 210,
        "display_name": "Telegram Stars",
        "min_quantity": 50,
        "max_quantity": 10_000,
        "duration_hours": 24,
        "source_note": "Original Myxvest bot",
    }
    values.update(changes)
    return ManualPriceInput(**values)


async def save_price(session, data, actor=None, now=None):
    return await create_manual_price(
        session,
        data=data,
        actor=actor or superadmin(),
        requires_superadmin_approval=False,
        min_profit_percent=Decimal("5"),
        min_profit_som=1,
        now=now,
    )


@pytest.mark.asyncio
async def test_superadmin_creates_stars_price_and_audit(sessions) -> None:
    await seed_pricing(sessions)
    async with sessions.begin() as session:
        price = await save_price(session, stars_input())
        assert price.service_key == "MYXVEST:STARS"
        assert price.version == 1
        assert price.status == ManualPriceStatus.ACTIVE
    async with sessions() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "MANUAL_PRICE_CREATED")
        )
        assert audit.sanitized_metadata["new_price"]["sale_price_som"] == 210


@pytest.mark.asyncio
async def test_admin_without_permission_cannot_create_price(sessions) -> None:
    await seed_pricing(sessions)
    actor = PricingActor(telegram_id=20001, is_superadmin=False, can_manage_pricing=False)
    with pytest.raises(PricingPermissionError):
        async with sessions.begin() as session:
            await save_price(session, stars_input(), actor=actor)


def test_below_cost_and_invalid_premium_are_rejected() -> None:
    with pytest.raises(ManualPriceBelowCostError):
        asyncio.run(_validate_without_db(stars_input(provider_cost_som=211, sale_price_som=210)))
    invalid = ManualPriceInput(
        service_type=ServiceType.PREMIUM,
        provider_cost_som=100,
        sale_price_som=120,
        display_name="Premium",
        premium_months=4,
    )
    with pytest.raises(ManualPriceValidationError):
        asyncio.run(_validate_without_db(invalid))


async def _validate_without_db(data):
    from app.services.manual_pricing import validate_price_input

    validate_price_input(data)


@pytest.mark.asyncio
async def test_stars_total_and_manual_override_api_price(sessions) -> None:
    _, user_id, _ = await seed_pricing(sessions, with_api_stars=True)
    async with sessions.begin() as session:
        await save_price(session, stars_input())
    async with sessions.begin() as session:
        quote = await create_price_quote(
            session,
            user_id=user_id,
            service_type=ServiceType.STARS,
            quantity=100,
            quote_ttl_seconds=300,
        )
        assert quote.provider_cost_som == 18_900
        assert quote.sale_price_som == 21_000
        assert quote.expected_profit_som == 2_100
        assert quote.price_source == PriceSource.MANUAL_OVERRIDE


@pytest.mark.asyncio
async def test_premium_packages_have_separate_prices(sessions) -> None:
    await seed_pricing(sessions)
    async with sessions.begin() as session:
        for months, cost, sale in ((3, 100, 120), (6, 180, 220), (12, 300, 360)):
            await save_price(
                session,
                ManualPriceInput(
                    service_type=ServiceType.PREMIUM,
                    provider_cost_som=cost,
                    sale_price_som=sale,
                    display_name=f"Premium {months}",
                    premium_months=months,
                ),
            )
    async with sessions() as session:
        keys = set(await session.scalars(select(ManualProviderPrice.service_key)))
        assert keys == {
            "MYXVEST:PREMIUM:3",
            "MYXVEST:PREMIUM:6",
            "MYXVEST:PREMIUM:12",
        }


@pytest.mark.asyncio
async def test_gift_name_created_and_inactive_gift_hidden(sessions) -> None:
    await seed_pricing(sessions)
    async with sessions.begin() as session:
        gift = await save_price(
            session,
            ManualPriceInput(
                service_type=ServiceType.GIFT,
                provider_cost_som=30_000,
                sale_price_som=35_000,
                display_name="Atirgul",
                gift_name="Rose",
                allow_comment=True,
                sort_order=10,
                active=False,
            ),
        )
        assert gift.gift_name == "Rose"
        assert gift.sort_order == 10
    async with sessions() as session:
        assert await list_active_manual_prices(session, service_type=ServiceType.GIFT) == []


@pytest.mark.asyncio
async def test_expired_price_is_unavailable_but_existing_quote_snapshot_survives(sessions) -> None:
    _, user_id, _ = await seed_pricing(sessions)
    base = datetime.now(UTC)
    async with sessions.begin() as session:
        first = await save_price(session, stars_input(duration_hours=1), now=base)
    async with sessions.begin() as session:
        quote = await create_price_quote(
            session,
            user_id=user_id,
            service_type=ServiceType.STARS,
            quantity=100,
            quote_ttl_seconds=300,
            now=base + timedelta(minutes=30),
        )
        await session.flush()
        quote_id = quote.id
        assert quote.manual_price_id == first.id
    async with sessions.begin() as session:
        await save_price(
            session,
            stars_input(provider_cost_som=200, sale_price_som=230),
            now=base + timedelta(minutes=40),
        )
    async with sessions() as session:
        old_quote = await session.get(type(quote), quote_id)
        assert old_quote.sale_price_som == 21_000
        assert old_quote.price_version == 1
    with pytest.raises(PriceTemporarilyUnavailableError):
        async with sessions.begin() as session:
            await create_price_quote(
                session,
                user_id=user_id,
                service_type=ServiceType.PREMIUM,
                premium_months=3,
                quote_ttl_seconds=300,
                now=base + timedelta(hours=2),
            )


@pytest.mark.asyncio
async def test_manage_pricing_permission_is_database_backed(sessions) -> None:
    _, _, admin_id = await seed_pricing(sessions)
    async with sessions.begin() as session:
        session.add(
            AdminPermission(
                user_id=admin_id,
                permission=MANAGE_PRICING,
                granted_by_telegram_id=90001,
            )
        )
    async with sessions() as session:
        actor = await pricing_actor(session, telegram_id=20001, superadmin_ids=frozenset())
        assert actor.can_manage_pricing is True


@pytest.mark.asyncio
async def test_low_profit_admin_price_becomes_draft(sessions) -> None:
    await seed_pricing(sessions)
    actor = PricingActor(telegram_id=20001, is_superadmin=False, can_manage_pricing=True)
    async with sessions.begin() as session:
        price = await create_manual_price(
            session,
            data=stars_input(provider_cost_som=100, sale_price_som=101),
            actor=actor,
            requires_superadmin_approval=False,
            min_profit_percent=Decimal("5"),
            min_profit_som=5,
        )
        assert price.status == ManualPriceStatus.DRAFT
        assert price.active is False


class NeverPurchaseClient:
    purchase_calls = 0

    async def buy_stars(self, _request):
        self.purchase_calls += 1
        raise AssertionError("purchase client must not be called")


@pytest.mark.asyncio
async def test_purchase_flag_false_never_calls_provider(sessions) -> None:
    provider_id, user_id, _ = await seed_pricing(sessions)
    async with sessions.begin() as session:
        order = Order(
            public_order_number="GATE-1",
            user_id=user_id,
            provider_id=provider_id,
            service_type=ServiceType.STARS,
            target_username_original="@valid_user",
            target_username="valid_user",
            external_service_id="buy_stars",
            quantity=50,
            provider_cost_som=9_450,
            sale_price_som=10_500,
            reserved_amount_som=10_500,
            expected_profit_som=1_050,
            quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            internal_status=OrderStatus.READY_TO_SUBMIT,
            idempotency_key="ute:gate:myxvest:submit:v1",
        )
        session.add(order)
        await session.flush()
        order_id = order.id
    client = NeverPurchaseClient()
    await ProviderWorkflow(sessions, client, purchase_enabled=False).submit_order(order_id)
    assert client.purchase_calls == 0
    async with sessions() as session:
        assert (await session.get(Order, order_id)).internal_status == OrderStatus.READY_TO_SUBMIT


@pytest.mark.asyncio
async def test_concurrent_admin_updates_allocate_unique_versions(sessions) -> None:
    await seed_pricing(sessions)
    actor_one = PricingActor(20001, False, True)
    actor_two = PricingActor(20002, False, True)

    async def create(actor, sale):
        async with sessions.begin() as session:
            return await create_manual_price(
                session,
                data=stars_input(sale_price_som=sale),
                actor=actor,
                requires_superadmin_approval=False,
                min_profit_percent=Decimal("0"),
                min_profit_som=0,
            )

    await asyncio.gather(create(actor_one, 210), create(actor_two, 220))
    async with sessions() as session:
        prices = list(
            await session.scalars(select(ManualProviderPrice).order_by(ManualProviderPrice.version))
        )
        assert [price.version for price in prices] == [1, 2]
        assert sum(price.active for price in prices) == 1


@pytest.mark.asyncio
async def test_sensitive_source_note_is_redacted_from_audit(sessions) -> None:
    await seed_pricing(sessions)
    async with sessions.begin() as session:
        await save_price(session, stars_input(source_note="api_key=do-not-store"))
    async with sessions() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "MANUAL_PRICE_CREATED")
        )
        serialized = str(audit.sanitized_metadata)
        assert "do-not-store" not in serialized
        assert "REDACTED" in serialized
