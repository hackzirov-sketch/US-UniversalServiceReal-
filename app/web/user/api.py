from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from aiogram import Bot
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.logging import logger
from app.db.enums import OrderStatus, ServiceType
from app.db.models import LedgerEntry, ManualProviderPrice, Order, Payment, User
from app.db.session import session_factory
from app.services.farm import FarmError, get_farm, harvest, plant, water
from app.services.manual_pricing import (
    ManualPricingError,
    create_price_quote,
    list_active_manual_prices,
)
from app.services.payments import (
    CardCipher,
    PaymentError,
    attach_payment_receipt,
    create_topup_payment,
    get_user_payment_card,
)
from app.services.rewards import ranking
from app.web.common.auth import current_session, token_hash, verify_csrf
from app.web.common.security import rate_limit
from app.web.common.serializers import public_order, public_payment, public_price

router = APIRouter(prefix="/web-api/user", tags=["user-web-app"])


class QuoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_id: str = Field(min_length=1, max_length=64)
    quantity: int | None = Field(default=None, ge=1, le=100_000)


class TopupBody(BaseModel):
    amount_som: int = Field(gt=0, le=100_000_000)


class FarmBody(BaseModel):
    slot: int = Field(ge=0, le=99)


@router.get("/dashboard")
async def dashboard(request: Request):
    context = await current_session(request)
    async with session_factory.begin() as session:
        profile, _ = await get_farm(session, user_id=context.user.id)
        active_orders = int(
            await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.user_id == context.user.id,
                    Order.internal_status.not_in(
                        [OrderStatus.COMPLETED, OrderStatus.REFUNDED, OrderStatus.CANCELLED]
                    ),
                )
            )
            or 0
        )
    return {
        "name": context.user.full_name or context.user.username or str(context.user.telegram_id),
        "balance_som": context.user.available_balance_som,
        "bonus_balance_som": context.user.bonus_balance_som,
        "farm_points": context.user.farm_points,
        "ranking_points": context.user.ranking_points,
        "farm_level": profile.level,
        "active_orders": active_orders,
        "purchase_enabled": False,
    }


@router.get("/catalog")
async def catalog(request: Request, service_type: ServiceType | None = None):
    await current_session(request)
    async with session_factory() as session:
        prices = await list_active_manual_prices(session, service_type=service_type)
    return {"items": [public_price(price) for price in prices]}


@router.post("/quotes")
async def quote(body: QuoteBody, request: Request):
    context = await current_session(request)
    verify_csrf(request, context)
    try:
        async with session_factory.begin() as session:
            price = await session.get(ManualProviderPrice, body.price_id)
            if price is None or not price.active:
                raise ManualPricingError("Narx topilmadi yoki faol emas")
            row = await create_price_quote(
                session,
                user_id=context.user.id,
                service_type=price.service_type,
                quantity=body.quantity,
                premium_months=price.premium_months,
                gift_name=price.gift_name,
                quote_ttl_seconds=get_settings().myxvest_quote_ttl_seconds,
            )
            if row.manual_price_id != price.id:
                raise ManualPricingError("Tanlangan narx eskirgan")
            await session.flush()
            return {
                "quote_id": row.id,
                "sale_price_som": row.sale_price_som,
                "expires_at": row.expires_at.isoformat(),
                "purchase_enabled": False,
                "message": "Xizmat hozircha test rejimida.",
            }
    except ManualPricingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.get("/balance")
async def balance(request: Request):
    context = await current_session(request)
    async with session_factory() as session:
        history = list(
            await session.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == context.user.id)
                .order_by(LedgerEntry.created_at.desc())
                .limit(30)
            )
        )
    return {
        "available_som": context.user.available_balance_som,
        "reserved_som": context.user.reserved_balance_som,
        "bonus_som": context.user.bonus_balance_som,
        "history": [
            {
                "type": row.type.value,
                "amount_som": row.amount_som,
                "balance_after_som": row.balance_after_som,
                "created_at": row.created_at.isoformat(),
            }
            for row in history
        ],
    }


@router.get("/orders")
async def orders(request: Request):
    context = await current_session(request)
    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(Order)
                .where(Order.user_id == context.user.id)
                .order_by(Order.created_at.desc())
                .limit(50)
            )
        )
    return {"items": [public_order(row) for row in rows]}


@router.get("/rewards")
async def rewards(request: Request):
    context = await current_session(request)
    return {
        "bonus_som": context.user.bonus_balance_som,
        "farm_points": context.user.farm_points,
        "ranking_points": context.user.ranking_points,
    }


@router.get("/ranking")
async def ranking_list(request: Request):
    context = await current_session(request)
    async with session_factory() as session:
        rows = await ranking(session, limit=50)
    return {
        "me": context.user.ranking_points,
        "items": [
            {
                "name": row.full_name or row.username or str(row.telegram_id),
                "points": row.ranking_points,
                "is_me": row.id == context.user.id,
            }
            for row in rows
        ],
    }


