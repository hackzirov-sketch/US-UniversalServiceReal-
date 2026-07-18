from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.buttons import inline_button
from app.core.config import get_settings
from app.db.enums import ServiceType
from app.db.models import ManualProviderPrice
from app.db.session import session_factory
from app.services.manual_pricing import (
    ManualPriceInput,
    ManualPricingError,
    approve_manual_price,
    calculate_sale_from_original,
    create_manual_price,
    deactivate_manual_price,
    grant_manage_pricing,
    list_active_manual_prices,
    preview_price,
    pricing_actor,
    quick_adjust_price,
)


class PricingStates(StatesGroup):
    provider_cost = State()
    sale_price = State()
    stars_min = State()
    stars_max = State()
    gift_name = State()
    gift_display_name = State()
    gift_comment = State()
    duration = State()
    source_note = State()
    confirm = State()
    edit_value = State()
    quick_confirm = State()


def pricing_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="⭐ Stars",
                    callback_data="pricing:new:stars",
                    style="success",
                    emoji_key="stars",
                ),
                inline_button(
                    text="💎 Premium",
                    callback_data="pricing:premium",
                    style="success",
                    emoji_key="premium",
                ),
            ],
            [
                inline_button(
                    text="🎁 Gifts",
                    callback_data="pricing:new:gift",
                    style="success",
                    emoji_key="gift",
                ),
                inline_button(
                    text="📋 Faol narxlar",
                    callback_data="pricing:list",
                    style="primary",
                    emoji_key="active_prices",
                ),
            ],
            [
                inline_button(
                    text="📋 Barcha faol narxlar",
                    callback_data="pricing:list",
                    style="primary",
                    emoji_key="active_prices",
                )
            ],
            [
                inline_button(
                    text="🕓 Narxlar tarixi",
                    callback_data="pricing:history",
                    emoji_key="price_history",
                )
            ],
            [
                inline_button(
                    text="⚠️ Narxsiz xizmatlar",
                    callback_data="pricing:expiring",
                    style="danger",
                    emoji_key="unpriced_services",
                )
            ],
            [
                inline_button(text="◀️ Orqaga", callback_data="nav:home", emoji_key="back"),
                inline_button(text="🏠 Bosh menyu", callback_data="nav:home", emoji_key="home"),
            ],
        ]
    )


async def _actor(telegram_id: int):
    settings = get_settings()
    async with session_factory() as session:
        return await pricing_actor(
            session, telegram_id=telegram_id, superadmin_ids=settings.superadmin_ids
        )


async def _allowed(telegram_id: int) -> bool:
    return (await _actor(telegram_id)).can_manage_pricing


