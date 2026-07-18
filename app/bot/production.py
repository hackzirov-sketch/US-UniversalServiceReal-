from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

import redis.asyncio as redis
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import desc, select

from app.bot.buttons import inline_button
from app.core.config import get_settings
from app.db.enums import OrderStatus, ServiceType
from app.db.models import ManualProviderPrice, Order, Provider, ProviderBalanceSnapshot, User
from app.db.session import session_factory
from app.integrations.providers.myxvest.client import MyxvestClient
from app.services.balance import BalanceError, reserve_order_funds
from app.services.preflight import (
    SalesGateError,
    confirmation_code,
    run_preflight,
    runtime_sales_enabled,
    set_runtime_sales,
)
from app.services.provider import ProviderWorkflow

_USERNAME = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
logger = logging.getLogger(__name__)


class ProductionStates(StatesGroup):
    enable_code = State()
    test_recipient = State()
    test_confirm = State()


def sales_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="🔍 Preflight tekshirish",
                    callback_data="sales:preflight",
                    style="primary",
                    emoji_key="preflight",
                )
            ],
            [
                inline_button(
                    text="🧪 Nazoratli test buyurtma",
                    callback_data="sales:test",
                    style="primary",
                    emoji_key="controlled_test",
                )
            ],
            [
                inline_button(
                    text="▶️ Real savdoni yoqish",
                    callback_data="sales:enable",
                    style="success",
                    emoji_key="enable",
                )
            ],
            [
                inline_button(
                    text="⏸ Real savdoni o‘chirish",
                    callback_data="sales:disable",
                    style="danger",
                    emoji_key="disable",
                )
            ],
            [
                inline_button(
                    text="📜 Savdo auditlari", callback_data="admin:audit", emoji_key="audit"
                )
            ],
            [inline_button(text="◀️ Orqaga", callback_data="nav:home", emoji_key="back")],
        ]
    )