@router.get("/payments")
async def payments(request: Request):
    context = await current_session(request)
    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(Payment)
                .where(Payment.user_id == context.user.id)
                .order_by(Payment.created_at.desc())
                .limit(30)
            )
        )
    return {"items": [public_payment(row) for row in rows]}


@router.post("/topup")
async def topup(body: TopupBody, request: Request):
    context = await current_session(request)
    verify_csrf(request, context)
    settings = get_settings()
    if settings.payment_card_encryption_key is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Karta sozlanmagan")
    cipher = CardCipher(settings.payment_card_encryption_key.get_secret_value())
    try:
        async with session_factory.begin() as session:
            card = await get_user_payment_card(session, cipher=cipher)
            payment = await create_topup_payment(
                session, user_id=context.user.id, amount_som=body.amount_som, cipher=cipher
            )
            return {
                "payment": public_payment(payment),
                "card_number": card.formatted_card_number,
                "card_holder": card.card_holder_name,
            }
    except PaymentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.post("/topup/{payment_id}/receipt")
async def upload_receipt(
    payment_id: str,
    request: Request,
    receipt: Annotated[UploadFile, File()],
    csrf: Annotated[str, Form()],
):
    context = await current_session(request)
    if not secrets.compare_digest(request.cookies.get("us_csrf", ""), csrf):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token noto‘g‘ri")
    if not secrets.compare_digest(token_hash(csrf), context.web_session.csrf_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token noto‘g‘ri")
    await rate_limit(request, "receipt")
    settings = get_settings()
    content = await receipt.read(settings.max_receipt_bytes + 1)
    mime = _receipt_mime(content)
    if len(content) > settings.max_receipt_bytes or mime is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Chek JPG, PNG yoki PDF bo‘lishi kerak"
        )
    if settings.telegram_bot_token is None or settings.payment_review_chat_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chek saqlash kanali sozlanmagan")
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        message = await bot.send_document(
            settings.payment_review_chat_id,
            BufferedInputFile(content, filename=receipt.filename or "receipt"),
            caption=f"Web receipt: {payment_id}",
        )
        recipients = (
            set(settings.superadmin_ids)
            if settings.payment_review_notify_superadmins
            else set()
        )
        if settings.payment_review_notify_admins:
            async with session_factory() as session:
                recipients.update(
                    await session.scalars(
                        select(User.telegram_id).where(
                            User.is_admin.is_(True), User.admin_active.is_(True)
                        )
                    )
                )
        for recipient in recipients:
            if recipient == settings.payment_review_chat_id:
                continue
            try:
                await bot.copy_message(
                    chat_id=recipient,
                    from_chat_id=settings.payment_review_chat_id,
                    message_id=message.message_id,
                )
            except Exception as exc:
                logger.warning("Receipt admin notification failed: %s", type(exc).__name__)
    finally:
        await bot.session.close()
    document = message.document
    if document is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Chek saqlanmadi")
    try:
        async with session_factory.begin() as session:
            payment = await attach_payment_receipt(
                session,
                payment_id=payment_id,
                user_id=context.user.id,
                file_id=document.file_id,
                file_unique_id=document.file_unique_id,
                checksum=hashlib.sha256(content).hexdigest(),
                file_type="PDF" if mime == "application/pdf" else "PHOTO",
                mime_type=mime,
                file_size=len(content),
            )
            return {"payment": public_payment(payment)}
    except PaymentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.get("/farm")
async def farm_state(request: Request):
    context = await current_session(request)
    async with session_factory.begin() as session:
        profile, plots = await get_farm(session, user_id=context.user.id)
        return {
            "profile": {
                "energy": profile.energy,
                "water": profile.water,
                "seeds": profile.seeds,
                "xp": profile.xp,
                "level": profile.level,
            },
            "points": context.user.farm_points,
            "plots": [
                {
                    "slot": p.slot,
                    "state": p.state.value,
                    "crop": p.crop,
                    "ready_at": p.ready_at.isoformat() if p.ready_at else None,
                }
                for p in plots
            ],
        }


@router.post("/farm/{action}")
async def farm_action(action: str, body: FarmBody, request: Request):
    context = await current_session(request)
    verify_csrf(request, context)
    try:
        async with session_factory.begin() as session:
            if action == "plant":
                await plant(session, user_id=context.user.id, slot=body.slot)
                result = {"ok": True}
            elif action == "water":
                await water(session, user_id=context.user.id, slot=body.slot)
                result = {"ok": True}
            elif action == "harvest":
                result = {
                    "ok": True,
                    "reward": await harvest(session, user_id=context.user.id, slot=body.slot),
                }
            else:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Farm amali topilmadi")
            return result
    except FarmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


def _receipt_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None
