import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.enums import OrderStatus, ProviderState, ServiceType
from app.db.models import (
    LedgerEntry,
    Order,
    Provider,
    ProviderBalanceSnapshot,
    ProviderService,
    User,
)
from app.integrations.providers.myxvest.exceptions import (
    MyxvestAuthenticationError,
    MyxvestInsufficientFundsError,
    MyxvestInvalidResponseError,
    MyxvestTimeoutError,
)
from app.integrations.providers.myxvest.schemas import (
    ProviderBalance,
    ProviderOrderResult,
    ProviderOrderStatus,
    ProviderServiceType,
    ProviderStatus,
)
from app.integrations.providers.myxvest.schemas import (
    ProviderService as ProviderServiceSchema,
)
from app.services.provider import ProviderWorkflow


class FakeClient:
    def __init__(self, *, balance: int = 10_000) -> None:
        self.balance = balance
        self.purchase_calls = 0
        self.keys: list[str] = []
        self.services = []
        self.purchase_error = None
        self.purchase_status = ProviderStatus.PROCESSING
        self.status_result = ProviderStatus.PROCESSING
        self.status_error = None
        self.reconcile_result = ProviderStatus.PROCESSING

    async def get_balance(self) -> ProviderBalance:
        return ProviderBalance(balance_som=self.balance)

    async def get_services(self):
        return self.services

    async def buy_stars(self, request) -> ProviderOrderResult:
        self.purchase_calls += 1
        self.keys.append(request.idempotency_key)
        await asyncio.sleep(0)
        if self.purchase_error:
            raise self.purchase_error
        return ProviderOrderResult(
            provider_order_id="provider-1",
            status=self.purchase_status,
            charged_amount_som=700,
        )

    async def buy_premium(self, request):
        return await self.buy_stars(request)

    async def buy_gift(self, request):
        return await self.buy_stars(request)

    async def get_order_status(self, provider_order_id):
        if self.status_error:
            raise self.status_error
        return ProviderOrderStatus(provider_order_id=provider_order_id, status=self.status_result)

    async def reconcile_order(self, *, provider_order_id, idempotency_key):
        return ProviderOrderStatus(
            provider_order_id=provider_order_id or "reconciled-1",
            status=self.reconcile_result,
        )


async def seed_order(
    sessions,
    *,
    status: OrderStatus,
    provider_cost: int = 700,
    sale_price: int = 1_000,
    expired: bool = False,
    service_type: ServiceType = ServiceType.STARS,
    provider_order_id: str | None = None,
) -> str:
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        user = User(
            telegram_id=12345,
            available_balance_som=0,
            reserved_balance_som=sale_price,
        )
        provider = Provider(
            code="MYXVEST", name="Myxvest", enabled=True, status=ProviderState.AVAILABLE
        )
        session.add_all([user, provider])
        await session.flush()
        session.add(
            ProviderService(
                provider_id=provider.id,
                external_service_id="stars",
                service_type=ServiceType.STARS,
                name="Stars",
                provider_price_som=provider_cost,
                min_quantity=50,
                max_quantity=10000,
                active=True,
                raw_metadata={},
                synced_at=now,
            )
        )
        order = Order(
            public_order_number="U000001",
            user_id=user.id,
            provider_id=provider.id,
            service_type=service_type,
            target_username_original="@valid_user",
            target_username="valid_user",
            external_service_id="stars",
            quantity=50,
            premium_months=3 if service_type == ServiceType.PREMIUM else None,
            gift_id="Rose" if service_type == ServiceType.GIFT else None,
            provider_cost_som=provider_cost,
            sale_price_som=sale_price,
            reserved_amount_som=sale_price,
            expected_profit_som=sale_price - provider_cost,
            quote_expires_at=now + timedelta(minutes=-1 if expired else 5),
            internal_status=status,
            idempotency_key="ute:order-1:myxvest:submit:v1",
            approved_at=now,
            provider_order_id=provider_order_id,
        )
        session.add(order)
        await session.flush()
        return order.id


@pytest.mark.asyncio
async def test_two_workers_submit_order_only_once(sessions) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.READY_TO_SUBMIT)
    client = FakeClient()
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    await asyncio.gather(workflow.submit_order(order_id), workflow.submit_order(order_id))
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == OrderStatus.PROCESSING
        assert order.provider_request_attempts == 1
    assert client.purchase_calls == 1
    assert client.keys == ["ute:order-1:myxvest:submit:v1"]


@pytest.mark.asyncio
async def test_insufficient_provider_balance_keeps_order_queued(sessions) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING)
    client = FakeClient(balance=100)
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    await workflow.sync_balance()
    count = await workflow.dispatch_pending()
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == OrderStatus.INSUFFICIENT_PROVIDER_FUNDS
    assert count == 0
    assert client.purchase_calls == 0


