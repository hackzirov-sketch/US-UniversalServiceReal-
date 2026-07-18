import asyncio

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.bot.admin_management import build_admin_management_router
from app.bot.button_design_admin import build_button_design_router
from app.bot.buttons import inline_button
from app.bot.menu import build_menu_router
from app.bot.messages import user_order_message
from app.bot.middleware import IdentityMiddleware
from app.bot.payments import build_payment_router
from app.bot.pricing import build_pricing_router
from app.bot.production import build_production_router
from app.core.config import get_settings
from app.core.security import is_superadmin
from app.db.enums import OrderStatus, ServiceType
from app.db.models import Order, Provider, ProviderBalanceSnapshot, User
from app.db.session import session_factory
from app.integrations.providers.myxvest.client import MyxvestClient
from app.services.admin import (
    AdminActionError,
    approve_safe_price_changed_order,
    audit_history,
    manual_refund,
    set_order_priority,
    set_provider_enabled,
    set_service_type_enabled,
)
from app.services.bootstrap import has_admin_access
from app.services.button_design import load_button_design_cache
from app.services.provider import ProviderWorkflow


def build_router(workflow: ProviderWorkflow) -> Router:
    router = Router()
    settings = get_settings()

    async def admin_allowed(telegram_id: int) -> bool:
        async with session_factory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            return bool(user and has_admin_access(user, settings.superadmin_ids))

    async def require_admin(message: Message, *, superadmin: bool = False) -> bool:
        if message.from_user is None:
            return False
        allowed = (
            is_superadmin(message.from_user.id, settings.superadmin_ids)
            if superadmin
            else await admin_allowed(message.from_user.id)
        )
        if not allowed:
            await message.answer("Bu amal uchun ruxsat yo‘q.")
        return allowed

    @router.message(Command("provider"))
    async def provider_card(message: Message) -> None:
        if message.from_user is None or not await admin_allowed(message.from_user.id):
            return
        async with session_factory() as session:
            provider = await session.scalar(select(Provider).where(Provider.code == "MYXVEST"))
            if provider is None:
                await message.answer("Provider sozlanmagan.")
                return
            balance = await session.scalar(
                select(ProviderBalanceSnapshot.balance_som)
                .where(ProviderBalanceSnapshot.provider_id == provider.id)
                .order_by(ProviderBalanceSnapshot.fetched_at.desc())
                .limit(1)
            )
            waiting = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.internal_status.in_(
                        [
                            OrderStatus.AWAITING_PROVIDER_FUNDING,
                            OrderStatus.INSUFFICIENT_PROVIDER_FUNDS,
                        ]
                    )
                )
            )
            processing = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(Order.internal_status == OrderStatus.PROCESSING)
            )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    inline_button(
                        text="🔄 Balansni qayta tekshirish",
                        callback_data="provider:sync",
                        style="primary",
                        emoji_key="sync",
                    )
                ],
                [
                    inline_button(
                        text="▶️ Navbatni ishga tushirish",
                        callback_data="provider:dispatch",
                        style="success",
                        emoji_key="enable",
                    )
                ],
            ]
        )
        await message.answer(
            f"Myxvest: {provider.status.value}\n"
            f"Balans: {balance if balance is not None else 'noma’lum'} so‘m\n"
            f"Funding navbati: {waiting or 0}\n"
            f"Bajarilmoqda: {processing or 0}\n"
            f"Oxirgi sync: {provider.last_balance_sync_at or 'hali bajarilmagan'}",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data == "provider:sync")
    async def provider_sync(callback: CallbackQuery) -> None:
        if callback.from_user is None or not await admin_allowed(callback.from_user.id):
            await callback.answer("Ruxsat yo‘q", show_alert=True)
            return
        await callback.answer("Tekshirilmoqda…")
        try:
            balance = await workflow.sync_balance()
            dispatched = await workflow.dispatch_pending()
            text = f"Real balans: {balance} so‘m. Yuborilgan orderlar: {dispatched}."
        except Exception:
            text = "Provider bilan aloqa vaqtincha muvaffaqiyatsiz. Texnik tafsilotlar yashirildi."
        if callback.message:
            await callback.message.answer(text)

    @router.callback_query(F.data == "provider:dispatch")
    async def provider_dispatch(callback: CallbackQuery) -> None:
        if callback.from_user is None or not await admin_allowed(callback.from_user.id):
            await callback.answer("Ruxsat yo‘q", show_alert=True)
            return
        count = await workflow.dispatch_pending()
        await callback.answer(f"Navbat tekshirildi: {count}", show_alert=True)

    @router.message(Command("order"))
    async def order_status(message: Message) -> None:
        if message.from_user is None or not message.text:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Foydalanish: /order BUYURTMA_RAQAMI")
            return
        async with session_factory() as session:
            order = await session.scalar(
                select(Order)
                .join(User, User.id == Order.user_id)
                .where(
                    Order.public_order_number == parts[1].strip().lstrip("#"),
                    User.telegram_id == message.from_user.id,
                )
            )
        if order is None:
            await message.answer("Buyurtma topilmadi.")
            return
        await message.answer(user_order_message(order.internal_status, order.public_order_number))

    @router.message(Command("provider_toggle"))
    async def provider_toggle(message: Message) -> None:
        if not await require_admin(message, superadmin=True) or not message.text:
            return
        parts = message.text.split()
        if len(parts) != 2 or parts[1].casefold() not in {"on", "off"}:
            await message.answer("Foydalanish: /provider_toggle on|off")
            return
        enabled = parts[1].casefold() == "on"
        async with session_factory.begin() as session:
            await set_provider_enabled(
                session, enabled=enabled, actor_telegram_id=message.from_user.id
            )
        await message.answer("Provider holati yangilandi.")

    @router.message(Command("service_toggle"))
    async def service_toggle(message: Message) -> None:
        if not await require_admin(message, superadmin=True) or not message.text:
            return
        parts = message.text.split()
        try:
            service_type = ServiceType(parts[1].upper())
            enabled = {"on": True, "off": False}[parts[2].casefold()]
        except (IndexError, KeyError, ValueError):
            await message.answer("Foydalanish: /service_toggle STARS|PREMIUM|GIFT on|off")
            return
        async with session_factory.begin() as session:
            changed = await set_service_type_enabled(
                session,
                service_type=service_type,
                enabled=enabled,
                actor_telegram_id=message.from_user.id,
            )
        await message.answer(f"Yangilangan xizmatlar: {changed}.")

    @router.message(Command("priority"))
    async def priority(message: Message) -> None:
        if not await require_admin(message) or not message.text:
            return
        parts = message.text.split()
        try:
            public_number, value = parts[1].lstrip("#"), int(parts[2])
            async with session_factory.begin() as session:
                await set_order_priority(
                    session,
                    public_number=public_number,
                    priority=value,
                    actor_telegram_id=message.from_user.id,
                )
        except (IndexError, ValueError, AdminActionError) as exc:
            await message.answer(f"Amal bajarilmadi: {exc}")
            return
        await message.answer("Order priority yangilandi va auditga yozildi.")

    @router.message(Command("reconcile"))
    async def reconcile(message: Message) -> None:
        if not await require_admin(message) or not message.text:
            return
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("Foydalanish: /reconcile ORDER_NUMBER")
            return
        async with session_factory() as session:
            order_id = await session.scalar(
                select(Order.id).where(Order.public_order_number == parts[1].lstrip("#"))
            )
        if order_id is None:
            await message.answer("Order topilmadi.")
            return
        try:
            await workflow.reconcile(order_id)
            await message.answer("Reconciliation yakunlandi; eski idempotency key saqlandi.")
        except Exception:
            await message.answer("Provider javobi noaniq; order review holatida qoldi.")

    @router.message(Command("price_approve"))
    async def price_approve(message: Message) -> None:
        if not await require_admin(message) or not message.text:
            return
        parts = message.text.split()
        try:
            async with session_factory.begin() as session:
                await approve_safe_price_changed_order(
                    session,
                    public_number=parts[1].lstrip("#"),
                    actor_telegram_id=message.from_user.id,
                )
        except (IndexError, AdminActionError) as exc:
            await message.answer(f"Tasdiqlanmadi: {exc}")
            return
        await message.answer("Yangi tannarx xavfsiz; order funding navbatiga qaytarildi.")

    @router.message(Command("refund"))
    async def refund(message: Message) -> None:
        if not await require_admin(message, superadmin=True) or not message.text:
            return
        parts = message.text.split()
        try:
            async with session_factory.begin() as session:
                await manual_refund(
                    session,
                    public_number=parts[1].lstrip("#"),
                    actor_telegram_id=message.from_user.id,
                )
        except (IndexError, ValueError) as exc:
            await message.answer(f"Refund bajarilmadi: {exc}")
            return
        await message.answer("Refund ledger va audit bilan bajarildi.")

    @router.message(Command("audit"))
    async def audit(message: Message) -> None:
        if not await require_admin(message) or not message.text:
            return
        parts = message.text.split()
        try:
            async with session_factory() as session:
                rows = await audit_history(session, public_number=parts[1].lstrip("#"), limit=10)
        except (IndexError, AdminActionError) as exc:
            await message.answer(f"Audit topilmadi: {exc}")
            return
        text = "\n".join(f"{row.created_at}: {row.action}" for row in rows) or "Audit bo‘sh."
        await message.answer(text)

    return router


async def main() -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    if not settings.myxvest_enabled:
        raise RuntimeError("MYXVEST_ENABLED must be true to run provider bot handlers")
    client = MyxvestClient(
        base_url=settings.myxvest_base_url,
        api_key=settings.myxvest_api_key.get_secret_value(),
        timeout_seconds=settings.myxvest_timeout_seconds,
        max_retries=settings.myxvest_max_retries,
    )
    workflow = ProviderWorkflow(
        session_factory,
        client,
        purchase_enabled=settings.myxvest_purchase_enabled,
        runtime_gate_required=True,
    )
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dispatcher = Dispatcher()
    identity = IdentityMiddleware()
    dispatcher.message.middleware(identity)
    dispatcher.callback_query.middleware(identity)
    async with session_factory() as session:
        await load_button_design_cache(session)
    dispatcher.include_router(build_menu_router())
    dispatcher.include_router(build_button_design_router())
    dispatcher.include_router(build_payment_router())
    dispatcher.include_router(build_pricing_router())
    dispatcher.include_router(build_admin_management_router())
    dispatcher.include_router(build_production_router(workflow))
    dispatcher.include_router(build_router(workflow))
    try:
        await dispatcher.start_polling(bot)
    finally:
        await client.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
