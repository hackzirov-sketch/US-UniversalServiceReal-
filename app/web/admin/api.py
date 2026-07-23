from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import from_url
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import logger
from app.db.enums import OrderStatus, PaymentStatus, ServiceType
from app.db.models import (
    AuditLog,
    FarmProfile,
    FarmReward,
    Order,
    Payment,
    User,
)
from app.db.session import session_factory
from app.services.admin import (
    AdminActionError,
    add_admin,
    list_admin_cards,
    remove_admin,
    replace_admin_permissions,
)
from app.services.audit import audit_text, list_business_audit
from app.services.farm import FarmError, reverse_reward
from app.services.manual_pricing import (
    ManualPriceInput,
    ManualPricingError,
    create_manual_price,
    deactivate_manual_price,
    list_active_manual_prices,
    pricing_actor,
)
from app.services.payments import (
    PaymentError,
    approve_payment,
    payment_actor,
    reject_payment,
    request_payment_info,
)
from app.services.preflight import run_preflight, runtime_sales_enabled, set_runtime_sales
from app.services.rewards import RewardError, grant_bonus, ranking
from app.web.common.auth import require_admin, verify_csrf
from app.web.common.serializers import public_order, public_payment

router = APIRouter(prefix="/web-api/admin", tags=["admin-web-app"])


class ReviewBody(BaseModel):
    approved_amount_som: int | None = Field(default=None, gt=0)
    confirm: bool = False


class AdminBody(BaseModel):
    reference: str = Field(min_length=1, max_length=64)
    confirm: bool = False


class PermissionBody(BaseModel):
    permissions: set[str]
    confirm: bool = False


class ToggleBody(BaseModel):
    enabled: bool
    confirm: bool = False


class InfoBody(BaseModel):
    note: str = Field(min_length=1, max_length=500)
    confirm: bool = False


class BonusBody(BaseModel):
    telegram_id: int = Field(gt=0)
    amount_som: int = Field(gt=0, le=100_000_000)
    reason: str = Field(min_length=1, max_length=300)
    confirm: bool = False


class PriceBody(BaseModel):
    service_type: str
    provider_cost_som: int = Field(gt=0)
    sale_price_som: int = Field(gt=0)
    display_name: str = Field(min_length=1, max_length=128)
    min_quantity: int | None = None
    max_quantity: int | None = None
    premium_months: int | None = None
    gift_name: str | None = Field(default=None, max_length=128)
    allow_comment: bool = False
    duration_hours: int | None = 24
    reason: str | None = Field(default=None, max_length=300)
    confirm: bool = False


@router.get("/dashboard")
async def dashboard(request: Request):
    await require_admin(request)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_factory() as session:
        revenue = int(
            await session.scalar(
                select(func.coalesce(func.sum(Payment.approved_amount_som), 0)).where(
                    Payment.status == PaymentStatus.APPROVED.value,
                    Payment.approved_at >= today,
                )
            )
            or 0
        )
        pending = int(
            await session.scalar(
                select(func.count())
                .select_from(Payment)
                .where(Payment.status == PaymentStatus.REVIEW_PENDING.value)
            )
            or 0
        )
        order_count = int(await session.scalar(select(func.count()).select_from(Order)) or 0)
        users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
        expected_profit = int(
            await session.scalar(
                select(func.coalesce(func.sum(Order.expected_profit_som), 0)).where(
                    Order.internal_status.not_in([OrderStatus.REFUNDED, OrderStatus.CANCELLED])
                )
            )
            or 0
        )
        audit = list(
            await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(8))
        )
    return {
        "revenue_today_som": revenue,
        "pending_payments": pending,
        "orders": order_count,
        "users": users,
        "expected_profit_som": expected_profit,
        "audit": [audit_text(row) for row in audit],
    }


@router.get("/payments")
async def payments(request: Request):
    await require_admin(request, "REVIEW_PAYMENTS")
    async with session_factory() as session:
        rows = list(
            await session.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(100))
        )
    return {
        "items": [
            public_payment(row)
            | {
                "user_id": row.user_id,
                "receipt_type": row.receipt_file_type,
                "receipt_size": row.receipt_file_size,
            }
            for row in rows
        ]
    }