@pytest.mark.asyncio
async def test_balance_top_up_automatically_resumes_queue(sessions) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING)
    client = FakeClient(balance=100)
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    await workflow.sync_balance()
    assert await workflow.dispatch_pending() == 0
    client.balance = 10_000
    await workflow.sync_balance()
    assert await workflow.dispatch_pending() == 1
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == OrderStatus.PROCESSING


@pytest.mark.asyncio
async def test_expired_quote_moves_to_price_changed(sessions) -> None:
    order_id = await seed_order(
        sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING, expired=True
    )
    workflow = ProviderWorkflow(sessions, FakeClient(), purchase_enabled=True)
    await workflow.sync_balance()
    assert await workflow.dispatch_pending() == 0
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == OrderStatus.PRICE_CHANGED


@pytest.mark.asyncio
async def test_provider_price_above_sale_moves_to_review(sessions) -> None:
    order_id = await seed_order(
        sessions,
        status=OrderStatus.AWAITING_PROVIDER_FUNDING,
        provider_cost=1_100,
        sale_price=1_200,
    )
    async with sessions.begin() as session:
        service = await session.scalar(select(ProviderService))
        service.provider_price_som = 1_201
    workflow = ProviderWorkflow(sessions, FakeClient(), purchase_enabled=True)
    await workflow.sync_balance()
    assert await workflow.dispatch_pending() == 0
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == OrderStatus.PRICE_CHANGED


@pytest.mark.asyncio
async def test_restart_recovers_persisted_pending_order(sessions) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING)
    client = FakeClient()
    first_process = ProviderWorkflow(sessions, client, purchase_enabled=True)
    await first_process.sync_balance()
    del first_process
    restarted_process = ProviderWorkflow(sessions, client, purchase_enabled=True)
    assert await restarted_process.dispatch_pending() == 1
    async with sessions() as session:
        order = await session.get(Order, order_id)
        snapshots = await session.scalar(select(func.count()).select_from(ProviderBalanceSnapshot))
        assert order.internal_status == OrderStatus.PROCESSING
        assert snapshots == 1
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 0


@pytest.mark.asyncio
async def test_balance_sync_error_is_persisted_without_secret(sessions) -> None:
    await seed_order(sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING)
    client = FakeClient()

    async def fail_balance():
        raise MyxvestAuthenticationError("Provider authentication failed")

    client.get_balance = fail_balance
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    with pytest.raises(MyxvestAuthenticationError):
        await workflow.sync_balance()
    async with sessions() as session:
        provider = await session.scalar(select(Provider))
        snapshot = await session.scalar(select(ProviderBalanceSnapshot))
        assert provider.status == ProviderState.DEGRADED
        assert snapshot.success is False
        assert snapshot.error_code == "authentication_error"


@pytest.mark.asyncio
async def test_service_sync_creates_updates_and_deactivates(sessions) -> None:
    await seed_order(sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING)
    client = FakeClient()
    client.services = [
        ProviderServiceSchema(
            external_service_id="buy_stars",
            service_type=ProviderServiceType.STARS,
            name="Stars",
            provider_price_som=189,
            min_quantity=50,
            max_quantity=10_000,
            required_params=["username", "amount (50-10000)"],
        )
    ]
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    assert await workflow.sync_services() == 1
    client.services = [
        ProviderServiceSchema(
            external_service_id="buy_premium",
            service_type=ProviderServiceType.PREMIUM,
            name="Premium",
            provider_price_som=None,
            required_params=["username", "months (3|6|12)"],
        )
    ]
    assert await workflow.sync_services() == 1
    async with sessions() as session:
        stars = await session.scalar(
            select(ProviderService).where(ProviderService.external_service_id == "buy_stars")
        )
        premium = await session.scalar(
            select(ProviderService).where(ProviderService.external_service_id == "buy_premium")
        )
        assert stars.active is False
        assert premium.provider_price_som is None


@pytest.mark.asyncio
async def test_dispatch_requires_enabled_provider_and_balance_snapshot(sessions) -> None:
    await seed_order(sessions, status=OrderStatus.AWAITING_PROVIDER_FUNDING)
    workflow = ProviderWorkflow(sessions, FakeClient(), purchase_enabled=True)
    assert await workflow.dispatch_pending() == 0
    async with sessions.begin() as session:
        provider = await session.scalar(select(Provider))
        provider.enabled = False
    assert await workflow.dispatch_pending() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MyxvestTimeoutError(), OrderStatus.PROVIDER_TIMEOUT),
        (MyxvestInsufficientFundsError(), OrderStatus.NEEDS_REVIEW),
        (MyxvestInvalidResponseError(), OrderStatus.NEEDS_REVIEW),
    ],
)
async def test_submit_errors_move_to_recoverable_states(sessions, error, expected) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.READY_TO_SUBMIT)
    client = FakeClient()
    client.purchase_error = error
    await ProviderWorkflow(sessions, client, purchase_enabled=True).submit_order(order_id)
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == expected
        assert order.provider_request_attempts == 1


