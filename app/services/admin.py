import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AdminRole, PaymentStatus, ServiceType
from app.db.models import (
    AdminPermission,
    AuditLog,
    Order,
    Payment,
    Provider,
    ProviderService,
    User,
)
from app.services.audit import write_audit
from app.services.balance import refund_order_funds

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

ADMIN_PERMISSIONS = frozenset(
    {
        "REVIEW_PAYMENTS",
        "APPROVE_PAYMENTS",
        "REJECT_PAYMENTS",
        "EDIT_APPROVED_AMOUNT",
        "MANAGE_PRICING",
        "MANAGE_GIFTS",
        "MANAGE_FARM",
        "MANAGE_BONUSES",
        "MANAGE_RANKING",
        "VIEW_USERS",
        "VIEW_ORDERS",
        "VIEW_FINANCIAL_REPORTS",
        "VIEW_AUDIT",
        "MANAGE_PROVIDER",
        "TRIGGER_RECONCILIATION",
        "MANUAL_REFUND",
    }
)
SUPERADMIN_ONLY_PERMISSIONS = frozenset(
    {
        "MANAGE_ADMINS",
        "MANAGE_PAYMENT_CARD",
        "MANAGE_SUPERADMINS",
        "ENABLE_REAL_SALES",
        "DISABLE_REAL_SALES",
        "VIEW_FULL_FINANCIAL_REPORTS",
        "OVERRIDE_REVIEW_LOCK",
    }
)
ALL_ADMIN_PERMISSIONS = ADMIN_PERMISSIONS | SUPERADMIN_ONLY_PERMISSIONS


class AdminActionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminCard:
    user: User
    permissions: tuple[str, ...]
    active_review_count: int
    last_action: AuditLog | None


def parse_admin_reference(raw: str) -> tuple[str, object]:
    """Return ("id", int) or ("username", normalized str)."""
    text = (raw or "").strip()
    if text.startswith("@"):
        text = text[1:]
    if not text:
        raise AdminActionError("Raqamli ID yoki @username kiriting")
    if text.isascii() and text.isdecimal():
        value = int(text)
        if value <= 0 or value > 9_223_372_036_854_775_807:
            raise AdminActionError("Telegram ID musbat 64-bit integer bo‘lishi kerak")
        return ("id", value)
    if _USERNAME_RE.fullmatch(text):
        return ("username", text.casefold())
    raise AdminActionError("Noto‘g‘ri format. Raqamli ID yoki @username yuboring")


async def _resolve_existing_user(session: AsyncSession, reference: str) -> User | None:
    kind, value = parse_admin_reference(reference)
    if kind == "id":
        return await session.scalar(select(User).where(User.telegram_id == value).with_for_update())
    matches = list(
        await session.scalars(
            select(User)
            .where(func.lower(User.username) == str(value))
            .order_by(User.updated_at.desc())
            .with_for_update()
        )
    )
    if len(matches) > 1:
        raise AdminActionError(
            "Bu username bir nechta tarixiy profilga mos keldi. Numeric Telegram ID orqali aniqlang"
        )
    return matches[0] if matches else None


async def preview_admin_candidate(session: AsyncSession, *, reference: str) -> User:
    user = await _resolve_existing_user(session, reference)
    if user is None:
        raise AdminActionError(
            "Bu foydalanuvchi hali botdan foydalanmagan yoki username topilmadi. "
            "Foydalanuvchi botga /start bosishi kerak. Yoki Telegram ID orqali qo‘shing."
        )
    if user.is_admin:
        raise AdminActionError("Bu foydalanuvchi allaqachon admin")
    return user


async def add_admin(
    session: AsyncSession,
    *,
    reference: str,
    actor_telegram_id: int,
    superadmin_ids: frozenset[int],
) -> User:
    if actor_telegram_id not in superadmin_ids:
        raise AdminActionError("Faqat superadmin admin qo‘sha oladi")
    user = await preview_admin_candidate(session, reference=reference)
    user.is_admin = True
    user.role = AdminRole.ADMIN.value
    user.admin_active = True
    user.admin_added_by_telegram_id = actor_telegram_id
    user.admin_added_at = datetime.now(UTC)
    user.admin_disabled_at = None
    user.admin_session_version += 1
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="ADMIN_ADDED",
        entity_type="USER",
        entity_id=user.id,
        summary=f"Admin qo‘shildi: {user.telegram_id}",
        new_values={"telegram_id": user.telegram_id, "username": user.username, "role": "ADMIN"},
    )
    return user


