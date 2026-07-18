from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.enums import LedgerType, OrderStatus, ProviderState, ServiceType
from app.db.models import LedgerEntry, Order, Payment, Provider, User
from app.domain.state_machine import InvalidOrderTransition, transition
from app.integrations.providers.myxvest.exceptions import MyxvestInvalidResponseError
from app.integrations.providers.myxvest.mapper import map_service_type, require_dict
from app.services.balance import (
    BalanceError,
    complete_order_funds,
    credit_approved_payment,
    refund_order_funds,
    reserve_order_funds,
)
from app.services.pricing import (
    calculate_quote,
    price_is_unsafe,
    quote_is_expired,
)


def make_order(*, user_id: str, provider_id: str, status: OrderStatus) -> Order:
    now = datetime.now(UTC)
    return Order(
        public_order_number=f"U{now.timestamp():.6f}".replace(".", ""),
        user_id=user_id,
        provider_id=provider_id,
        service_type=ServiceType.STARS,
        target_username_original="@valid_user",
        target_username="valid_user",
        external_service_id="stars",
        quantity=50,
        provider_cost_som=700,
        sale_price_som=1_000,
        reserved_amount_som=0,
        expected_profit_som=300,
        quote_expires_at=now + timedelta(minutes=5),
        internal_status=status,
        idempotency_key=f"ute:{now.timestamp()}:myxvest:submit:v1",
    )


async def seed_user_provider(sessions, *, balance: int = 2_000):
    async with sessions.begin() as session:
        user = User(telegram_id=10001, available_balance_som=balance)
        provider = Provider(
            code="MYXVEST", name="Myxvest", enabled=True, status=ProviderState.AVAILABLE
        )
        session.add_all([user, provider])
        await session.flush()
        return user.id, provider.id


@pytest.mark.asyncio
async def test_user_balance_reserve_is_atomic_and_idempotent(sessions) -> None:
    user_id, provider_id = await seed_user_provider(sessions)
    async with sessions.begin() as session:
        order = make_order(
            user_id=user_id, provider_id=provider_id, status=OrderStatus.PAYMENT_REVIEW
        )
        session.add(order)
        await session.flush()
        order_id = order.id
    async with sessions.begin() as session:
        await reserve_order_funds(session, order_id=order_id)
    async with sessions.begin() as session:
        assert await reserve_order_funds(session, order_id=order_id) is None
    async with sessions() as session:
        user = await session.get(User, user_id)
        order = await session.get(Order, order_id)
        count = await session.scalar(select(func.count()).select_from(LedgerEntry))
        assert (user.available_balance_som, user.reserved_balance_som) == (1_000, 1_000)
        assert order.internal_status == OrderStatus.AWAITING_PROVIDER_FUNDING
        assert count == 1


@pytest.mark.asyncio
async def test_completed_order_writes_one_ledger_entry(sessions) -> None:
    user_id, provider_id = await seed_user_provider(sessions, balance=1_000)
    async with sessions.begin() as session:
        user = await session.get(User, user_id)
        user.available_balance_som = 0
        user.reserved_balance_som = 1_000
        order = make_order(user_id=user_id, provider_id=provider_id, status=OrderStatus.PROCESSING)
        order.reserved_amount_som = 1_000
        session.add(order)
        await session.flush()
        order_id = order.id
    async with sessions.begin() as session:
        await complete_order_funds(session, order_id=order_id)
    async with sessions.begin() as session:
        assert await complete_order_funds(session, order_id=order_id) is None
    async with sessions() as session:
        entry = await session.scalar(
            select(LedgerEntry).where(LedgerEntry.type == LedgerType.COMPLETE)
        )
        user = await session.get(User, user_id)
        assert entry is not None
        assert user.reserved_balance_som == 0


@pytest.mark.asyncio
async def test_refund_ledger_and_double_refund_blocked(sessions) -> None:
    user_id, provider_id = await seed_user_provider(sessions, balance=1_000)
    async with sessions.begin() as session:
        user = await session.get(User, user_id)
        user.available_balance_som = 0
        user.reserved_balance_som = 1_000
        order = make_order(user_id=user_id, provider_id=provider_id, status=OrderStatus.PROCESSING)
        order.reserved_amount_som = 1_000
        session.add(order)
        await session.flush()
        order_id = order.id
    async with sessions.begin() as session:
        await refund_order_funds(session, order_id=order_id)
    async with sessions.begin() as session:
        assert await refund_order_funds(session, order_id=order_id) is None
    async with sessions() as session:
        user = await session.get(User, user_id)
        entries = list(
            await session.scalars(select(LedgerEntry).where(LedgerEntry.type == LedgerType.REFUND))
        )
        assert user.available_balance_som == 1_000
        assert user.reserved_balance_som == 0
        assert len(entries) == 1


