from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.enums import AdminRole, ManualPriceStatus, PaymentStatus, ProviderState, ServiceType
from app.db.models import (
    AdminPermission,
    AuditLog,
    ManualProviderPrice,
    Payment,
    PaymentCard,
    PreflightResult,
    Provider,
    RuntimeSetting,
    User,
)
from app.services.admin import (
    AdminActionError,
    add_admin,
    remove_admin,
    replace_admin_permissions,
    set_admin_active,
)
from app.services.audit import BUSINESS_AUDIT_ACTIONS, list_business_audit, write_audit
from app.services.manual_pricing import calculate_sale_from_original, quick_adjust_price
from app.services.preflight import (
    SalesGateError,
    assert_purchase_gates,
    run_preflight,
    set_runtime_sales,
)

SUPERADMIN_ID = 90001


@pytest.mark.asyncio
async def test_superadmin_adds_existing_user_by_numeric_id_without_placeholder(sessions) -> None:
    async with sessions.begin() as session:
        user = User(telegram_id=10101, username="candidate")
        session.add(user)
    async with sessions.begin() as session:
        added = await add_admin(
            session,
            reference="10101",
            actor_telegram_id=SUPERADMIN_ID,
            superadmin_ids=frozenset({SUPERADMIN_ID}),
        )
        assert added.role == AdminRole.ADMIN.value
        assert added.admin_active
    async with sessions() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "ADMIN_ADDED"))
        assert audit is not None
        assert audit.new_values["telegram_id"] == 10101


@pytest.mark.asyncio
async def test_missing_numeric_user_is_rejected_and_not_created(sessions) -> None:
    async with sessions.begin() as session:
        with pytest.raises(AdminActionError, match="start"):
            await add_admin(
                session,
                reference="404404",
                actor_telegram_id=SUPERADMIN_ID,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
            )
    async with sessions() as session:
        assert await session.scalar(select(User).where(User.telegram_id == 404404)) is None


@pytest.mark.asyncio
async def test_username_lookup_is_casefolded_and_missing_username_rejected(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(telegram_id=20202, username="Known_User"))
    async with sessions.begin() as session:
        user = await add_admin(
            session,
            reference="@known_user",
            actor_telegram_id=SUPERADMIN_ID,
            superadmin_ids=frozenset({SUPERADMIN_ID}),
        )
        assert user.telegram_id == 20202
    async with sessions.begin() as session:
        with pytest.raises(AdminActionError, match="start"):
            await add_admin(
                session,
                reference="@missing_user",
                actor_telegram_id=SUPERADMIN_ID,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
            )


@pytest.mark.asyncio
async def test_ordinary_admin_cannot_add_or_remove_admin(sessions) -> None:
    async with sessions.begin() as session:
        session.add_all(
            [
                User(telegram_id=30301, is_admin=True),
                User(telegram_id=30302, is_admin=True),
                User(telegram_id=30303),
            ]
        )
    async with sessions.begin() as session:
        with pytest.raises(AdminActionError, match="superadmin"):
            await add_admin(
                session,
                reference="30303",
                actor_telegram_id=30301,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
            )
        with pytest.raises(AdminActionError, match="superadmin"):
            await remove_admin(
                session,
                reference="30302",
                actor_telegram_id=30301,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
            )


@pytest.mark.asyncio
async def test_remove_admin_revokes_permissions_session_and_review_assignment(sessions) -> None:
    async with sessions.begin() as session:
        user = User(telegram_id=40401, is_admin=True, role="ADMIN", admin_session_version=3)
        customer = User(telegram_id=40402)
        session.add_all([user, customer])
        await session.flush()
        session.add(
            AdminPermission(user_id=user.id, permission="VIEW_AUDIT", granted_by_telegram_id=1)
        )
        payment = Payment(
            user_id=customer.id,
            amount_som=10_000,
            status=PaymentStatus.REVIEW_PENDING.value,
            reviewed_by_admin_id=user.telegram_id,
        )
        session.add(payment)
    async with sessions.begin() as session:
        removed = await remove_admin(
            session,
            reference="40401",
            actor_telegram_id=SUPERADMIN_ID,
            superadmin_ids=frozenset({SUPERADMIN_ID}),
        )
        assert removed.role == "USER"
        assert removed.admin_session_version == 4
    async with sessions() as session:
        assert await session.scalar(select(AdminPermission)) is None
        payment = await session.scalar(select(Payment))
        assert payment.reviewed_by_admin_id is None