async def remove_admin(
    session: AsyncSession,
    *,
    reference: str,
    actor_telegram_id: int,
    superadmin_ids: frozenset[int],
) -> User:
    if actor_telegram_id not in superadmin_ids:
        raise AdminActionError("Faqat superadmin adminni olib tashlay oladi")
    user = await _resolve_existing_user(session, reference)
    if user is None:
        raise AdminActionError("Foydalanuvchi topilmadi")
    if user.telegram_id in superadmin_ids:
        raise AdminActionError("Bootstrap superadmin database orqali oddiy admin qilinmaydi")
    if user.telegram_id == actor_telegram_id:
        raise AdminActionError("Admin o‘zini o‘zi olib tashlay olmaydi")
    if not user.is_admin:
        raise AdminActionError("Bu foydalanuvchi admin emas")
    user.is_admin = False
    user.role = AdminRole.USER.value
    user.admin_active = False
    user.admin_disabled_at = datetime.now(UTC)
    user.admin_session_version += 1
    await session.execute(delete(AdminPermission).where(AdminPermission.user_id == user.id))
    await _release_payment_reviews(session, user.telegram_id)
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="ADMIN_REMOVED",
        entity_type="USER",
        entity_id=user.id,
        summary=f"Adminlikdan olindi: {user.telegram_id}",
        old_values={"telegram_id": user.telegram_id, "username": user.username, "role": "ADMIN"},
        new_values={"role": "USER", "permissions": []},
    )
    return user


async def list_admins(session: AsyncSession) -> list[User]:
    return list(
        await session.scalars(
            select(User).where(User.is_admin.is_(True)).order_by(User.telegram_id)
        )
    )


async def list_admin_cards(session: AsyncSession) -> list[AdminCard]:
    cards: list[AdminCard] = []
    for user in await list_admins(session):
        permissions = tuple(
            sorted(
                await session.scalars(
                    select(AdminPermission.permission).where(AdminPermission.user_id == user.id)
                )
            )
        )
        review_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Payment)
                .where(
                    Payment.reviewed_by_admin_id == user.telegram_id,
                    Payment.status == PaymentStatus.REVIEW_PENDING.value,
                )
            )
            or 0
        )
        last_action = await session.scalar(
            select(AuditLog)
            .where(AuditLog.actor_id == str(user.telegram_id))
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        cards.append(AdminCard(user, permissions, review_count, last_action))
    return cards


async def set_admin_active(
    session: AsyncSession,
    *,
    target_telegram_id: int,
    active: bool,
    actor_telegram_id: int,
    superadmin_ids: frozenset[int],
) -> User:
    if actor_telegram_id not in superadmin_ids:
        raise AdminActionError("Faqat superadmin admin holatini o‘zgartira oladi")
    if target_telegram_id in superadmin_ids:
        raise AdminActionError(
            "Bootstrap superadmin holatini database orqali o‘zgartirib bo‘lmaydi"
        )
    if target_telegram_id == actor_telegram_id:
        raise AdminActionError("O‘z holatingizni o‘zgartira olmaysiz")
    user = await session.scalar(
        select(User).where(User.telegram_id == target_telegram_id).with_for_update()
    )
    if user is None or not user.is_admin:
        raise AdminActionError("Admin topilmadi")
    if user.admin_active == active:
        return user
    user.admin_active = active
    user.admin_disabled_at = None if active else datetime.now(UTC)
    user.admin_session_version += 1
    if not active:
        await _release_payment_reviews(session, user.telegram_id)
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="ADMIN_ENABLED" if active else "ADMIN_DISABLED",
        entity_type="USER",
        entity_id=user.id,
        summary=(
            f"Admin {'faollashtirildi' if active else 'faolsizlantirildi'}: {target_telegram_id}"
        ),
        new_values={"admin_active": active},
    )
    return user


