from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from redis.asyncio import Redis

from app.bot.buttons import inline_button
from app.core.config import get_settings
from app.db.session import session_factory
from app.services.preflight import (
    SalesGateError,
    confirmation_code,
    run_preflight,
    runtime_sales_enabled,
    set_runtime_sales,
)


class SalesStates(StatesGroup):
    enable_code = State()


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
                    text="📜 Savdo auditlari",
                    callback_data="admin:audit",
                    emoji_key="audit",
                )
            ],
            [inline_button(text="◀️ Orqaga", callback_data="nav:home", emoji_key="back")],
        ]
    )


def build_production_router() -> Router:
    router = Router(name="direct_sales")

    async def allowed(telegram_id: int) -> bool:
        return telegram_id in get_settings().superadmin_ids

    async def report(callback: CallbackQuery):
        settings = get_settings()
        redis_ok = False
        client = Redis.from_url(settings.redis_url)
        try:
            redis_ok = bool(await client.ping())
        except Exception:
            redis_ok = False
        finally:
            await client.aclose()
        try:
            await callback.bot.get_me()
            telegram_ok = True
        except Exception:
            telegram_ok = False
        async with session_factory.begin() as session:
            return await run_preflight(
                session,
                settings=settings,
                actor_telegram_id=callback.from_user.id,
                infrastructure={"redis": redis_ok, "telegram": telegram_ok},
            )

    @router.callback_query(F.data == "sales:home")
    async def home(callback: CallbackQuery) -> None:
        if not await allowed(callback.from_user.id):
            await callback.answer("Faqat superadmin uchun", show_alert=True)
            return
        async with session_factory() as session:
            enabled = await runtime_sales_enabled(session)
        await _answer(
            callback,
            f"To‘g‘ridan-to‘g‘ri savdo: {'yoqilgan' if enabled else 'o‘chirilgan'}",
            sales_menu(),
        )

    @router.callback_query(F.data == "sales:preflight")
    async def preflight(callback: CallbackQuery) -> None:
        if not await allowed(callback.from_user.id):
            await callback.answer("Faqat superadmin uchun", show_alert=True)
            return
        result = await report(callback)
        await _answer(callback, _report_text(result.checks), sales_menu())

    @router.callback_query(F.data == "sales:enable")
    async def enable_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await allowed(callback.from_user.id):
            await callback.answer("Faqat superadmin uchun", show_alert=True)
            return
        result = await report(callback)
        if not result.success:
            await _answer(callback, _report_text(result.checks), sales_menu())
            return
        code = confirmation_code()
        await state.set_state(SalesStates.enable_code)
        await state.update_data(enable_code=code)
        await _answer(callback, f"Real savdoni yoqish uchun kodni yuboring: {code}")

    @router.message(SalesStates.enable_code)
    async def enable_confirm(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not await allowed(message.from_user.id):
            return
        data = await state.get_data()
        if (message.text or "").strip() != data.get("enable_code"):
            await message.answer("Tasdiq kodi noto‘g‘ri.")
            return
        settings = get_settings()
        try:
            async with session_factory.begin() as session:
                await set_runtime_sales(
                    session,
                    enabled=True,
                    actor_telegram_id=message.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                    environment_enabled=settings.direct_sales_enabled,
                )
        except SalesGateError as exc:
            await message.answer(f"Real savdo yoqilmadi: {exc}", reply_markup=sales_menu())
            return
        await state.clear()
        await message.answer("✅ To‘g‘ridan-to‘g‘ri savdo yoqildi.", reply_markup=sales_menu())

    @router.callback_query(F.data == "sales:disable")
    async def disable(callback: CallbackQuery, state: FSMContext) -> None:
        if not await allowed(callback.from_user.id):
            await callback.answer("Faqat superadmin uchun", show_alert=True)
            return
        settings = get_settings()
        async with session_factory.begin() as session:
            await set_runtime_sales(
                session,
                enabled=False,
                actor_telegram_id=callback.from_user.id,
                superadmin_ids=settings.superadmin_ids,
                environment_enabled=settings.direct_sales_enabled,
            )
        await state.clear()
        await _answer(callback, "⏸ Real savdo o‘chirildi.", sales_menu())

    return router


def _report_text(checks: dict[str, dict[str, object]]) -> str:
    lines = ["Savdo tayyorgarligi:"]
    for name, result in checks.items():
        lines.append(f"{'✅' if result['ok'] else '❌'} {name}: {result['detail']}")
    return "\n".join(lines)


async def _answer(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()