@pytest.mark.asyncio
async def test_bootstrap_superadmin_is_protected_from_removal(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(telegram_id=SUPERADMIN_ID, is_admin=True, role="SUPERADMIN"))
    async with sessions.begin() as session:
        with pytest.raises(AdminActionError, match="Bootstrap"):
            await remove_admin(
                session,
                reference=str(SUPERADMIN_ID),
                actor_telegram_id=SUPERADMIN_ID,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
            )


@pytest.mark.asyncio
async def test_permissions_replace_is_transactional_and_audited(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(telegram_id=50501, is_admin=True, admin_active=True))
    async with sessions.begin() as session:
        result = await replace_admin_permissions(
            session,
            target_telegram_id=50501,
            permissions={"VIEW_AUDIT", "MANAGE_PRICING"},
            actor_telegram_id=SUPERADMIN_ID,
            superadmin_ids=frozenset({SUPERADMIN_ID}),
        )
        assert result == ("MANAGE_PRICING", "VIEW_AUDIT")
    async with sessions() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "ADMIN_PERMISSIONS_CHANGED")
        )
        assert audit.new_values["permissions"] == ["MANAGE_PRICING", "VIEW_AUDIT"]


@pytest.mark.asyncio
async def test_superadmin_only_permission_cannot_be_granted_to_admin(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(telegram_id=60601, is_admin=True, admin_active=True))
    async with sessions.begin() as session:
        with pytest.raises(AdminActionError, match="Superadmin-only"):
            await replace_admin_permissions(
                session,
                target_telegram_id=60601,
                permissions={"ENABLE_REAL_SALES"},
                actor_telegram_id=SUPERADMIN_ID,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
            )


@pytest.mark.asyncio
async def test_disable_admin_invalidates_session(sessions) -> None:
    async with sessions.begin() as session:
        session.add(User(telegram_id=70701, is_admin=True, admin_active=True))
    async with sessions.begin() as session:
        user = await set_admin_active(
            session,
            target_telegram_id=70701,
            active=False,
            actor_telegram_id=SUPERADMIN_ID,
            superadmin_ids=frozenset({SUPERADMIN_ID}),
        )
        assert not user.admin_active
        assert user.admin_session_version == 2


def test_decimal_quick_adjustments_and_original_calculation_are_exact() -> None:
    assert quick_adjust_price(189, delta_som=1) == 190
    assert quick_adjust_price(189, percent=Decimal("5")) == 198
    assert quick_adjust_price(189, percent=Decimal("10")) == 208
    assert (
        calculate_sale_from_original(
            189,
            minimum_profit_per_unit_som=10,
            percentage_markup=Decimal("11.11"),
        )
        == 210
    )


@pytest.mark.asyncio
async def test_navigation_event_is_not_in_business_audit(sessions) -> None:
    async with sessions.begin() as session:
        write_audit(
            session,
            actor_type="ADMIN",
            actor_id="1",
            action="MENU_OPENED",
            entity_type="MENU",
            entity_id="pricing",
        )
        write_audit(
            session,
            actor_type="ADMIN",
            actor_id="1",
            action="PAYMENT_APPROVED",
            entity_type="PAYMENT",
            entity_id="p1",
        )
    async with sessions() as session:
        rows = await list_business_audit(session)
        assert [row.action for row in rows] == ["PAYMENT_APPROVED"]
        assert "MENU_OPENED" not in BUSINESS_AUDIT_ACTIONS


@pytest.mark.asyncio
async def test_audit_redacts_api_key_and_card_number(sessions) -> None:
    fake_card = "8600" + "123412341234"
    async with sessions.begin() as session:
        write_audit(
            session,
            actor_type="ADMIN",
            actor_id="1",
            action="PAYMENT_APPROVED",
            entity_type="PAYMENT",
            entity_id="p1",
            new_values={"api_key": "secret", "card_number": fake_card},
        )
    async with sessions() as session:
        row = await session.scalar(select(AuditLog))
        assert "secret" not in str(row.new_values)
        assert fake_card not in str(row.new_values)