async def replace_admin_permissions(
    session: AsyncSession,
    *,
    target_telegram_id: int,
    permissions: set[str],
    actor_telegram_id: int,
    superadmin_ids: frozenset[int],
) -> tuple[str, ...]:
    if actor_telegram_id not in superadmin_ids:
        raise AdminActionError("Faqat superadmin huquqlarni boshqara oladi")
    unknown = permissions - ALL_ADMIN_PERMISSIONS
    if unknown:
        raise AdminActionError(f"Noma’lum permission: {', '.join(sorted(unknown))}")
    if permissions & SUPERADMIN_ONLY_PERMISSIONS:
        raise AdminActionError("Superadmin-only huquqlar oddiy adminga berilmaydi")
    user = await session.scalar(
        select(User).where(User.telegram_id == target_telegram_id).with_for_update()
    )
    if user is None or not user.is_admin or not user.admin_active:
        raise AdminActionError("Faol admin topilmadi")
    old = set(
        await session.scalars(
            select(AdminPermission.permission).where(AdminPermission.user_id == user.id)
        )
    )
    if old == permissions:
        return tuple(sorted(old))
    await session.execute(delete(AdminPermission).where(AdminPermission.user_id == user.id))
    session.add_all(
        AdminPermission(
            user_id=user.id,
            permission=permission,
            granted_by_telegram_id=actor_telegram_id,
        )
        for permission in sorted(permissions)
    )
    user.admin_session_version += 1
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="ADMIN_PERMISSIONS_CHANGED",
        entity_type="USER",
        entity_id=user.id,
        summary=f"Admin huquqlari yangilandi: {target_telegram_id}",
        old_values={"permissions": sorted(old)},
        new_values={"permissions": sorted(permissions)},
    )
    return tuple(sorted(permissions))


async def _release_payment_reviews(session: AsyncSession, telegram_id: int) -> None:
    await session.execute(
        update(Payment)
        .where(
            Payment.reviewed_by_admin_id == telegram_id,
            Payment.status == PaymentStatus.REVIEW_PENDING.value,
        )
        .values(reviewed_by_admin_id=None)
    )


async def admin_recipients(
    session: AsyncSession, *, superadmin_ids: frozenset[int]
) -> frozenset[int]:
    admin_ids = set(
        await session.scalars(
            select(User.telegram_id).where(User.is_admin.is_(True), User.admin_active.is_(True))
        )
    )
    return frozenset(admin_ids | set(superadmin_ids))


async def set_service_type_enabled(
    session: AsyncSession,
    *,
    service_type: ServiceType,
    enabled: bool,
    actor_telegram_id: int,
) -> int:
    provider = await session.scalar(select(Provider).where(Provider.code == "DIRECT"))
    if provider is None:
        raise AdminActionError("Provider not found")
    result = await session.execute(
        update(ProviderService)
        .where(
            ProviderService.provider_id == provider.id,
            ProviderService.service_type == service_type,
        )
        .values(active=enabled)
    )
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="SERVICE_ENABLED" if enabled else "SERVICE_DISABLED",
        entity_type="PROVIDER_SERVICE_TYPE",
        entity_id=service_type.value,
    )
    return result.rowcount


async def set_order_priority(
    session: AsyncSession, *, public_number: str, priority: int, actor_telegram_id: int
) -> None:
    if not -100 <= priority <= 100:
        raise AdminActionError("Priority must be between -100 and 100")
    order = await session.scalar(
        select(Order).where(Order.public_order_number == public_number).with_for_update()
    )
    if order is None:
        raise AdminActionError("Order not found")
    before = order.priority
    order.priority = priority
    write_audit(
        session,
        actor_type="ADMIN",
        actor_id=str(actor_telegram_id),
        action="ORDER_PRIORITY_CHANGED",
        entity_type="ORDER",
        entity_id=order.id,
        old_values={"priority": before},
        new_values={"priority": priority},
    )


async def manual_refund(
    session: AsyncSession, *, public_number: str, actor_telegram_id: int
) -> None:
    order = await session.scalar(
        select(Order).where(Order.public_order_number == public_number).with_for_update()
    )
    if order is None:
        raise AdminActionError("Order not found")
    await refund_order_funds(session, order_id=order.id)
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="MANUAL_REFUND_COMPLETED",
        entity_type="ORDER",
        entity_id=order.id,
    )


async def audit_history(
    session: AsyncSession, *, public_number: str, limit: int = 10
) -> list[AuditLog]:
    order = await session.scalar(select(Order).where(Order.public_order_number == public_number))
    if order is None:
        raise AdminActionError("Order not found")
    return list(
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "ORDER", AuditLog.entity_id == order.id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    )
