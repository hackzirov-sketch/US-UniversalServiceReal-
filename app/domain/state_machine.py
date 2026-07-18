from app.db.enums import OrderStatus


class InvalidOrderTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset({OrderStatus.AWAITING_PAYMENT, OrderStatus.CANCELLED}),
    OrderStatus.AWAITING_PAYMENT: frozenset({OrderStatus.PAYMENT_REVIEW, OrderStatus.CANCELLED}),
    OrderStatus.PAYMENT_REVIEW: frozenset(
        {
            OrderStatus.AWAITING_PROVIDER_FUNDING,
            OrderStatus.PAYMENT_REJECTED,
            OrderStatus.PRICE_CHANGED,
        }
    ),
    OrderStatus.PRICE_CHANGED: frozenset(
        {OrderStatus.AWAITING_PROVIDER_FUNDING, OrderStatus.REFUND_PENDING, OrderStatus.CANCELLED}
    ),
    OrderStatus.AWAITING_PROVIDER_FUNDING: frozenset(
        {
            OrderStatus.READY_TO_SUBMIT,
            OrderStatus.PRICE_CHANGED,
            OrderStatus.INSUFFICIENT_PROVIDER_FUNDS,
            OrderStatus.REFUND_PENDING,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.INSUFFICIENT_PROVIDER_FUNDS: frozenset(
        {OrderStatus.AWAITING_PROVIDER_FUNDING, OrderStatus.READY_TO_SUBMIT}
    ),
    OrderStatus.READY_TO_SUBMIT: frozenset(
        {OrderStatus.SUBMITTING, OrderStatus.PRICE_CHANGED, OrderStatus.NEEDS_REVIEW}
    ),
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.PROCESSING,
            OrderStatus.COMPLETED,
            OrderStatus.PROVIDER_TIMEOUT,
            OrderStatus.NEEDS_REVIEW,
        }
    ),
    OrderStatus.PROVIDER_TIMEOUT: frozenset(
        {OrderStatus.PROCESSING, OrderStatus.COMPLETED, OrderStatus.NEEDS_REVIEW}
    ),
    OrderStatus.PROCESSING: frozenset(
        {
            OrderStatus.COMPLETED,
            OrderStatus.REFUND_PENDING,
            OrderStatus.NEEDS_REVIEW,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.NEEDS_REVIEW: frozenset(
        {
            OrderStatus.PROCESSING,
            OrderStatus.COMPLETED,
            OrderStatus.REFUND_PENDING,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.REFUND_PENDING: frozenset({OrderStatus.REFUNDED, OrderStatus.NEEDS_REVIEW}),
}


def transition(current: OrderStatus, target: OrderStatus) -> OrderStatus:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidOrderTransition(f"Transition {current.value} -> {target.value} is not allowed")
    return target