@pytest.mark.asyncio
async def test_environment_and_runtime_purchase_gates_are_both_required(sessions) -> None:
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        session.add(RuntimeSetting(key="real_sales_enabled", bool_value=False))
        session.add(
            PreflightResult(
                requested_by_telegram_id=SUPERADMIN_ID,
                success=True,
                checks={},
                created_at=now,
                expires_at=now + timedelta(minutes=15),
            )
        )
    async with sessions() as session:
        with pytest.raises(SalesGateError, match="Environment"):
            await assert_purchase_gates(session, environment_enabled=False)
        with pytest.raises(SalesGateError, match="runtime"):
            await assert_purchase_gates(session, environment_enabled=True)
    async with sessions.begin() as session:
        await set_runtime_sales(
            session,
            enabled=True,
            actor_telegram_id=SUPERADMIN_ID,
            superadmin_ids=frozenset({SUPERADMIN_ID}),
            environment_enabled=True,
        )
    async with sessions() as session:
        await assert_purchase_gates(session, environment_enabled=True)


@pytest.mark.asyncio
async def test_ordinary_admin_cannot_enable_real_sales(sessions) -> None:
    async with sessions.begin() as session:
        session.add(RuntimeSetting(key="real_sales_enabled", bool_value=False))
    async with sessions.begin() as session:
        with pytest.raises(SalesGateError, match="superadmin"):
            await set_runtime_sales(
                session,
                enabled=True,
                actor_telegram_id=123,
                superadmin_ids=frozenset({SUPERADMIN_ID}),
                environment_enabled=True,
            )


@pytest.mark.asyncio
async def test_preflight_checks_complete_production_dependencies(sessions) -> None:
    now = datetime.now(UTC)
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite://",
        superadmin_ids=str(SUPERADMIN_ID),
        payment_review_chat_id=-100123,
        direct_sales_enabled=True,
        expected_alembic_head="head-for-test",
        backup_verified_at=now,
        secrets_rotated_after_compromise=True,
    )
    async with sessions.begin() as session:
        admin = User(telegram_id=SUPERADMIN_ID, is_admin=True, role="SUPERADMIN")
        provider = Provider(
            code="DIRECT",
            name="Direct fulfillment",
            enabled=True,
            status=ProviderState.AVAILABLE,
        )
        session.add_all([admin, provider])
        await session.flush()
        session.add(
            PaymentCard(
                card_number_encrypted="encrypted",
                card_number_last4="1234",
                card_holder_name="TEST USER",
                updated_by_admin_id=SUPERADMIN_ID,
                active=True,
            )
        )
        keys = [
            ("DIRECT:STARS", ServiceType.STARS, 1),
            ("DIRECT:PREMIUM:3", ServiceType.PREMIUM, 2),
            ("DIRECT:PREMIUM:6", ServiceType.PREMIUM, 3),
            ("DIRECT:PREMIUM:12", ServiceType.PREMIUM, 4),
            ("DIRECT:GIFT:test", ServiceType.GIFT, 5),
        ]
        session.add_all(
            ManualProviderPrice(
                provider_id=provider.id,
                service_type=kind,
                service_key=key,
                display_name=key,
                provider_cost_som=100,
                sale_price_som=110,
                unit_type="FIXED",
                active=True,
                status=ManualPriceStatus.ACTIVE,
                version=version,
                valid_from=now - timedelta(minutes=1),
                created_by_admin_id=SUPERADMIN_ID,
            )
            for key, kind, version in keys
        )
    async with sessions.begin() as session:
        report = await run_preflight(
            session,
            settings=settings,
            actor_telegram_id=SUPERADMIN_ID,
            infrastructure={
                "migration_head": True,
                "redis": True,
                "telegram": True,
                "topup_approval": True,
                "ledger": True,
            },
            now=now,
        )
        assert report.success, report.failures