def build_pricing_router() -> Router:
    router = Router(name="manual_pricing")

    @router.message(Command("pricing"))
    async def pricing_command(message: Message) -> None:
        if message.from_user is None or not await _allowed(message.from_user.id):
            await message.answer("Narxlarni boshqarish uchun MANAGE_PRICING huquqi kerak.")
            return
        await message.answer("💰 Narxlar boshqaruvi", reply_markup=pricing_menu())

    @router.callback_query(F.data == "pricing:new:stars")
    async def new_stars(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _callback_allowed(callback):
            return
        await state.clear()
        await state.update_data(service_type=ServiceType.STARS.value, display_name="Telegram Stars")
        await state.set_state(PricingStates.provider_cost)
        await _answer(callback, "1 Stars uchun Original narxni so‘mda kiriting:")

    @router.callback_query(F.data == "pricing:premium")
    async def premium_packages(callback: CallbackQuery) -> None:
        if not await _callback_allowed(callback):
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    inline_button(
                        text=f"💎 {months} oy",
                        callback_data=f"pricing:new:premium:{months}",
                        style="success",
                        emoji_key="premium",
                    )
                ]
                for months in (3, 6, 12)
            ]
        )
        await _answer(callback, "Premium paketini tanlang:", keyboard)

    @router.callback_query(F.data.startswith("pricing:new:premium:"))
    async def new_premium(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _callback_allowed(callback):
            return
        months = int(callback.data.rsplit(":", 1)[1])
        await state.clear()
        await state.update_data(
            service_type=ServiceType.PREMIUM.value,
            premium_months=months,
            display_name=f"Telegram Premium — {months} oy",
        )
        await state.set_state(PricingStates.provider_cost)
        await _answer(callback, "Paket Original narxini so‘mda kiriting:")

    @router.callback_query(F.data == "pricing:new:gift")
    async def new_gift(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _callback_allowed(callback):
            return
        await state.clear()
        await state.update_data(service_type=ServiceType.GIFT.value)
        await state.set_state(PricingStates.gift_name)
        await _answer(callback, "Myxvest’ga yuboriladigan aniq gift_name ni kiriting:")

    @router.message(PricingStates.gift_name)
    async def gift_name(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if not value:
            await message.answer("gift_name bo‘sh bo‘lmasin.")
            return
        await state.update_data(gift_name=value)
        await state.set_state(PricingStates.gift_display_name)
        await message.answer("Userga ko‘rinadigan Gift nomini kiriting:")

    @router.message(PricingStates.gift_display_name)
    async def gift_display(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if not value:
            await message.answer("Ko‘rinadigan nom bo‘sh bo‘lmasin.")
            return
        await state.update_data(display_name=value)
        await state.set_state(PricingStates.provider_cost)
        await message.answer("Gift Original narxini so‘mda kiriting:")

    @router.message(PricingStates.provider_cost)
    async def provider_cost(message: Message, state: FSMContext) -> None:
        value = _positive_int(message.text)
        if value is None:
            await message.answer("Musbat integer so‘m kiriting.")
            return
        await state.update_data(provider_cost_som=value)
        await state.set_state(PricingStates.sale_price)
        await message.answer("Bizdagi narxni so‘mda kiriting:")

    @router.message(PricingStates.sale_price)
    async def sale_price(message: Message, state: FSMContext) -> None:
        value = _positive_int(message.text)
        if value is None:
            await message.answer("Musbat integer so‘m kiriting.")
            return
        data = await state.get_data()
        if value < data["provider_cost_som"]:
            await message.answer("MANUAL_PRICE_BELOW_COST: sotuv narxi tannarxdan past.")
            return
        await state.update_data(sale_price_som=value)
        service_type = ServiceType(data["service_type"])
        if service_type == ServiceType.STARS:
            await state.set_state(PricingStates.stars_min)
            await message.answer("Minimal Stars miqdorini kiriting (kamida 50):")
        elif service_type == ServiceType.GIFT:
            await state.set_state(PricingStates.gift_comment)
            await message.answer("Gift comment qabul qiladimi? ha/yo‘q")
        else:
            await _ask_duration(message, state)

    @router.message(PricingStates.stars_min)
    async def stars_min(message: Message, state: FSMContext) -> None:
        value = _positive_int(message.text)
        if value is None or value < 50:
            await message.answer("Minimum kamida 50 bo‘lsin.")
            return
        await state.update_data(min_quantity=value)
        await state.set_state(PricingStates.stars_max)
        await message.answer("Maksimal Stars miqdorini kiriting (ko‘pi bilan 10000):")

    @router.message(PricingStates.stars_max)
    async def stars_max(message: Message, state: FSMContext) -> None:
        value = _positive_int(message.text)
        data = await state.get_data()
        if value is None or value > 10_000 or value < data["min_quantity"]:
            await message.answer("Max 10000 dan oshmasin va minimumdan kichik bo‘lmasin.")
            return
        await state.update_data(max_quantity=value)
        await _ask_duration(message, state)

    @router.message(PricingStates.gift_comment)
    async def gift_comment(message: Message, state: FSMContext) -> None:
        answer = (message.text or "").strip().casefold()
        if answer not in {"ha", "yo‘q", "yoq", "yes", "no"}:
            await message.answer("ha yoki yo‘q deb javob bering.")
            return
        await state.update_data(allow_comment=answer in {"ha", "yes"})
        await _ask_duration(message, state)

    @router.message(PricingStates.duration)
    async def duration(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip().casefold()
        duration_value = None if text in {"0", "muddatsiz"} else _positive_int(text)
        if duration_value not in {1, 3, 6, 12, 24, None}:
            await message.answer("1, 3, 6, 12, 24 yoki muddatsiz kiriting.")
            return
        await state.update_data(duration_hours=duration_value)
        await state.set_state(PricingStates.source_note)
        await message.answer("Source note/sabab kiriting (`-` bo‘lsa bo‘sh):")

    @router.message(PricingStates.source_note)
    async def source_note(message: Message, state: FSMContext) -> None:
        note = (message.text or "").strip()
        await state.update_data(source_note=None if note == "-" else note)
        data = await state.get_data()
        price_input = _input_from_state(data)
        settings = get_settings()
        try:
            preview = preview_price(
                price_input,
                min_profit_percent=settings.min_profit_percent,
                min_profit_som=settings.min_profit_som,
            )
        except ManualPricingError as exc:
            await message.answer(f"Narx yaroqsiz: {exc}")
            await state.clear()
            return
        await state.set_state(PricingStates.confirm)
        warning = (
            "\n⚠️ Minimal foydadan past — superadmin tasdig‘i kerak." if preview.low_profit else ""
        )
        stars_profit = (
            f"\n100 Stars foyda: {preview.profit_som * 100:,} so‘m"
            if price_input.service_type == ServiceType.STARS
            else ""
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    inline_button(
                        text="✅ Saqlash",
                        callback_data="pricing:save",
                        style="success",
                        emoji_key="confirm",
                    ),
                    inline_button(
                        text="❌ Bekor qilish",
                        callback_data="pricing:cancel",
                        style="danger",
                        emoji_key="cancel",
                    ),
                ]
            ]
        )
        await message.answer(
            f"💰 {price_input.display_name}\n\n"
            f"Original narx: {preview.provider_cost_som:,} so‘m\n"
            f"Bizdagi narx: {preview.sale_price_som:,} so‘m\n"
            f"Foyda: {preview.profit_som:,} so‘m{stars_profit}\n"
            f"Foyda foizi: {preview.profit_percent_text}\n"
            f"Amal qiladi: {price_input.duration_hours or 'muddatsiz'} soat"
            f"{warning}",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data == "pricing:save", PricingStates.confirm)
    async def save_price(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            return
        data = await state.get_data()
        settings = get_settings()
        try:
            async with session_factory.begin() as session:
                actor = await pricing_actor(
                    session,
                    telegram_id=callback.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                record = await create_manual_price(
                    session,
                    data=_input_from_state(data),
                    actor=actor,
                    requires_superadmin_approval=settings.pricing_requires_superadmin_approval,
                    min_profit_percent=settings.min_profit_percent,
                    min_profit_som=settings.min_profit_som,
                )
            await state.clear()
            await _answer(
                callback,
                f"Narx v{record.version} saqlandi. Holat: {record.status.value}.",
                pricing_menu(),
            )
            for superadmin_id in settings.superadmin_ids - {callback.from_user.id}:
                await callback.bot.send_message(
                    superadmin_id,
                    f"⚠️ Admin narxni o‘zgartirdi: {record.service_key}, v{record.version}, "
                    f"holat {record.status.value}.",
                )
        except ManualPricingError as exc:
            await callback.answer(f"Saqlanmadi: {exc}", show_alert=True)

    @router.callback_query(F.data == "pricing:cancel")
    async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _answer(callback, "Bekor qilindi.", pricing_menu())

    @router.callback_query(F.data == "pricing:list")
    async def list_prices(callback: CallbackQuery) -> None:
        if not await _callback_allowed(callback):
            return
        async with session_factory() as session:
            prices = await list_active_manual_prices(session)
        text = "\n".join(_price_line(price) for price in prices) or "Faol manual narx yo‘q."
        rows = [
            [
                inline_button(
                    text=f"✏️ {price.display_name}",
                    callback_data=f"pricing:view:{price.id}",
                    style="primary",
                    emoji_key="edit",
                )
            ]
            for price in prices
        ]
        rows.append(
            [inline_button(text="◀️ Orqaga", callback_data="admin:pricing", emoji_key="back")]
        )
        await _answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data.startswith("pricing:view:"))
    async def view_price(callback: CallbackQuery) -> None:
        if not await _callback_allowed(callback):
            return
        price_id = (callback.data or "").rsplit(":", 1)[1]
        async with session_factory() as session:
            price = await session.get(ManualProviderPrice, price_id)
        if price is None or not price.active:
            await callback.answer("Narx eskirgan yoki inactive", show_alert=True)
            return
        await _answer(callback, _price_card(price), _price_editor_keyboard(price))

    @router.callback_query(F.data.startswith("pricing:q:"))
    async def quick_adjust(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _callback_allowed(callback):
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4:
            await callback.answer("Eskirgan tugma", show_alert=True)
            return
        operation, price_id = parts[2], parts[3]
        settings = get_settings()
        async with session_factory() as session:
            price = await session.get(ManualProviderPrice, price_id)
        if price is None or not price.active:
            await callback.answer("Narx eskirgan", show_alert=True)
            return
        original = price.provider_cost_som
        sale = price.sale_price_som
        unit = 1 if price.service_type == ServiceType.STARS else 1_000
        if operation == "p1":
            sale = quick_adjust_price(sale, delta_som=unit)
        elif operation == "p5":
            sale = quick_adjust_price(sale, delta_som=unit * 5)
        elif operation == "p10":
            sale = quick_adjust_price(sale, delta_som=unit * 10)
        elif operation == "m1":
            sale = quick_adjust_price(sale, delta_som=-unit)
        elif operation == "m5":
            sale = quick_adjust_price(sale, delta_som=-unit * 5)
        elif operation == "pct5":
            sale = quick_adjust_price(sale, percent=Decimal("5"))
        elif operation == "pct10":
            sale = quick_adjust_price(sale, percent=Decimal("10"))
        elif operation == "calc":
            sale = calculate_sale_from_original(
                original,
                minimum_profit_per_unit_som=settings.min_profit_som,
                percentage_markup=settings.min_profit_percent,
            )
        else:
            await callback.answer("Noma’lum adjustment", show_alert=True)
            return
        await _prepare_adjustment_preview(callback, state, price, original, sale)

    @router.callback_query(F.data.startswith("pricing:edit:"))
    async def edit_price(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _callback_allowed(callback):
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 4 or parts[2] not in {"original", "sale"}:
            await callback.answer("Eskirgan tugma", show_alert=True)
            return
        async with session_factory() as session:
            price = await session.get(ManualProviderPrice, parts[3])
        if price is None or not price.active:
            await callback.answer("Narx eskirgan", show_alert=True)
            return
        await state.set_state(PricingStates.edit_value)
        await state.update_data(
            edit_field=parts[2], edit_price_id=price.id, edit_version=price.version
        )
        await _answer(
            callback,
            f"Yangi {'Original' if parts[2] == 'original' else 'Bizdagi'} narxni kiriting:",
        )

    @router.message(PricingStates.edit_value)
    async def edit_value(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not await _allowed(message.from_user.id):
            return
        value = _positive_int(message.text)
        if value is None:
            await message.answer("Musbat integer UZS kiriting.")
            return
        data = await state.get_data()
        async with session_factory() as session:
            price = await session.get(ManualProviderPrice, data.get("edit_price_id"))
        if price is None or price.version != data.get("edit_version") or not price.active:
            await state.clear()
            await message.answer("Narx o‘zgargan. Sahifani qayta oching.")
            return
        original = value if data["edit_field"] == "original" else price.provider_cost_som
        sale = value if data["edit_field"] == "sale" else price.sale_price_som
        if sale < original:
            await message.answer("Bizdagi narx Original narxdan past bo‘lishi mumkin emas.")
            return
        await _prepare_adjustment_preview_message(message, state, price, original, sale)

    @router.callback_query(F.data == "pricing:adjust:save", PricingStates.quick_confirm)
    async def adjustment_save(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _callback_allowed(callback):
            return
        data = await state.get_data()
        settings = get_settings()
        try:
            async with session_factory.begin() as session:
                original = await session.get(
                    ManualProviderPrice, data["base_price_id"], with_for_update=True
                )
                if (
                    original is None
                    or original.version != data["base_version"]
                    or not original.active
                ):
                    raise ManualPricingError("Narx preview eskirgan")
                actor = await pricing_actor(
                    session,
                    telegram_id=callback.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                record = await create_manual_price(
                    session,
                    data=_input_from_price(original, data["new_original"], data["new_sale"]),
                    actor=actor,
                    requires_superadmin_approval=settings.pricing_requires_superadmin_approval,
                    min_profit_percent=settings.min_profit_percent,
                    min_profit_som=settings.min_profit_som,
                )
            await state.clear()
            await _answer(callback, f"✅ Narx v{record.version} saqlandi.", pricing_menu())
        except ManualPricingError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("pricing:off:"))
    async def deactivate_price_callback(callback: CallbackQuery) -> None:
        if not await _callback_allowed(callback):
            return
        price_id = (callback.data or "").rsplit(":", 1)[1]
        settings = get_settings()
        try:
            async with session_factory.begin() as session:
                actor = await pricing_actor(
                    session,
                    telegram_id=callback.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                await deactivate_manual_price(session, price_id=price_id, actor=actor)
            await _answer(callback, "⏸ Xizmat inactive qilindi; tarix saqlandi.", pricing_menu())
        except ManualPricingError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.callback_query(F.data == "pricing:history")
    async def history(callback: CallbackQuery) -> None:
        if not await _callback_allowed(callback):
            return
        async with session_factory() as session:
            prices = list(
                await session.scalars(
                    select(ManualProviderPrice)
                    .order_by(ManualProviderPrice.created_at.desc())
                    .limit(20)
                )
            )
        text = "\n".join(_price_line(price) for price in prices) or "Tarix bo‘sh."
        await _answer(callback, text, pricing_menu())

    @router.callback_query(F.data == "pricing:expiring")
    async def expiring(callback: CallbackQuery) -> None:
        if not await _callback_allowed(callback):
            return
        now = datetime.now(UTC)
        async with session_factory() as session:
            prices = list(
                await session.scalars(
                    select(ManualProviderPrice).where(
                        ManualProviderPrice.active.is_(True),
                        ManualProviderPrice.valid_until.is_not(None),
                        ManualProviderPrice.valid_until <= now + timedelta(hours=1),
                    )
                )
            )
        text = "\n".join(_price_line(price) for price in prices) or "1 soatda tugaydigan narx yo‘q."
        await _answer(callback, text, pricing_menu())

    @router.message(Command("price_inactive"))
    async def price_inactive(message: Message) -> None:
        if message.from_user is None or not message.text:
            return
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("Foydalanish: /price_inactive PRICE_ID")
            return
        settings = get_settings()
        try:
            async with session_factory.begin() as session:
                actor = await pricing_actor(
                    session,
                    telegram_id=message.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                await deactivate_manual_price(session, price_id=parts[1], actor=actor)
            await message.answer("Narx inactive qilindi; tarix o‘chirilmagan.")
        except ManualPricingError as exc:
            await message.answer(f"Amal bajarilmadi: {exc}")

    @router.message(Command("pricing_grant"))
    async def pricing_grant(message: Message) -> None:
        if message.from_user is None or not message.text:
            return
        parts = message.text.split()
        try:
            target_id = int(parts[1])
            settings = get_settings()
            async with session_factory.begin() as session:
                actor = await pricing_actor(
                    session,
                    telegram_id=message.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                await grant_manage_pricing(session, target_telegram_id=target_id, actor=actor)
            await message.answer("MANAGE_PRICING huquqi berildi.")
        except (IndexError, ValueError, ManualPricingError) as exc:
            await message.answer(f"Foydalanish: /pricing_grant TELEGRAM_ID\n{exc}")

    @router.message(Command("manual_price_approve"))
    async def manual_price_approve(message: Message) -> None:
        if message.from_user is None or not message.text:
            return
        parts = message.text.split()
        try:
            settings = get_settings()
            async with session_factory.begin() as session:
                actor = await pricing_actor(
                    session,
                    telegram_id=message.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                record = await approve_manual_price(session, price_id=parts[1], actor=actor)
            await message.answer(f"Narx tasdiqlandi: {record.service_key} v{record.version}.")
        except (IndexError, ManualPricingError) as exc:
            await message.answer(f"Foydalanish: /manual_price_approve PRICE_ID\n{exc}")

    @router.message(Command("price_copy"))
    async def price_copy(message: Message) -> None:
        if message.from_user is None or not message.text:
            return
        parts = message.text.split()
        try:
            settings = get_settings()
            async with session_factory.begin() as session:
                actor = await pricing_actor(
                    session,
                    telegram_id=message.from_user.id,
                    superadmin_ids=settings.superadmin_ids,
                )
                original = await session.get(ManualProviderPrice, parts[1])
                if original is None:
                    raise ManualPricingError("Price not found")
                record = await create_manual_price(
                    session,
                    data=ManualPriceInput(
                        service_type=original.service_type,
                        provider_cost_som=original.provider_cost_som,
                        sale_price_som=original.sale_price_som,
                        display_name=original.display_name,
                        min_quantity=original.min_quantity,
                        max_quantity=original.max_quantity,
                        premium_months=original.premium_months,
                        gift_name=original.gift_name,
                        allow_comment=original.allow_comment,
                        sort_order=original.sort_order,
                        duration_hours=24,
                        source_note=f"Copied from {original.id}",
                    ),
                    actor=actor,
                    requires_superadmin_approval=settings.pricing_requires_superadmin_approval,
                    min_profit_percent=settings.min_profit_percent,
                    min_profit_som=settings.min_profit_som,
                )
            await message.answer(f"Narx nusxalandi: v{record.version}.")
        except (IndexError, ManualPricingError) as exc:
            await message.answer(f"Foydalanish: /price_copy PRICE_ID\n{exc}")

    return router


async def _callback_allowed(callback: CallbackQuery) -> bool:
    if callback.from_user is None or not await _allowed(callback.from_user.id):
        await callback.answer("MANAGE_PRICING huquqi kerak", show_alert=True)
        return False
    return True


async def _answer(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)


async def _ask_duration(message: Message, state: FSMContext) -> None:
    await state.set_state(PricingStates.duration)
    await message.answer("Amal qilish muddati: 1, 3, 6, 12, 24 yoki muddatsiz (default 24):")


def _positive_int(value: str | None) -> int | None:
    text = (value or "").strip().replace(" ", "")
    return int(text) if text.isdecimal() and int(text) > 0 else None


def _input_from_state(data: dict) -> ManualPriceInput:
    return ManualPriceInput(
        service_type=ServiceType(data["service_type"]),
        provider_cost_som=data["provider_cost_som"],
        sale_price_som=data["sale_price_som"],
        display_name=data["display_name"],
        min_quantity=data.get("min_quantity"),
        max_quantity=data.get("max_quantity"),
        premium_months=data.get("premium_months"),
        gift_name=data.get("gift_name"),
        allow_comment=data.get("allow_comment", False),
        sort_order=data.get("sort_order", 0),
        duration_hours=data.get("duration_hours", 24),
        source_note=data.get("source_note"),
    )


def _price_line(price: ManualProviderPrice) -> str:
    until = price.valid_until.isoformat(timespec="minutes") if price.valid_until else "muddatsiz"
    return (
        f"{price.display_name} | Original {price.provider_cost_som:,} | "
        f"Bizdagi {price.sale_price_som:,} so‘m | "
        f"v{price.version} | {price.status.value} | {until} | `{price.id}`"
    )


def _price_card(price: ManualProviderPrice) -> str:
    profit = price.sale_price_som - price.provider_cost_som
    margin_bps = profit * 10_000 // price.provider_cost_som
    unit = " / 1 Stars" if price.service_type == ServiceType.STARS else ""
    hundred = (
        f"\n100 Stars foydasi: {profit * 100:,} so‘m"
        if price.service_type == ServiceType.STARS
        else ""
    )
    return (
        f"{price.display_name}\n\n"
        f"Original narx: {price.provider_cost_som:,} so‘m{unit}\n"
        f"Bizdagi narx: {price.sale_price_som:,} so‘m{unit}\n"
        f"Foyda: {profit:,} so‘m{unit}{hundred}\n"
        f"Marja: {margin_bps // 100}.{margin_bps % 100:02d}%\n"
        f"Min: {price.min_quantity or '-'}\nMax: {price.max_quantity or '-'}\n"
        f"Holat: {'🟢 Faol' if price.active else '⏸ Inactive'}"
    )


def _price_editor_keyboard(price: ManualProviderPrice) -> InlineKeyboardMarkup:
    price_id = price.id
    unit = "1" if price.service_type == ServiceType.STARS else "1 000"
    rows = [
        [
            inline_button(
                text="✏️ Original narx",
                callback_data=f"pricing:edit:original:{price_id}",
                style="primary",
                emoji_key="edit",
            ),
            inline_button(
                text="✏️ Bizdagi narx",
                callback_data=f"pricing:edit:sale:{price_id}",
                style="primary",
                emoji_key="edit",
            ),
        ],
        [
            inline_button(
                text=f"➕ {unit}",
                callback_data=f"pricing:q:p1:{price_id}",
                style="success",
                emoji_key="quick_adjust",
            ),
            inline_button(
                text=f"➕ {'5' if price.service_type == ServiceType.STARS else '5 000'}",
                callback_data=f"pricing:q:p5:{price_id}",
                style="success",
                emoji_key="quick_adjust",
            ),
        ],
        [
            inline_button(
                text=f"➕ {'10' if price.service_type == ServiceType.STARS else '10 000'}",
                callback_data=f"pricing:q:p10:{price_id}",
                style="success",
                emoji_key="quick_adjust",
            ),
            inline_button(
                text=f"➖ {unit}",
                callback_data=f"pricing:q:m1:{price_id}",
                style="danger",
                emoji_key="quick_adjust",
            ),
        ],
        [
            inline_button(
                text="📈 +5%",
                callback_data=f"pricing:q:pct5:{price_id}",
                style="success",
                emoji_key="quick_adjust",
            ),
            inline_button(
                text="📈 +10%",
                callback_data=f"pricing:q:pct10:{price_id}",
                style="success",
                emoji_key="quick_adjust",
            ),
        ],
        [
            inline_button(
                text="♻️ Originaldan hisoblash",
                callback_data=f"pricing:q:calc:{price_id}",
                style="primary",
                emoji_key="quick_adjust",
            )
        ],
        [
            inline_button(
                text="⏸ Xizmatni yopish",
                callback_data=f"pricing:off:{price_id}",
                style="danger",
                emoji_key="deactivate",
            )
        ],
        [inline_button(text="◀️ Orqaga", callback_data="pricing:list", emoji_key="back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _prepare_adjustment_preview(
    callback: CallbackQuery,
    state: FSMContext,
    price: ManualProviderPrice,
    original: int,
    sale: int,
) -> None:
    try:
        preview = preview_price(
            _input_from_price(price, original, sale),
            min_profit_percent=get_settings().min_profit_percent,
            min_profit_som=get_settings().min_profit_som,
        )
    except ManualPricingError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _store_adjustment(state, price, original, sale)
    await _answer(callback, _adjustment_text(price, preview), _adjustment_keyboard())


async def _prepare_adjustment_preview_message(
    message: Message,
    state: FSMContext,
    price: ManualProviderPrice,
    original: int,
    sale: int,
) -> None:
    preview = preview_price(
        _input_from_price(price, original, sale),
        min_profit_percent=get_settings().min_profit_percent,
        min_profit_som=get_settings().min_profit_som,
    )
    await _store_adjustment(state, price, original, sale)
    await message.answer(_adjustment_text(price, preview), reply_markup=_adjustment_keyboard())


async def _store_adjustment(
    state: FSMContext, price: ManualProviderPrice, original: int, sale: int
) -> None:
    await state.set_state(PricingStates.quick_confirm)
    await state.update_data(
        base_price_id=price.id,
        base_version=price.version,
        new_original=original,
        new_sale=sale,
    )


def _adjustment_text(price: ManualProviderPrice, preview) -> str:
    warning = (
        "\n⚠️ Minimal foydadan past — faqat superadmin override qilishi mumkin."
        if preview.low_profit
        else ""
    )
    hundred = (
        f"\n100 dona foydasi: {preview.profit_som * 100:,} so‘m"
        if price.service_type == ServiceType.STARS
        else ""
    )
    return (
        "💰 Narxni tasdiqlash\n\n"
        f"Xizmat: {price.display_name}\n"
        f"Original narx: {preview.provider_cost_som:,} so‘m\n"
        f"Bizdagi narx: {preview.sale_price_som:,} so‘m\n"
        f"Birlik foydasi: {preview.profit_som:,} so‘m{hundred}\n"
        f"Marja: {preview.profit_percent_text}\n"
        "Manba: MANUAL"
        f"{warning}"
    )


def _adjustment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="✅ Faollashtirish",
                    callback_data="pricing:adjust:save",
                    style="success",
                    emoji_key="enable",
                )
            ],
            [
                inline_button(
                    text="✏️ Tahrirlash",
                    callback_data="pricing:list",
                    style="primary",
                    emoji_key="edit",
                )
            ],
            [
                inline_button(
                    text="❌ Bekor qilish",
                    callback_data="pricing:cancel",
                    style="danger",
                    emoji_key="cancel",
                )
            ],
        ]
    )


def _input_from_price(price: ManualProviderPrice, original: int, sale: int) -> ManualPriceInput:
    return ManualPriceInput(
        service_type=price.service_type,
        provider_cost_som=original,
        sale_price_som=sale,
        display_name=price.display_name,
        min_quantity=price.min_quantity,
        max_quantity=price.max_quantity,
        premium_months=price.premium_months,
        gift_name=price.gift_name,
        allow_comment=price.allow_comment,
        sort_order=price.sort_order,
        active=True,
        duration_hours=24,
        source_note=f"Quick adjustment from v{price.version}",
    )