@pytest.mark.asyncio
async def test_immediate_completed_purchase_finalizes_reserved_ledger(sessions) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.READY_TO_SUBMIT)
    client = FakeClient()
    client.purchase_status = ProviderStatus.COMPLETED
    await ProviderWorkflow(sessions, client, purchase_enabled=True).submit_order(order_id)
    async with sessions() as session:
        order = await session.get(Order, order_id)
        user = await session.get(User, order.user_id)
        assert order.internal_status == OrderStatus.COMPLETED
        assert user.reserved_balance_som == 0
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1


@pytest.mark.asyncio
async def test_unknown_purchase_result_moves_to_review(sessions) -> None:
    order_id = await seed_order(sessions, status=OrderStatus.READY_TO_SUBMIT)
    client = FakeClient()
    client.purchase_status = ProviderStatus.UNKNOWN
    await ProviderWorkflow(sessions, client, purchase_enabled=True).submit_order(order_id)
    async with sessions() as session:
        assert (await session.get(Order, order_id)).internal_status == OrderStatus.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_status_poller_completes_and_ignores_temporary_error(sessions) -> None:
    order_id = await seed_order(
        sessions,
        status=OrderStatus.PROCESSING,
        provider_order_id="provider-processing",
    )
    client = FakeClient()
    client.status_error = MyxvestTimeoutError()
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    assert await workflow.poll_processing() == 0
    client.status_error = None
    client.status_result = ProviderStatus.COMPLETED
    assert await workflow.poll_processing() == 1
    async with sessions() as session:
        assert (await session.get(Order, order_id)).internal_status == OrderStatus.COMPLETED


@pytest.mark.asyncio
async def test_status_poller_uses_bounded_concurrency(sessions) -> None:
    order_id = await seed_order(
        sessions,
        status=OrderStatus.PROCESSING,
        provider_order_id="provider-processing-0",
    )
    async with sessions.begin() as session:
        base = await session.get(Order, order_id)
        for index in range(2, 6):
            session.add(
                Order(
                    public_order_number=f"U{index:06d}",
                    user_id=base.user_id,
                    provider_id=base.provider_id,
                    service_type=base.service_type,
                    target_username_original=base.target_username_original,
                    target_username=base.target_username,
                    external_service_id=base.external_service_id,
                    quantity=base.quantity,
                    provider_cost_som=base.provider_cost_som,
                    sale_price_som=base.sale_price_som,
                    reserved_amount_som=base.reserved_amount_som,
                    expected_profit_som=base.expected_profit_som,
                    quote_expires_at=base.quote_expires_at,
                    internal_status=OrderStatus.PROCESSING,
                    idempotency_key=f"ute:order-{index}:myxvest:submit:v1",
                    provider_order_id=f"provider-processing-{index}",
                    submitted_at=datetime.now(UTC),
                )
            )

    client = FakeClient()
    active = 0
    peak = 0

    async def delayed_status(provider_order_id: str) -> ProviderOrderStatus:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ProviderOrderStatus(
            provider_order_id=provider_order_id,
            status=ProviderStatus.PROCESSING,
        )

    client.get_order_status = delayed_status
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True, max_concurrency=2)
    assert await workflow.poll_processing() == 5
    assert peak == 2


@pytest.mark.asyncio
async def test_provider_workflow_rejects_invalid_concurrency(sessions) -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        ProviderWorkflow(sessions, FakeClient(), max_concurrency=0)


@pytest.mark.asyncio
async def test_restart_reconciliation_uses_persisted_key_and_completes(sessions) -> None:
    order_id = await seed_order(
        sessions,
        status=OrderStatus.PROVIDER_TIMEOUT,
        provider_order_id=None,
    )
    client = FakeClient()
    client.reconcile_result = ProviderStatus.COMPLETED
    restarted_workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    await restarted_workflow.reconcile(order_id)
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.provider_order_id == "reconciled-1"
        assert order.internal_status == OrderStatus.COMPLETED


@pytest.mark.asyncio
async def test_reconciliation_refund_is_idempotent_and_audited(sessions) -> None:
    order_id = await seed_order(
        sessions,
        status=OrderStatus.PROVIDER_TIMEOUT,
        provider_order_id="provider-refund",
    )
    client = FakeClient()
    client.reconcile_result = ProviderStatus.REFUNDED
    workflow = ProviderWorkflow(sessions, client, purchase_enabled=True)
    await workflow.reconcile(order_id)
    await workflow.reconcile(order_id)
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order.internal_status == OrderStatus.REFUNDED
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", [ServiceType.PREMIUM, ServiceType.GIFT])
async def test_purchase_routing_for_premium_and_gift(sessions, service_type) -> None:
    order_id = await seed_order(
        sessions, status=OrderStatus.READY_TO_SUBMIT, service_type=service_type
    )
    client = FakeClient()
    await ProviderWorkflow(sessions, client, purchase_enabled=True).submit_order(order_id)
    assert client.purchase_calls == 1
