import asyncio

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.bot.admin_management import build_admin_management_router
from app.bot.button_design_admin import build_button_design_router
from app.bot.menu import build_menu_router
from app.bot.messages import user_order_message
from app.bot.middleware import IdentityMiddleware
from app.bot.payments import build_payment_router
from app.bot.pricing import build_pricing_router
from app.bot.production import build_production_router
from app.core.config import get_settings
from app.core.security import is_superadmin
from app.db.enums import ServiceType
from app.db.models import Order, User
from app.db.session import session_factory
from app.services.admin import (
    AdminActionError,
    audit_history,
    manual_refund,
    set_order_priority,
    set_service_type_enabled,
)
from app.services.bootstrap import has_admin_access
from app.services.button_design import load_button_design_cache


def build_operational_router() -> Router:
    """Admin utilities that don't depend on an external fulfillment provider."""

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
        await message.answer("Buyurtma ustuvorligi yangilandi.")

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
                rows = await audit_history(
                    session, public_number=parts[1].lstrip("#"), limit=10
                )
        except (IndexError, AdminActionError) as exc:
            await message.answer(f"Audit topilmadi: {exc}")
            return
        text = "\n".join(f"{row.created_at}: {row.action}" for row in rows) or "Audit bo‘sh."
        await message.answer(text)

    return router


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    identity = IdentityMiddleware()
    dispatcher.message.middleware(identity)
    dispatcher.callback_query.middleware(identity)
    dispatcher.include_router(build_menu_router())
    dispatcher.include_router(build_button_design_router())
    dispatcher.include_router(build_payment_router())
    dispatcher.include_router(build_pricing_router())
    dispatcher.include_router(build_admin_management_router())
    dispatcher.include_router(build_operational_router())
    dispatcher.include_router(build_production_router())
    return dispatcher


async def main() -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dispatcher = build_dispatcher()
    async with session_factory() as session:
        await load_button_design_cache(session)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