def test_quote_expiry_and_integer_pricing() -> None:
    now = datetime.now(UTC)
    quote = calculate_quote(
        provider_cost_som=10_001,
        fixed_markup_som=100,
        percentage_markup_bps=250,
        minimum_profit_som=500,
        risk_buffer_som=200,
        ttl_seconds=300,
        now=now,
    )
    assert quote.markup_som == 500
    assert quote.sale_price_som == 10_701
    assert not quote_is_expired(quote.expires_at, now=now)
    assert quote_is_expired(quote.expires_at, now=now + timedelta(seconds=301))


def test_provider_price_increase_blocks_loss() -> None:
    assert price_is_unsafe(current_provider_cost_som=1_001, sale_price_som=1_000)
    assert not price_is_unsafe(current_provider_cost_som=999, sale_price_som=1_000)


@pytest.mark.asyncio
async def test_approved_payment_credit_is_atomic_and_idempotent(sessions) -> None:
    user_id, _ = await seed_user_provider(sessions, balance=100)
    async with sessions.begin() as session:
        payment = Payment(user_id=user_id, amount_som=900)
        session.add(payment)
        await session.flush()
        payment_id = payment.id
    async with sessions.begin() as session:
        entry = await credit_approved_payment(
            session, payment_id=payment_id, reference="payment:one:approve:v1"
        )
        assert entry is not None
    async with sessions.begin() as session:
        assert (
            await credit_approved_payment(
                session, payment_id=payment_id, reference="payment:one:approve:v1"
            )
            is None
        )
    async with sessions() as session:
        user = await session.get(User, user_id)
        payment = await session.get(Payment, payment_id)
        assert user.available_balance_som == 1_000
        assert payment.status == "APPROVED"


@pytest.mark.asyncio
async def test_missing_payment_and_insufficient_reserve_fail_safely(sessions) -> None:
    with pytest.raises(BalanceError, match="Payment not found"):
        async with sessions.begin() as session:
            await credit_approved_payment(
                session, payment_id="missing", reference="payment:missing:approve:v1"
            )
    user_id, provider_id = await seed_user_provider(sessions, balance=1)
    async with sessions.begin() as session:
        order = make_order(
            user_id=user_id, provider_id=provider_id, status=OrderStatus.PAYMENT_REVIEW
        )
        session.add(order)
        await session.flush()
        order_id = order.id
    with pytest.raises(BalanceError, match="Insufficient user balance"):
        async with sessions.begin() as session:
            await reserve_order_funds(session, order_id=order_id)


@pytest.mark.asyncio
async def test_balance_invariant_failures_are_explicit(sessions) -> None:
    for operation in (reserve_order_funds, complete_order_funds, refund_order_funds):
        with pytest.raises(BalanceError, match="Order not found"):
            async with sessions.begin() as session:
                await operation(session, order_id="missing")

    user_id, provider_id = await seed_user_provider(sessions, balance=0)
    async with sessions.begin() as session:
        empty = make_order(user_id=user_id, provider_id=provider_id, status=OrderStatus.PROCESSING)
        session.add(empty)
        await session.flush()
        empty_id = empty.id
    with pytest.raises(BalanceError, match="no reserved funds"):
        async with sessions.begin() as session:
            await complete_order_funds(session, order_id=empty_id)
    with pytest.raises(BalanceError, match="no refundable reserved funds"):
        async with sessions.begin() as session:
            await refund_order_funds(session, order_id=empty_id)

    async with sessions.begin() as session:
        broken = make_order(user_id=user_id, provider_id=provider_id, status=OrderStatus.PROCESSING)
        broken.reserved_amount_som = 1_000
        session.add(broken)
        await session.flush()
        broken_id = broken.id
    with pytest.raises(BalanceError, match="Reserved balance invariant"):
        async with sessions.begin() as session:
            await complete_order_funds(session, order_id=broken_id)
    with pytest.raises(BalanceError, match="Reserved balance invariant"):
        async with sessions.begin() as session:
            await refund_order_funds(session, order_id=broken_id)


def test_invalid_state_transition_is_rejected() -> None:
    with pytest.raises(InvalidOrderTransition):
        transition(OrderStatus.DRAFT, OrderStatus.COMPLETED)


def test_mapper_handles_live_actions_and_invalid_shapes() -> None:
    assert map_service_type("buy_stars").value == "STARS"
    assert map_service_type("buy_premium").value == "PREMIUM"
    assert map_service_type("buy_gift").value == "GIFT"
    with pytest.raises(MyxvestInvalidResponseError):
        map_service_type("donat_buy")
    with pytest.raises(MyxvestInvalidResponseError):
        require_dict([])
