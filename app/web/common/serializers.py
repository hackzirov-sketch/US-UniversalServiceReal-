from __future__ import annotations

from app.db.models import ManualProviderPrice, Order, Payment


def public_price(price: ManualProviderPrice) -> dict[str, object]:
    return {
        "id": price.id,
        "type": price.service_type.value,
        "name": price.display_name,
        "sale_price_som": price.sale_price_som,
        "min_quantity": price.min_quantity,
        "max_quantity": price.max_quantity,
        "premium_months": price.premium_months,
        "allow_comment": price.allow_comment,
        "active": price.active,
    }


def public_order(order: Order) -> dict[str, object]:
    return {
        "number": order.public_order_number,
        "type": order.service_type.value,
        "sale_price_som": order.sale_price_som,
        "status": order.internal_status.value,
        "created_at": order.created_at.isoformat(),
    }


def public_payment(payment: Payment) -> dict[str, object]:
    return {
        "id": payment.id,
        "amount_som": payment.amount_som,
        "approved_amount_som": payment.approved_amount_som,
        "status": payment.status,
        "review_note": payment.review_note,
        "created_at": payment.created_at.isoformat(),
    }
