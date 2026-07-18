from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.metrics import provider_balance_som
from app.db.enums import OrderStatus, ProviderState, ServiceType, TransactionType
from app.db.models import (
    Order,
    Provider,
    ProviderBalanceSnapshot,
    ProviderService,
    ProviderTransaction,
)
from app.domain.state_machine import transition
from app.integrations.providers.myxvest.client import MyxvestClient
from app.integrations.providers.myxvest.exceptions import (
    MyxvestError,
    MyxvestInsufficientFundsError,
    MyxvestInvalidResponseError,
    MyxvestTimeoutError,
)
from app.integrations.providers.myxvest.schemas import (
    GiftPurchaseRequest,
    PremiumMonths,
    PremiumPurchaseRequest,
    ProviderStatus,
    StarsPurchaseRequest,
)
from app.services.audit import write_audit
from app.services.balance import complete_order_funds, refund_order_funds
from app.services.preflight import SalesGateError, assert_purchase_gates
from app.services.pricing import price_is_unsafe, quote_is_expired


class ProviderWorkflow:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: MyxvestClient,
        *,
        purchase_enabled: bool = False,
        max_concurrency: int = 5,
        runtime_gate_required: bool = False,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._sessions = session_factory
        self._client = client
        self._purchase_enabled = purchase_enabled
        self._runtime_gate_required = runtime_gate_required
        self._provider_slots = asyncio.Semaphore(max_concurrency)

    async def sync_balance(self) -> int:
        now = datetime.now(UTC)
        try:
            balance = await self._client.get_balance()
        except MyxvestError as exc:
            async with self._sessions.begin() as session:
                provider = await self._provider(session, lock=True)
                provider.status = (
                    ProviderState.UNAVAILABLE if exc.retryable else ProviderState.DEGRADED
                )
                provider.last_error_code = exc.code
                provider.last_error_message = str(exc)[:255]
                provider.last_health_check_at = now
                session.add(
                    ProviderBalanceSnapshot(
                        provider_id=provider.id,
                        balance_som=None,
                        fetched_at=now,
                        success=False,
                        error_code=exc.code,
                    )
                )
            raise

        async with self._sessions.begin() as session:
            provider = await self._provider(session, lock=True)
            provider.status = ProviderState.AVAILABLE
            provider.last_balance_sync_at = now
            provider.last_health_check_at = now
            provider.last_successful_request_at = now
            provider.last_error_code = None
            provider.last_error_message = None
            session.add(
                ProviderBalanceSnapshot(
                    provider_id=provider.id,
                    balance_som=balance.balance_som,
                    fetched_at=now,
                    success=True,
                )
            )
            session.add(
                ProviderTransaction(
                    provider_id=provider.id,
                    transaction_type=TransactionType.BALANCE_SYNC,
                    amount_som=0,
                    balance_after_som=balance.balance_som,
                    status="CONFIRMED",
                    created_at=now,
                )
            )
        provider_balance_som.labels(provider="MYXVEST").set(balance.balance_som)
        return balance.balance_som

    async def sync_services(self) -> int:
        external_services = await self._client.get_services()
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            provider = await self._provider(session, lock=True)
            current = (
                await session.scalars(
                    select(ProviderService).where(ProviderService.provider_id == provider.id)
                )
            ).all()
            by_external_id = {service.external_service_id: service for service in current}
            seen: set[str] = set()
            for item in external_services:
                seen.add(item.external_service_id)
                existing = by_external_id.get(item.external_service_id)
                service_type = ServiceType(item.service_type.value)
                if existing is None:
                    existing = ProviderService(
                        provider_id=provider.id,
                        external_service_id=item.external_service_id,
                        service_type=service_type,
                        name=item.name,
                        provider_price_som=item.provider_price_som,
                        synced_at=now,
                    )
                    session.add(existing)
                    by_external_id[item.external_service_id] = existing
                existing.service_type = service_type
                existing.name = item.name
                existing.provider_price_som = item.provider_price_som
                existing.min_quantity = item.min_quantity
                existing.max_quantity = item.max_quantity
                existing.active = item.active
                existing.raw_metadata = item.raw_metadata
                existing.synced_at = now
            for service in current:
                if service.external_service_id not in seen:
                    service.active = False
            provider.last_successful_request_at = now
        return len(external_services)

    async def dispatch_pending(self, *, limit: int = 50) -> int:
        if not self._purchase_enabled:
            return 0
        ready_ids: list[str] = []
        async with self._sessions.begin() as session:
            if self._runtime_gate_required:
                try:
                    await assert_purchase_gates(session, environment_enabled=self._purchase_enabled)
                except SalesGateError:
                    return 0
            provider = await self._provider(session, lock=True)
            if not provider.enabled or provider.status != ProviderState.AVAILABLE:
                return 0
            latest_balance = await session.scalar(
                select(ProviderBalanceSnapshot)
                .where(
                    ProviderBalanceSnapshot.provider_id == provider.id,
                    ProviderBalanceSnapshot.success.is_(True),
                )
                .order_by(desc(ProviderBalanceSnapshot.fetched_at))
                .limit(1)
                .with_for_update()
            )
            if latest_balance is None or latest_balance.balance_som is None:
                return 0
            budget = latest_balance.balance_som
            orders = (
                await session.scalars(
                    select(Order)
                    .where(
                        Order.provider_id == provider.id,
                        Order.internal_status.in_(
                            [
                                OrderStatus.AWAITING_PROVIDER_FUNDING,
                                OrderStatus.INSUFFICIENT_PROVIDER_FUNDS,
                            ]
                        ),
                    )
                    .order_by(desc(Order.priority), Order.approved_at, Order.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            external_ids = {
                order.external_service_id for order in orders if order.external_service_id
            }
            services_by_external_id: dict[str, ProviderService] = {}
            if external_ids:
                services = await session.scalars(
                    select(ProviderService).where(
                        ProviderService.provider_id == provider.id,
                        ProviderService.external_service_id.in_(external_ids),
                    )
                )
                services_by_external_id = {
                    service.external_service_id: service for service in services
                }
            for order in orders:
                service = services_by_external_id.get(order.external_service_id or "")
                current_cost = (
                    service.provider_price_som
                    if service and service.provider_price_som is not None
                    else order.provider_cost_som
                )
                if quote_is_expired(order.quote_expires_at) or price_is_unsafe(
                    current_provider_cost_som=current_cost,
                    sale_price_som=order.sale_price_som,
                ):
                    order.internal_status = transition(
                        order.internal_status, OrderStatus.PRICE_CHANGED
                    )
                    continue
                if current_cost > budget:
                    if order.internal_status == OrderStatus.AWAITING_PROVIDER_FUNDING:
                        order.internal_status = transition(
                            order.internal_status, OrderStatus.INSUFFICIENT_PROVIDER_FUNDS
                        )
                    continue
                if order.internal_status == OrderStatus.INSUFFICIENT_PROVIDER_FUNDS:
                    order.internal_status = transition(
                        order.internal_status, OrderStatus.AWAITING_PROVIDER_FUNDING
                    )
                order.internal_status = transition(
                    order.internal_status, OrderStatus.READY_TO_SUBMIT
                )
                budget -= current_cost
                ready_ids.append(order.id)

        await asyncio.gather(*(self.submit_order(order_id) for order_id in ready_ids))
        return len(ready_ids)

    async def submit_order(self, order_id: str, *, controlled_test: bool = False) -> None:
        if not self._purchase_enabled:
            return
        async with self._sessions.begin() as session:
            if self._runtime_gate_required and not controlled_test:
                try:
                    await assert_purchase_gates(session, environment_enabled=self._purchase_enabled)
                except SalesGateError:
                    return
            claimed = await session.scalar(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.internal_status == OrderStatus.READY_TO_SUBMIT,
                )
                .values(
                    internal_status=OrderStatus.SUBMITTING,
                    provider_request_attempts=Order.provider_request_attempts + 1,
                )
                .returning(Order.id)
            )
            if claimed is None:
                return
            # The stable key and SUBMITTING state are committed before the external request.

        async with self._sessions() as session:
            order = await session.get(Order, order_id)
        if order is None:
            return
        async with self._provider_slots:
            try:
                result = await self._purchase(order)
            except MyxvestTimeoutError:
                await self._set_submission_failure(order_id, OrderStatus.PROVIDER_TIMEOUT)
                return
            except MyxvestInsufficientFundsError:
                await self._set_submission_failure(order_id, OrderStatus.NEEDS_REVIEW)
                return
            except (MyxvestInvalidResponseError, MyxvestError):
                await self._set_submission_failure(order_id, OrderStatus.NEEDS_REVIEW)
                return

        async with self._sessions.begin() as session:
            locked = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if locked is None or locked.internal_status != OrderStatus.SUBMITTING:
                return
            locked.provider_order_id = result.provider_order_id
            locked.provider_status = result.status.value
            locked.submitted_at = datetime.now(UTC)
            provider = await self._provider(session)
            session.add(
                ProviderTransaction(
                    provider_id=provider.id,
                    order_id=locked.id,
                    transaction_type=TransactionType.PURCHASE,
                    amount_som=result.charged_amount_som or locked.provider_cost_som,
                    external_reference=result.provider_order_id,
                    status=result.status.value,
                    created_at=datetime.now(UTC),
                )
            )
            if result.status == ProviderStatus.COMPLETED:
                await complete_order_funds(session, order_id=locked.id)
            elif result.status in {ProviderStatus.PENDING, ProviderStatus.PROCESSING}:
                locked.internal_status = transition(locked.internal_status, OrderStatus.PROCESSING)
            else:
                locked.internal_status = transition(
                    locked.internal_status, OrderStatus.NEEDS_REVIEW
                )

    async def poll_processing(self, *, limit: int = 100) -> int:
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    select(Order.id, Order.provider_order_id)
                    .where(Order.internal_status == OrderStatus.PROCESSING)
                    .order_by(Order.submitted_at)
                    .limit(limit)
                )
            )

        async def poll_one(
            order_id: str, provider_order_id: str | None
        ) -> tuple[str, ProviderStatus] | None:
            if not provider_order_id:
                return None
            async with self._provider_slots:
                try:
                    status = await self._client.get_order_status(provider_order_id)
                except MyxvestError:
                    return None
            return order_id, status.status

        results = await asyncio.gather(
            *(poll_one(order_id, provider_id) for order_id, provider_id in rows)
        )
        completed = [result for result in results if result is not None]
        for order_id, status in completed:
            await self._apply_status(order_id, status)
        return len(completed)

    async def reconcile(self, order_id: str) -> None:
        async with self._sessions() as session:
            order = await session.get(Order, order_id)
            if order is None or order.internal_status not in {
                OrderStatus.SUBMITTING,
                OrderStatus.PROVIDER_TIMEOUT,
                OrderStatus.NEEDS_REVIEW,
            }:
                return
            provider_order_id = order.provider_order_id
            idempotency_key = order.idempotency_key
        async with self._provider_slots:
            status = await self._client.reconcile_order(
                provider_order_id=provider_order_id,
                idempotency_key=idempotency_key,
            )
        async with self._sessions.begin() as session:
            locked = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if locked is None:
                return
            locked.provider_order_id = status.provider_order_id
            locked.provider_status = status.status.value
        await self._apply_status(order_id, status.status)

    async def reconcile_pending(self, *, limit: int = 100) -> int:
        async with self._sessions() as session:
            order_ids = list(
                await session.scalars(
                    select(Order.id)
                    .where(
                        Order.internal_status.in_(
                            [
                                OrderStatus.SUBMITTING,
                                OrderStatus.PROVIDER_TIMEOUT,
                                OrderStatus.NEEDS_REVIEW,
                            ]
                        )
                    )
                    .order_by(Order.updated_at)
                    .limit(limit)
                )
            )

        async def reconcile_one(order_id: str) -> bool:
            try:
                await self.reconcile(order_id)
            except MyxvestError:
                return False
            return True

        results = await asyncio.gather(*(reconcile_one(order_id) for order_id in order_ids))
        return sum(results)

    async def _apply_status(self, order_id: str, status: ProviderStatus) -> None:
        async with self._sessions.begin() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is None:
                return
            order.provider_status = status.value
            if status == ProviderStatus.COMPLETED:
                if order.internal_status == OrderStatus.SUBMITTING:
                    order.internal_status = transition(
                        order.internal_status, OrderStatus.PROCESSING
                    )
                await complete_order_funds(session, order_id=order.id)
            elif status == ProviderStatus.REFUNDED:
                if order.internal_status in {OrderStatus.SUBMITTING, OrderStatus.PROVIDER_TIMEOUT}:
                    order.internal_status = transition(
                        order.internal_status, OrderStatus.NEEDS_REVIEW
                    )
                await refund_order_funds(session, order_id=order.id)
                write_audit(
                    session,
                    actor_type="SYSTEM",
                    actor_id=None,
                    action="PROVIDER_REFUND",
                    entity_type="ORDER",
                    entity_id=order.id,
                )
            elif status == ProviderStatus.FAILED and order.internal_status in {
                OrderStatus.PROCESSING,
                OrderStatus.NEEDS_REVIEW,
            }:
                order.internal_status = transition(order.internal_status, OrderStatus.FAILED)

    async def _set_submission_failure(self, order_id: str, target: OrderStatus) -> None:
        async with self._sessions.begin() as session:
            order = await session.scalar(
                select(Order).where(Order.id == order_id).with_for_update()
            )
            if order is not None and order.internal_status == OrderStatus.SUBMITTING:
                order.internal_status = transition(order.internal_status, target)

    async def _purchase(self, order: Order):
        if order.service_type == ServiceType.STARS:
            return await self._client.buy_stars(
                StarsPurchaseRequest(
                    username=order.target_username,
                    quantity=order.quantity,
                    idempotency_key=order.idempotency_key,
                )
            )
        if order.service_type == ServiceType.PREMIUM:
            return await self._client.buy_premium(
                PremiumPurchaseRequest(
                    username=order.target_username,
                    months=PremiumMonths(order.premium_months),
                    idempotency_key=order.idempotency_key,
                )
            )
        return await self._client.buy_gift(
            GiftPurchaseRequest(
                username=order.target_username,
                gift_name=order.gift_id,
                idempotency_key=order.idempotency_key,
            )
        )

    @staticmethod
    async def _provider(session: AsyncSession, *, lock: bool = False) -> Provider:
        statement = select(Provider).where(Provider.code == "MYXVEST")
        if lock:
            statement = statement.with_for_update()
        provider = await session.scalar(statement)
        if provider is None:
            raise RuntimeError("MYXVEST provider is not bootstrapped")
        return provider