def build_production_router(workflow: ProviderWorkflow) -> Router:
    router = Router(name="production_sales")

    async def allowed(callback: CallbackQuery) -> bool:
        if callback.from_user.id not in get_settings().superadmin_ids:
            await callback.answer("Faqat superadmin uchun", show_alert=True)
            return False
        return True

    @router.callback_query(F.data == "sales:home")
    async def home(callback: CallbackQuery) -> None:
        if not await allowed(callback):
            return
        await _answer(callback, await _status_text(), sales_menu())

    @router.callback_query(F.data == "sales:preflight")
    async def preflight(callback: CallbackQuery) -> None:
        if not await allowed(callback):
            return
        report = await _run_live_preflight(callback)
        await _answer(callback, _report_text(report), sales_menu())

    @router.callback_query(F.data == "sales:enable")
    async def enable_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await allowed(callback):
            return
        report = await _run_live_preflight(callback)
        if not report.success:
            await _answer(callback, _report_text(report), sales_menu())
            return
        code = confirmation_code()
        await state.set_state(ProductionStates.enable_code)
        await state.update_data(enable_code=code, preflight_id=report.result_id)
        await _answer(
            callback,
            "Barcha preflight tekshiruvlari muvaffaqiyatli.\n"
            f"Real savdoni yoqish uchun quyidagi kodni yuboring: {code}\n"
            "Kod bir martalik va joriy preflightga bog‘langan.",
        )

    @router.message(ProductionStates.enable_code)
    async def enable_confirm(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.from_user.id not in get_settings().superadmin_ids:
            return
        data = await state.get_data()
        if (message.text or "").strip() != data.get("enable_code"):
            await message.answer("Tasdiq kodi noto‘g‘ri. Real savdo yoqilmadi.")
            return
        settings = get_settings()
        try:
            async with session_factory.begin() as session:
                await set_runtime_sales(
                    session,
                    enabled=True,
                    actor_telegram_id=message.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                    environment_enabled=settings.myxvest_purchase_enabled,
                )
            await state.clear()
            await message.answer("✅ Real savdo yoqildi.", reply_markup=sales_menu())
            await _notify_superadmins(
                message.bot, "🚀 Real savdo superadmin tasdig‘i bilan yoqildi."
            )
        except SalesGateError as exc:
            await message.answer(f"Real savdo yoqilmadi: {exc}", reply_markup=sales_menu())

    @router.callback_query(F.data == "sales:disable")
    async def disable(callback: CallbackQuery, state: FSMContext) -> None:
        if not await allowed(callback):
            return
        settings = get_settings()
        async with session_factory.begin() as session:
            await set_runtime_sales(
                session,
                enabled=False,
                actor_telegram_id=callback.from_user.id,
                superadmin_ids=settings.superadmin_ids,
                environment_enabled=settings.myxvest_purchase_enabled,
            )
        await state.clear()
        await _answer(
            callback,
            "⏸ Real savdo o‘chirildi. Yangi provider orderlar bloklandi; "
            "polling va reconciliation davom etadi.",
            sales_menu(),
        )
        await _notify_superadmins(callback.bot, "⏸ Real savdo superadmin tomonidan o‘chirildi.")

    @router.callback_query(F.data == "sales:test")
    async def test_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await allowed(callback):
            return
        await state.set_state(ProductionStates.test_recipient)
        await _answer(callback, "Nazoratli minimal Stars test recipientini kiriting (@username):")

    @router.message(ProductionStates.test_recipient)
    async def test_recipient(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.from_user.id not in get_settings().superadmin_ids:
            return
        recipient = (message.text or "").strip()
        if not _USERNAME.fullmatch(recipient):
            await message.answer("Telegram @username formati noto‘g‘ri.")
            return
        recipient = recipient.lstrip("@").casefold()
        async with session_factory() as session:
            price = await _active_stars_price(session)
            balance = await _latest_provider_balance(session)
            actor = await session.scalar(
                select(User).where(User.telegram_id == message.from_user.id)
            )
        if price is None:
            await message.answer("Active Stars manual narxi yo‘q.", reply_markup=sales_menu())
            return
        quantity = price.min_quantity or 50
        total = price.sale_price_som * quantity
        if actor is None or actor.available_balance_som < total:
            await message.answer(
                f"Test uchun superadmin balansida kamida {total:,} so‘m bo‘lishi kerak.",
                reply_markup=sales_menu(),
            )
            return
        await state.update_data(recipient=recipient, price_id=price.id, price_version=price.version)
        await state.set_state(ProductionStates.test_confirm)
        await message.answer(
            "🧪 Nazoratli test buyurtma\n\n"
            f"Recipient: @{recipient}\n"
            f"Miqdor: {quantity} Stars\n"
            f"Bizdagi narx: {total:,} so‘m\n"
            f"Provider balans: {balance if balance is not None else 'noma’lum'}\n\n"
            "Tasdiqlash real provider purchase’ni aynan bir marta yuboradi.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        inline_button(
                            text="✅ Real testni yuborish",
                            callback_data="sales:test:confirm",
                            style="success",
                            emoji_key="confirm",
                        )
                    ],
                    [
                        inline_button(
                            text="❌ Bekor qilish",
                            callback_data="sales:home",
                            style="danger",
                            emoji_key="cancel",
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data == "sales:test:confirm", ProductionStates.test_confirm)
    async def test_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if not await allowed(callback):
            return
        data = await state.get_data()
        order_id: str | None = None
        try:
            async with session_factory.begin() as session:
                actor = await session.scalar(
                    select(User).where(User.telegram_id == callback.from_user.id).with_for_update()
                )
                price = await session.scalar(
                    select(ManualProviderPrice)
                    .where(
                        ManualProviderPrice.id == data.get("price_id"),
                        ManualProviderPrice.version == data.get("price_version"),
                        ManualProviderPrice.active.is_(True),
                    )
                    .with_for_update()
                )
                provider = await session.scalar(
                    select(Provider).where(Provider.code == "MYXVEST").with_for_update()
                )
                if actor is None or price is None or provider is None:
                    raise SalesGateError("Test preview eskirgan")
                quantity = price.min_quantity or 50
                latest_balance = await _latest_provider_balance(session)
                cost = price.provider_cost_som * quantity
                if latest_balance is None or latest_balance < cost:
                    raise SalesGateError("Provider balansi test uchun yetarli emas")
                order = Order(
                    public_order_number=f"TST-{uuid.uuid4().hex[:12].upper()}",
                    user_id=actor.id,
                    provider_id=provider.id,
                    service_type=ServiceType.STARS,
                    target_username_original=f"@{data['recipient']}",
                    target_username=data["recipient"],
                    quantity=quantity,
                    provider_cost_som=cost,
                    sale_price_som=price.sale_price_som * quantity,
                    expected_profit_som=(price.sale_price_som - price.provider_cost_som) * quantity,
                    quote_expires_at=datetime.now(UTC),
                    internal_status=OrderStatus.DRAFT,
                    idempotency_key=f"controlled-test:{uuid.uuid4()}",
                )
                session.add(order)
                await session.flush()
                order_id = order.id
                await reserve_order_funds(session, order_id=order.id)
                order.internal_status = OrderStatus.READY_TO_SUBMIT
            await workflow.submit_order(order_id, controlled_test=True)
            async with session_factory() as session:
                completed = await session.scalar(select(Order).where(Order.id == order_id))
            if completed is None or completed.internal_status != OrderStatus.COMPLETED:
                await _fail_closed(callback.from_user.id)
                status = completed.internal_status if completed else "missing"
                raise SalesGateError(f"Test noaniq/yakunlanmagan: {status}")
            await state.clear()
            await _answer(
                callback,
                f"✅ Nazoratli test muvaffaqiyatli: #{completed.public_order_number}. "
                "Ledger va foyda yozildi.",
                sales_menu(),
            )
        except (SalesGateError, BalanceError) as exc:
            await state.clear()
            await _fail_closed(callback.from_user.id)
            await _answer(
                callback,
                f"❌ Nazoratli test muvaffaqiyatsiz: {exc}. Runtime gate avtomatik o‘chirildi.",
                sales_menu(),
            )
            await _notify_superadmins(callback.bot, f"🚨 Nazoratli test muvaffaqiyatsiz: {exc}")

    return router


async def _run_live_preflight(callback: CallbackQuery):
    settings = get_settings()
    probes = {"redis": False, "worker": False, "telegram": False, "provider_balance": False}
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        probes["redis"] = bool(await client.ping())
        keys = await client.keys("*health-check*")
        probes["worker"] = bool(keys)
    except Exception as exc:
        logger.warning("Redis/worker preflight probe failed: %s", type(exc).__name__)
    finally:
        await client.aclose()
    try:
        await callback.bot.get_me()
        probes["telegram"] = True
    except Exception as exc:
        logger.warning("Telegram preflight probe failed: %s", type(exc).__name__)
    if settings.myxvest_enabled and settings.myxvest_api_key:
        provider_client = MyxvestClient(
            base_url=settings.myxvest_base_url,
            api_key=settings.myxvest_api_key.get_secret_value(),
            timeout_seconds=settings.myxvest_timeout_seconds,
            max_retries=settings.myxvest_max_retries,
        )
        try:
            await provider_client.get_balance()
            probes["provider_balance"] = True
        except Exception as exc:
            logger.warning("Provider balance preflight probe failed: %s", type(exc).__name__)
        finally:
            await provider_client.aclose()
    async with session_factory.begin() as session:
        return await run_preflight(
            session,
            settings=settings,
            actor_telegram_id=callback.from_user.id,
            infrastructure=probes,
        )


async def _status_text() -> str:
    settings = get_settings()
    async with session_factory() as session:
        runtime = await runtime_sales_enabled(session)
        card = await session.scalar(select(Provider).where(Provider.code == "MYXVEST"))
    return (
        "🚀 Real savdo\n\n"
        f"Bot: {'🟢' if settings.telegram_bot_token else '🔴'}\n"
        "Database: 🟢\n"
        "Redis: preflight orqali\n"
        "Worker: preflight orqali\n"
        f"Provider: {'🟢' if card and card.enabled else '🔴'}\n"
        f"Environment gate: {'🟢' if settings.myxvest_purchase_enabled else '🔴'}\n"
        f"Runtime gate: {'🟢 Yoqiq' if runtime else '🔴 O‘chiq'}"
    )


def _report_text(report) -> str:
    lines = ["🔍 Production preflight", ""]
    lines.extend(
        f"{'🟢' if result['ok'] else '🔴'} {name}: {result['detail']}"
        for name, result in report.checks.items()
    )
    lines.append("")
    lines.append(
        "Natija: barcha tekshiruvlar muvaffaqiyatli"
        if report.success
        else "Natija: production bloklangan"
    )
    return "\n".join(lines)


async def _active_stars_price(session):
    now = datetime.now(UTC)
    return await session.scalar(
        select(ManualProviderPrice)
        .where(
            ManualProviderPrice.service_key == "MYXVEST:STARS",
            ManualProviderPrice.active.is_(True),
            ManualProviderPrice.valid_from <= now,
            (ManualProviderPrice.valid_until.is_(None)) | (ManualProviderPrice.valid_until > now),
        )
        .order_by(ManualProviderPrice.version.desc())
        .limit(1)
    )


async def _latest_provider_balance(session):
    return await session.scalar(
        select(ProviderBalanceSnapshot.balance_som)
        .where(ProviderBalanceSnapshot.success.is_(True))
        .order_by(desc(ProviderBalanceSnapshot.fetched_at))
        .limit(1)
    )


async def _fail_closed(actor_id: int) -> None:
    settings = get_settings()
    async with session_factory.begin() as session:
        await set_runtime_sales(
            session,
            enabled=False,
            actor_telegram_id=actor_id,
            superadmin_ids=settings.superadmin_ids,
            environment_enabled=settings.myxvest_purchase_enabled,
        )


async def _notify_superadmins(bot, text: str) -> None:
    for telegram_id in get_settings().superadmin_ids:
        try:
            await bot.send_message(telegram_id, text)
        except Exception as exc:
            logger.warning("Superadmin notification failed: %s", type(exc).__name__)
            continue


async def _answer(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=markup)