@router.post("/payments/{payment_id}/approve")
async def approve(payment_id: str, body: ReviewBody, request: Request):
    context = await require_admin(request, "APPROVE_PAYMENTS")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    if (
        body.approved_amount_som is not None
        and not context.is_superadmin
        and "EDIT_APPROVED_AMOUNT" not in context.permissions
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Summani o‘zgartirish huquqi yo‘q")
    try:
        async with session_factory.begin() as session:
            actor = await payment_actor(
                session,
                telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            row = await approve_payment(
                session,
                payment_id=payment_id,
                actor=actor,
                approved_amount_som=body.approved_amount_som,
            )
            return {"payment": public_payment(row)}
    except PaymentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("/payments/{payment_id}/reject")
async def reject(payment_id: str, body: ReviewBody, request: Request):
    context = await require_admin(request, "REJECT_PAYMENTS")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    try:
        async with session_factory.begin() as session:
            actor = await payment_actor(
                session,
                telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            row = await reject_payment(session, payment_id=payment_id, actor=actor)
            return {"payment": public_payment(row)}
    except PaymentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("/payments/{payment_id}/request-info")
async def payment_info(payment_id: str, body: InfoBody, request: Request):
    context = await require_admin(request, "REVIEW_PAYMENTS")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    try:
        async with session_factory.begin() as session:
            actor = await payment_actor(
                session,
                telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            row = await request_payment_info(
                session, payment_id=payment_id, actor=actor, note=body.note
            )
            return {"payment": public_payment(row)}
    except PaymentError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/farm")
async def farm_admin(request: Request):
    await require_admin(request, "MANAGE_FARM")
    async with session_factory() as session:
        profiles = int(await session.scalar(select(func.count()).select_from(FarmProfile)) or 0)
        rewards = list(
            await session.scalars(
                select(FarmReward).order_by(FarmReward.granted_at.desc()).limit(100)
            )
        )
    return {
        "profiles": profiles,
        "rewards": [
            {
                "id": row.id,
                "user_id": row.user_id,
                "amount": row.amount,
                "granted_at": row.granted_at.isoformat(),
                "reversed": row.reversed_at is not None,
            }
            for row in rewards
        ],
    }


@router.post("/farm/rewards/{reward_id}/reverse")
async def farm_reward_reverse(reward_id: str, body: ToggleBody, request: Request):
    context = await require_admin(request, "MANAGE_FARM")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    try:
        async with session_factory.begin() as session:
            row = await reverse_reward(
                session, reward_id=reward_id, actor_telegram_id=context.user.telegram_id
            )
            return {"id": row.id, "reversed": row.reversed_at is not None}
    except FarmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/bonuses")
async def bonuses(request: Request):
    await require_admin(request, "MANAGE_BONUSES")
    async with session_factory() as session:
        total = int(await session.scalar(select(func.sum(User.bonus_balance_som))) or 0)
    return {"total_bonus_som": total}


@router.post("/bonuses/grant")
async def bonus_grant(body: BonusBody, request: Request):
    context = await require_admin(request, "MANAGE_BONUSES")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    try:
        async with session_factory.begin() as session:
            user = await grant_bonus(
                session,
                telegram_id=body.telegram_id,
                amount_som=body.amount_som,
                actor_telegram_id=context.user.telegram_id,
                reason=body.reason,
            )
            return {"telegram_id": user.telegram_id, "bonus_som": user.bonus_balance_som}
    except RewardError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/ranking")
async def ranking_admin(request: Request):
    await require_admin(request, "MANAGE_RANKING")
    async with session_factory() as session:
        rows = await ranking(session, limit=100)
    return {
        "items": [
            {
                "telegram_id": row.telegram_id,
                "username": row.username,
                "points": row.ranking_points,
            }
            for row in rows
        ]
    }


@router.get("/orders")
async def orders(request: Request):
    await require_admin(request, "VIEW_ORDERS")
    async with session_factory() as session:
        rows = list(
            await session.scalars(select(Order).order_by(Order.created_at.desc()).limit(100))
        )
    return {
        "items": [
            public_order(row)
            | {"user_id": row.user_id, "expected_profit_som": row.expected_profit_som}
            for row in rows
        ]
    }


@router.get("/pricing")
async def pricing(request: Request):
    await require_admin(request, "MANAGE_PRICING")
    async with session_factory() as session:
        rows = await list_active_manual_prices(session)
    return {
        "items": [
            {
                "id": row.id,
                "name": row.display_name,
                "type": row.service_type.value,
                "provider_cost_som": row.provider_cost_som,
                "sale_price_som": row.sale_price_som,
                "version": row.version,
                "active": row.active,
            }
            for row in rows
        ]
    }


@router.post("/pricing")
async def pricing_create(body: PriceBody, request: Request):
    context = await require_admin(request, "MANAGE_PRICING")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    try:
        service_type = ServiceType(body.service_type)
        async with session_factory.begin() as session:
            actor = await pricing_actor(
                session,
                telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            row = await create_manual_price(
                session,
                data=ManualPriceInput(
                    service_type=service_type,
                    provider_cost_som=body.provider_cost_som,
                    sale_price_som=body.sale_price_som,
                    display_name=body.display_name,
                    min_quantity=body.min_quantity,
                    max_quantity=body.max_quantity,
                    premium_months=body.premium_months,
                    gift_name=body.gift_name,
                    allow_comment=body.allow_comment,
                    duration_hours=body.duration_hours,
                    source_note=body.reason,
                ),
                actor=actor,
                requires_superadmin_approval=get_settings().pricing_requires_superadmin_approval,
                min_profit_percent=get_settings().min_profit_percent,
                min_profit_som=get_settings().min_profit_som,
            )
            return {"id": row.id, "version": row.version, "active": row.active}
    except (ValueError, ManualPricingError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.post("/pricing/{price_id}/deactivate")
async def pricing_deactivate(price_id: str, body: ToggleBody, request: Request):
    context = await require_admin(request, "MANAGE_PRICING")
    verify_csrf(request, context)
    if not body.confirm:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ikkinchi tasdiq kerak")
    try:
        async with session_factory.begin() as session:
            actor = await pricing_actor(
                session,
                telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            row = await deactivate_manual_price(session, price_id=price_id, actor=actor)
            return {"id": row.id, "active": row.active}
    except ManualPricingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/users")
async def users(request: Request):
    await require_admin(request, "VIEW_USERS")
    async with session_factory() as session:
        rows = list(await session.scalars(select(User).order_by(User.created_at.desc()).limit(100)))
    return {
        "items": [
            {
                "telegram_id": row.telegram_id,
                "username": row.username,
                "full_name": row.full_name,
                "balance_som": row.available_balance_som,
                "bonus_som": row.bonus_balance_som,
                "farm_points": row.farm_points,
                "active": row.admin_active,
            }
            for row in rows
        ]
    }


@router.get("/admins")
async def admins(request: Request):
    await require_admin(request, "MANAGE_ADMINS")
    async with session_factory() as session:
        cards = await list_admin_cards(session)
    return {
        "items": [
            {
                "telegram_id": card.user.telegram_id,
                "username": card.user.username,
                "active": card.user.admin_active,
                "role": card.user.role,
                "permissions": card.permissions,
            }
            for card in cards
        ]
    }


@router.post("/admins")
async def admin_add(body: AdminBody, request: Request):
    context = await require_admin(request, "MANAGE_ADMINS")
    verify_csrf(request, context)
    if not context.is_superadmin or not body.confirm:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN if not context.is_superadmin else status.HTTP_409_CONFLICT,
            "Superadmin va ikkinchi tasdiq kerak",
        )
    try:
        async with session_factory.begin() as session:
            row = await add_admin(
                session,
                reference=body.reference,
                actor_telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            return {"telegram_id": row.telegram_id, "role": row.role}
    except AdminActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.delete("/admins/{telegram_id}")
async def admin_remove(telegram_id: int, body: ToggleBody, request: Request):
    context = await require_admin(request, "MANAGE_ADMINS")
    verify_csrf(request, context)
    if not context.is_superadmin or not body.confirm:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN if not context.is_superadmin else status.HTTP_409_CONFLICT,
            "Superadmin va ikkinchi tasdiq kerak",
        )
    try:
        async with session_factory.begin() as session:
            row = await remove_admin(
                session,
                reference=str(telegram_id),
                actor_telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            return {"telegram_id": row.telegram_id, "role": row.role}
    except AdminActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.put("/admins/{telegram_id}/permissions")
async def admin_permissions(telegram_id: int, body: PermissionBody, request: Request):
    context = await require_admin(request, "MANAGE_ADMINS")
    verify_csrf(request, context)
    if not context.is_superadmin or not body.confirm:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN if not context.is_superadmin else status.HTTP_409_CONFLICT,
            "Superadmin va ikkinchi tasdiq kerak",
        )
    try:
        async with session_factory.begin() as session:
            permissions = await replace_admin_permissions(
                session,
                target_telegram_id=telegram_id,
                permissions=body.permissions,
                actor_telegram_id=context.user.telegram_id,
                superadmin_ids=get_settings().superadmin_ids,
            )
            return {"telegram_id": telegram_id, "permissions": list(permissions)}
    except AdminActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.get("/audit")
async def audit(request: Request):
    await require_admin(request, "VIEW_AUDIT")
    async with session_factory() as session:
        rows = await list_business_audit(session, limit=100)
    return {
        "items": [
            {
                "action": row.action,
                "summary": audit_text(row),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/real-sales")
async def real_sales(request: Request):
    await require_admin(request, "ENABLE_REAL_SALES")
    async with session_factory() as session:
        runtime = await runtime_sales_enabled(session)
    return {
        "environment_gate": get_settings().direct_sales_enabled,
        "runtime_gate": runtime,
        "effective": get_settings().direct_sales_enabled and runtime,
    }


@router.post("/real-sales")
async def real_sales_toggle(body: ToggleBody, request: Request):
    context = await require_admin(
        request, "ENABLE_REAL_SALES" if body.enabled else "DISABLE_REAL_SALES"
    )
    verify_csrf(request, context)
    if not context.is_superadmin or not body.confirm:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN if not context.is_superadmin else status.HTTP_409_CONFLICT,
            "Superadmin va ikkinchi tasdiq kerak",
        )
    if body.enabled and not get_settings().direct_sales_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Environment purchase gate o‘chiq")
    async with session_factory.begin() as session:
        await set_runtime_sales(
            session,
            enabled=body.enabled,
            actor_telegram_id=context.user.telegram_id,
            superadmin_ids=get_settings().superadmin_ids,
            environment_enabled=get_settings().direct_sales_enabled,
        )
    return {"runtime_gate": body.enabled}


@router.post("/preflight")
async def preflight(request: Request):
    context = await require_admin(request, "ENABLE_REAL_SALES")
    verify_csrf(request, context)
    probes = await _infrastructure_probes()
    async with session_factory.begin() as session:
        result = await run_preflight(
            session,
            settings=get_settings(),
            actor_telegram_id=context.user.telegram_id,
            infrastructure=probes,
        )
    return {"success": result.success, "checks": result.checks}


async def _infrastructure_probes() -> dict[str, bool]:
    settings = get_settings()
    probes = {"redis": False, "telegram": False}
    redis_client = from_url(settings.redis_url, decode_responses=True)
    try:
        probes["redis"] = bool(await redis_client.ping())
    except Exception as exc:
        logger.warning("Web preflight Redis probe failed: %s", type(exc).__name__)
    finally:
        await redis_client.aclose()
    if settings.telegram_bot_token is not None:
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        try:
            await bot.get_me()
            probes["telegram"] = True
        except Exception as exc:
            logger.warning("Web preflight Telegram probe failed: %s", type(exc).__name__)
        finally:
            await bot.session.close()
    return probes
