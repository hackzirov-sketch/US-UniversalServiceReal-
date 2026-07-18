from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import duplicate_prevented_total, order_completion_total, refund_total
from app.db.enums import LedgerType, OrderStatus, PaymentStatus
from app.db.models import LedgerEntry, Order, Payment, User
from app.domain.state_machine import transition


class BalanceError(ValueError):
    pass


async def _reference_exists(session: AsyncSession, reference: str) -> bool:
    return (
        await session.scalar(select(LedgerEntry.id).where(LedgerEntry.reference == reference))
        is not None
    )


async def credit_approved_payment(
    session: AsyncSession, *, payment_id: str, reference: str
) -> LedgerEntry | None:
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        raise BalanceError("Payment not found")
    if payment.status == PaymentStatus.APPROVED.value:
        existing = await session.scalar(
            select(LedgerEntry).where(LedgerEntry.payment_id == payment.id)
        )
        if existing is not None:
            duplicate_prevented_total.labels(operation="payment_credit").inc()
            return None
        raise BalanceError("Approved payment has no matching ledger entry")
    if await _reference_exists(session, reference):
        duplicate_prevented_total.labels(operation="payment_credit").inc()
        return None
    user = await session.scalar(select(User).where(User.id == payment.user_id).with_for_update())
    if user is None:
        raise BalanceError("User not found")
    approved_amount = payment.approved_amount_som or payment.amount_som
    before = user.available_balance_som
    user.available_balance_som += approved_amount
    user.version += 1
    payment.approved_amount_som = approved_amount
    payment.status = PaymentStatus.APPROVED.value
    payment.approved_at = datetime.now(UTC)
    entry = LedgerEntry(
        user_id=user.id,
        payment_id=payment.id,
        type=LedgerType.PAYMENT_CREDIT,
        amount_som=approved_amount,
        balance_before_som=before,
        balance_after_som=user.available_balance_som,
        reference=reference,
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    return entry


async def reserve_order_funds(session: AsyncSession, *, order_id: str) -> LedgerEntry | None:
    reference = f"order:{order_id}:reserve:v1"
    if await _reference_exists(session, reference):
        duplicate_prevented_total.labels(operation="reserve").inc()
        return None
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise BalanceError("Order not found")
    if order.reserved_amount_som:
        raise BalanceError("Order is already reserved without a ledger entry")
    user = await session.scalar(select(User).where(User.id == order.user_id).with_for_update())
    if user is None:
        raise BalanceError("User not found")
    if user.available_balance_som < order.sale_price_som:
        raise BalanceError("Insufficient user balance")
    before = user.available_balance_som
    user.available_balance_som -= order.sale_price_som
    user.reserved_balance_som += order.sale_price_som
    user.version += 1
    order.reserved_amount_som = order.sale_price_som
    order.internal_status = transition(order.internal_status, OrderStatus.AWAITING_PROVIDER_FUNDING)
    order.approved_at = order.approved_at or datetime.now(UTC)
    entry = LedgerEntry(
        user_id=user.id,
        order_id=order.id,
        type=LedgerType.RESERVE,
        amount_som=-order.sale_price_som,
        balance_before_som=before,
        balance_after_som=user.available_balance_som,
        reference=reference,
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    return entry


async def complete_order_funds(session: AsyncSession, *, order_id: str) -> LedgerEntry | None:
    reference = f"order:{order_id}:complete:v1"
    if await _reference_exists(session, reference):
        duplicate_prevented_total.labels(operation="complete").inc()
        return None
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise BalanceError("Order not found")
    user = await session.scalar(select(User).where(User.id == order.user_id).with_for_update())
    if user is None or order.reserved_amount_som <= 0:
        raise BalanceError("Order has no reserved funds")
    if user.reserved_balance_som < order.reserved_amount_som:
        raise BalanceError("Reserved balance invariant violated")
    reserved = order.reserved_amount_som
    before = user.reserved_balance_som
    user.reserved_balance_som -= reserved
    user.version += 1
    order.reserved_amount_som = 0
    order.internal_status = transition(order.internal_status, OrderStatus.COMPLETED)
    order.completed_at = datetime.now(UTC)
    order.actual_profit_som = order.sale_price_som - order.provider_cost_som
    entry = LedgerEntry(
        user_id=user.id,
        order_id=order.id,
        type=LedgerType.COMPLETE,
        amount_som=-reserved,
        balance_before_som=before,
        balance_after_som=user.reserved_balance_som,
        reference=reference,
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    order_completion_total.labels(provider="MYXVEST").inc()
    return entry


async def refund_order_funds(session: AsyncSession, *, order_id: str) -> LedgerEntry | None:
    reference = f"order:{order_id}:refund:v1"
    if await _reference_exists(session, reference):
        duplicate_prevented_total.labels(operation="refund").inc()
        return None
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise BalanceError("Order not found")
    user = await session.scalar(select(User).where(User.id == order.user_id).with_for_update())
    if user is None or order.reserved_amount_som <= 0:
        raise BalanceError("Order has no refundable reserved funds")
    if order.internal_status != OrderStatus.REFUND_PENDING:
        order.internal_status = transition(order.internal_status, OrderStatus.REFUND_PENDING)
    refundable = order.reserved_amount_som
    if user.reserved_balance_som < refundable:
        raise BalanceError("Reserved balance invariant violated")
    before = user.available_balance_som
    user.reserved_balance_som -= refundable
    user.available_balance_som += refundable
    user.version += 1
    order.reserved_amount_som = 0
    order.internal_status = transition(order.internal_status, OrderStatus.REFUNDED)
    order.refunded_at = datetime.now(UTC)
    entry = LedgerEntry(
        user_id=user.id,
        order_id=order.id,
        type=LedgerType.REFUND,
        amount_som=refundable,
        balance_before_som=before,
        balance_after_som=user.available_balance_som,
        reference=reference,
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    refund_total.labels(provider="MYXVEST").inc()
    return entry
