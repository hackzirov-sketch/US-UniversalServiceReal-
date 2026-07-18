from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sanitize
from app.db.models import AuditLog

BUSINESS_AUDIT_ACTIONS = frozenset(
    {
        "ADMIN_ADDED",
        "ADMIN_REMOVED",
        "ADMIN_DISABLED",
        "ADMIN_ENABLED",
        "ADMIN_PERMISSIONS_CHANGED",
        "PRICE_CREATED",
        "PRICE_UPDATED",
        "PRICE_ACTIVATED",
        "PRICE_DEACTIVATED",
        "MANUAL_PRICE_APPROVED",
        "MANUAL_PRICE_DEACTIVATED",
        "PAYMENT_APPROVED",
        "PAYMENT_REJECTED",
        "PAYMENT_INFO_REQUESTED",
        "PAYMENT_AMOUNT_CHANGED",
        "ORDER_CREATED",
        "ORDER_SUBMITTED",
        "ORDER_COMPLETED",
        "ORDER_FAILED",
        "ORDER_REFUNDED",
        "PROVIDER_BALANCE_SYNCED",
        "PROVIDER_FUNDING_CONFIRMED",
        "REAL_SALES_ENABLED",
        "REAL_SALES_DISABLED",
        "GIFT_CREATED",
        "GIFT_UPDATED",
        "BONUS_GRANTED",
        "BONUS_REVOKED",
        "FARM_REWARD_GRANTED",
        "FARM_REWARD_REVERSED",
        "RANKING_REWARD_GRANTED",
        "RECONCILIATION_COMPLETED",
        "MANUAL_REFUND_COMPLETED",
    }
)

AUDIT_CATEGORIES = {
    "admins": {action for action in BUSINESS_AUDIT_ACTIONS if action.startswith("ADMIN_")},
    "pricing": {
        action
        for action in BUSINESS_AUDIT_ACTIONS
        if action.startswith(("PRICE_", "MANUAL_PRICE_", "GIFT_"))
    },
    "payments": {action for action in BUSINESS_AUDIT_ACTIONS if action.startswith("PAYMENT_")},
    "orders": {
        action
        for action in BUSINESS_AUDIT_ACTIONS
        if action.startswith("ORDER_") or action == "MANUAL_REFUND_COMPLETED"
    },
    "provider": {
        action
        for action in BUSINESS_AUDIT_ACTIONS
        if action.startswith(("PROVIDER_", "RECONCILIATION_", "REAL_SALES_"))
    },
    "rewards": {
        action
        for action in BUSINESS_AUDIT_ACTIONS
        if action.startswith(("BONUS_", "FARM_", "RANKING_"))
    },
}


def write_audit(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict | None = None,
    actor_username: str | None = None,
    actor_role: str | None = None,
    summary: str | None = None,
    old_values: dict | None = None,
    new_values: dict | None = None,
    reason: str | None = None,
    correlation_id: str | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            actor_username_snapshot=actor_username,
            actor_role=actor_role or actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            human_summary=sanitize(summary) if summary else None,
            old_values=sanitize(old_values) if old_values is not None else None,
            new_values=sanitize(new_values) if new_values is not None else None,
            reason=sanitize(reason) if reason else None,
            correlation_id=correlation_id,
            request_id=request_id,
            sanitized_metadata=sanitize(metadata or {}),
        )
    )


async def list_business_audit(
    session: AsyncSession,
    *,
    category: str | None = None,
    days: int | None = None,
    query: str | None = None,
    limit: int = 50,
) -> list[AuditLog]:
    actions = AUDIT_CATEGORIES.get(category, BUSINESS_AUDIT_ACTIONS)
    statement = select(AuditLog).where(AuditLog.action.in_(actions))
    if days is not None:
        statement = statement.where(AuditLog.created_at >= datetime.now(UTC) - timedelta(days=days))
    if query:
        needle = f"%{query.strip()}%"
        statement = statement.where(
            AuditLog.entity_id.ilike(needle)
            | AuditLog.actor_id.ilike(needle)
            | AuditLog.human_summary.ilike(needle)
        )
    return list(await session.scalars(statement.order_by(AuditLog.created_at.desc()).limit(limit)))


def audit_text(row: AuditLog) -> str:
    actor = (
        f"@{row.actor_username_snapshot}"
        if row.actor_username_snapshot
        else row.actor_id or "SYSTEM"
    )
    summary = row.human_summary or row.action.replace("_", " ").title()
    return (
        f"{summary}\n"
        f"Bajargan: {actor} ({row.actor_role or row.actor_type})\n"
        f"Obyekt: {row.entity_type} / {row.entity_id}\n"
        f"Sana: {row.created_at.astimezone(UTC).strftime('%d.%m.%Y %H:%M UTC')}"
    )
