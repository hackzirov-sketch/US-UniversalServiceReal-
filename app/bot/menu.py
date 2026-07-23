from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import func, select

from app.bot.admin_management import admin_management_menu
from app.bot.buttons import ButtonStyle, inline_button
from app.bot.payment_messages import format_som
from app.bot.pricing import pricing_menu
from app.core.config import get_settings
from app.db.enums import PaymentStatus, ServiceType
from app.db.models import (
    AuditLog,
    LedgerEntry,
    ManualProviderPrice,
    Order,
    Payment,
    User,
)
from app.db.session import session_factory
from app.services.manual_pricing import list_active_manual_prices, pricing_actor

USER_MENU_ROWS = (
    ("🎁 Konkursda ishtirok etish",),
    ("💳 Hisob to‘ldirish", "👛 Hisobim"),
    ("⭐ Stars olish", "💎 Premium olish"),
    ("🎁 Gift olish", "🌱 Ferma"),
    ("🏆 Reyting", "🎯 Ballarim"),
    ("🎁 Bonuslarim", "📦 Buyurtmalarim"),
    ("👤 Profil", "ℹ️ Yordam"),
)

ADMIN_MENU_ROWS = (
    ("💰 Narxlar",),
    ("🧾 To‘lov review", "📦 Buyurtmalar"),
    ("📜 Audit", "ℹ️ Yordam"),
)

SUPERADMIN_MENU_ROWS = (
    ("💰 Narxlar",),
    ("🧾 To‘lov review", "📦 Buyurtmalar"),
    ("💳 Asosiy karta", "👥 Adminlar"),
    ("🎨 Tugmalar dizayni",),
    ("🚀 Real savdo",),
    ("📜 Audit", "ℹ️ Yordam"),
)

USER_MENU_CALLBACKS = (
    ("menu:contest",),
    ("topup:start", "menu:account"),
    ("menu:stars", "menu:premium"),
    ("menu:gifts", "menu:farm"),
    ("menu:rating", "menu:points"),
    ("menu:bonuses", "menu:orders"),
    ("menu:profile", "menu:help"),
)

ADMIN_MENU_CALLBACKS = (
    ("admin:pricing",),
    ("admin:payments", "admin:orders"),
    ("admin:audit", "menu:help"),
)

SUPERADMIN_MENU_CALLBACKS = (
    ("admin:pricing",),
    ("admin:payments", "admin:orders"),
    ("admin:card", "admin:admins"),
    ("admin:button_design",),
    ("sales:home",),
    ("admin:audit", "menu:help"),
)

MAIN_BUTTON_STYLES: dict[str, ButtonStyle] = {
    "menu:contest": "danger",
    "topup:start": "primary",
    "menu:account": "primary",
    "menu:stars": "success",
    "menu:premium": "success",
    "menu:gifts": "success",
    "menu:farm": "primary",
    "menu:rating": "primary",
    "menu:profile": "primary",
}

MAIN_BUTTON_EMOJI_KEYS = {
    "menu:contest": "contest",
    "topup:start": "topup",
    "menu:account": "account",
    "menu:stars": "stars",
    "menu:premium": "premium",
    "menu:gifts": "gift",
    "menu:farm": "farm",
    "menu:rating": "rating",
    "menu:points": "points",
    "menu:bonuses": "bonuses",
    "menu:orders": "orders",
    "menu:profile": "profile",
    "menu:help": "help",
    "admin:pricing": "pricing",
    "admin:payments": "payment_review",
    "admin:orders": "admin_orders",
    "admin:card": "card",
    "admin:admins": "admins",
    "admin:button_design": "button_design",
    "sales:home": "real_sales",
    "admin:audit": "audit",
}


class CatalogStates(StatesGroup):
    custom_stars = State()


class AdminMgmtStates(StatesGroup):
    add_reference = State()


class SupportStates(StatesGroup):
    awaiting_message = State()
    awaiting_reply = State()


def main_menu(*, is_admin: bool, is_superadmin: bool) -> InlineKeyboardMarkup:
    if is_superadmin:
        rows, callbacks = SUPERADMIN_MENU_ROWS, SUPERADMIN_MENU_CALLBACKS
    elif is_admin:
        rows, callbacks = ADMIN_MENU_ROWS, ADMIN_MENU_CALLBACKS
    else:
        rows, callbacks = USER_MENU_ROWS, USER_MENU_CALLBACKS
    menu_rows = [
        [
            inline_button(
                text=text,
                callback_data=callback_data,
                style=MAIN_BUTTON_STYLES.get(callback_data),
                emoji_key=MAIN_BUTTON_EMOJI_KEYS.get(callback_data),
            )
            for text, callback_data in zip(row, callback_row, strict=True)
        ]
        for row, callback_row in zip(rows, callbacks, strict=True)
    ]
    settings = get_settings()
    if is_admin or is_superadmin:
        if settings.admin_webapp_url:
            menu_rows.insert(
                0,
                [
                    inline_button(
                        text="🛡 Admin panelni ochish",
                        web_app=WebAppInfo(url=settings.admin_webapp_url),
                        style="primary",
                        emoji_key="open_admin_webapp",
                    )
                ],
            )
    elif settings.user_webapp_url:
        menu_rows.insert(
            0,
            [
                inline_button(
                    text="🚀 UniversalService’ni ochish",
                    web_app=WebAppInfo(url=settings.user_webapp_url),
                    style="primary",
                    emoji_key="open_webapp",
                )
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=menu_rows)


def can_open_admin_menu(*, is_admin: bool, is_superadmin: bool) -> bool:
    return is_admin or is_superadmin


def navigation_keyboard(*, back_callback: str = "nav:home") -> list[InlineKeyboardButton]:
    return [
        inline_button(text="◀️ Orqaga", callback_data=back_callback, emoji_key="back"),
        inline_button(text="🏠 Bosh menyu", callback_data="nav:home", emoji_key="home"),
    ]


STAR_PRESETS = (50, 75, 100, 150, 250, 350, 500, 750, 1000, 1500, 2500, 5000, 10000)


def stars_menu(*, unit_price_som: int | None = None) -> InlineKeyboardMarkup:
    buttons = [
        inline_button(
            text=_stars_button_text(quantity, unit_price_som),
            callback_data=f"stars:q{quantity}",
            style="success",
            emoji_key="stars",
        )
        for quantity in STAR_PRESETS
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(
        inline_keyboard=rows
        + [
            [InlineKeyboardButton(text="🔢 Boshqa miqdor", callback_data="stars:custom")],
            navigation_keyboard(),
        ]
    )


def premium_menu(*, prices: dict[int, int] | None = None) -> InlineKeyboardMarkup:
    values = prices or {}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text=_premium_button_text(3, values.get(3)),
                    callback_data="premium:m3",
                    style="success",
                    emoji_key="premium",
                ),
                inline_button(
                    text=_premium_button_text(6, values.get(6)),
                    callback_data="premium:m6",
                    style="success",
                    emoji_key="premium",
                ),
            ],
            [
                inline_button(
                    text=_premium_button_text(12, values.get(12)),
                    callback_data="premium:m12",
                    style="success",
                    emoji_key="premium",
                )
            ],
            navigation_keyboard(),
        ]
    )


def gift_menu(prices: Iterable[ManualProviderPrice]) -> InlineKeyboardMarkup:
    buttons = [
        inline_button(
            text=(
                f"🎁 {_short_label(price.display_name, maximum=12)} — "
                f"{format_som(price.sale_price_som)} so‘m"
            ),
            callback_data=f"gift:{price.id}",
            style="success",
            emoji_key="gift",
        )
        for price in prices
        if price.service_type == ServiceType.GIFT and price.active
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append(navigation_keyboard())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 Hisob to‘ldirish", callback_data="topup:start"),
                InlineKeyboardButton(text="📜 Balans tarixi", callback_data="account:history"),
            ],
            [
                InlineKeyboardButton(text="🎁 Bonuslarim", callback_data="menu:bonuses"),
                InlineKeyboardButton(text="🎯 Ballarim", callback_data="menu:points"),
            ],
            navigation_keyboard(),
        ]
    )


def farm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌾 Ekin ekish", callback_data="farm:plant"),
                InlineKeyboardButton(text="💧 Sug‘orish", callback_data="farm:water"),
            ],
            [
                InlineKeyboardButton(text="🧺 Hosilni yig‘ish", callback_data="farm:harvest"),
                InlineKeyboardButton(text="🏪 Do‘kon", callback_data="farm:shop"),
            ],
            [
                InlineKeyboardButton(text="⚡ Energiya", callback_data="farm:energy"),
                InlineKeyboardButton(text="📋 Vazifalar", callback_data="farm:tasks"),
            ],
            [
                InlineKeyboardButton(text="📊 Ferma holati", callback_data="farm:status"),
                InlineKeyboardButton(text="🎯 Farm ballari", callback_data="farm:points"),
            ],
            navigation_keyboard(),
        ]
    )


def purchase_confirmation_keyboard(*, back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="👛 Balansdan to‘lash",
                    callback_data="purchase:confirm",
                    style="success",
                    emoji_key="confirm",
                )
            ],
            navigation_keyboard(back_callback=back_callback),
        ]
    )


def insufficient_balance_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="💳 Balansni to‘ldirish",
                    callback_data="topup:start",
                    style="success",
                    emoji_key="topup",
                )
            ]
        ]
    )


def simple_internal_keyboard(*, back_callback: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[navigation_keyboard(back_callback=back_callback)])


def build_menu_router() -> Router:
    router = Router(name="navigation_menus")

    @router.message(Command("start"))
    async def start(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.clear()
        is_admin, is_superadmin = await _roles(message.from_user.id)
        removal_message = await message.answer(
            "Eski pastki menyu yopildi.", reply_markup=ReplyKeyboardRemove()
        )
        with suppress(TelegramBadRequest):
            await removal_message.delete()
        await message.answer(
            "UniversalService bosh menyusi",
            reply_markup=main_menu(is_admin=is_admin, is_superadmin=is_superadmin),
        )

    @router.message(Command("admin"))
    async def admin_start(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        is_admin, is_superadmin = await _roles(message.from_user.id)
        if not is_admin and not is_superadmin:
            await message.answer("Admin bo‘limiga ruxsat yo‘q.")
            return
        await state.clear()
        await message.answer(
            "Admin bosh menyusi",
            reply_markup=main_menu(is_admin=is_admin, is_superadmin=is_superadmin),
        )

    @router.callback_query(F.data == "nav:home")
    async def home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        is_admin, is_superadmin = await _roles(callback.from_user.id)
        await _answer_callback(
            callback,
            "UniversalService bosh menyusi",
            main_menu(is_admin=is_admin, is_superadmin=is_superadmin),
        )

    @router.message(F.text == "⭐ Stars olish")
    async def stars(message: Message) -> None:
        price = await _active_manual_price("DIRECT:STARS")
        await message.answer(
            _stars_catalog_text(price),
            reply_markup=stars_menu(
                unit_price_som=price.sale_price_som if price is not None else None
            ),
        )

    @router.callback_query(F.data == "menu:stars")
    async def stars_callback(callback: CallbackQuery) -> None:
        price = await _active_manual_price("DIRECT:STARS")
        await _answer_callback(
            callback,
            _stars_catalog_text(price),
            stars_menu(unit_price_som=price.sale_price_som if price is not None else None),
        )

    @router.callback_query(F.data.startswith("stars:q"))
    async def choose_stars(callback: CallbackQuery, state: FSMContext) -> None:
        quantity = _positive_int((callback.data or "").removeprefix("stars:q"))
        if quantity not in STAR_PRESETS:
            await callback.answer("Noto‘g‘ri miqdor", show_alert=True)
            return
        await _show_stars_quote(callback, state, quantity)

    @router.callback_query(F.data == "stars:custom")
    async def custom_stars(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(CatalogStates.custom_stars)
        await _answer_callback(
            callback,
            "50 dan 10 000 gacha Stars miqdorini kiriting:",
            simple_internal_keyboard(back_callback="menu:stars"),
        )

    @router.message(CatalogStates.custom_stars)
    async def custom_stars_amount(message: Message, state: FSMContext) -> None:
        value = _positive_int(message.text)
        if value is None or not 50 <= value <= 10_000:
            await message.answer("50 dan 10 000 gacha butun miqdor kiriting.")
            return
        price = await _active_manual_price("DIRECT:STARS")
        if price is None:
            await message.answer(
                "Stars narxi vaqtincha mavjud emas.",
                reply_markup=stars_menu(),
            )
            await state.clear()
            return
        await state.update_data(service="STARS", quantity=value, price_id=price.id)
        await state.set_state(None)
        await message.answer(
            _stars_quote_text(value, price),
            reply_markup=purchase_confirmation_keyboard(back_callback="menu:stars"),
        )

    @router.message(F.text == "💎 Premium olish")
    async def premium(message: Message) -> None:
        await message.answer("💎 Premium paketini tanlang:", reply_markup=await _premium_menu())

    @router.callback_query(F.data == "menu:premium")
    async def premium_callback(callback: CallbackQuery) -> None:
        await _answer_callback(callback, "💎 Premium paketini tanlang:", await _premium_menu())

    @router.callback_query(F.data.startswith("premium:m"))
    async def choose_premium(callback: CallbackQuery, state: FSMContext) -> None:
        months_by_action = {"premium:m3": 3, "premium:m6": 6, "premium:m12": 12}
        months = months_by_action.get(callback.data or "")
        if months is None:
            await callback.answer("Noto‘g‘ri paket", show_alert=True)
            return
        price = await _active_manual_price(f"DIRECT:PREMIUM:{months}")
        if price is None:
            await _answer_callback(
                callback,
                "Bu paket narxi vaqtincha mavjud emas.",
                await _premium_menu(),
            )
            return
        await state.update_data(service="PREMIUM", months=months, price_id=price.id)
        await _answer_callback(
            callback,
            f"💎 Premium — {months} oy\nNarx: {format_som(price.sale_price_som)} so‘m",
            purchase_confirmation_keyboard(back_callback="menu:premium"),
        )

    @router.message(F.text == "🎁 Gift olish")
    async def gifts(message: Message) -> None:
        prices = await _active_gifts()
        await message.answer(
            "🎁 Faol Giftlardan birini tanlang:" if prices else "Faol Gift hozircha yo‘q.",
            reply_markup=gift_menu(prices),
        )

    @router.callback_query(F.data == "menu:gifts")
    async def gifts_callback(callback: CallbackQuery) -> None:
        prices = await _active_gifts()
        await _answer_callback(
            callback,
            "🎁 Faol Giftlardan birini tanlang:" if prices else "Faol Gift hozircha yo‘q.",
            gift_menu(prices),
        )

    @router.callback_query(F.data.startswith("gift:"))
    async def gift_detail(callback: CallbackQuery, state: FSMContext) -> None:
        price_id = (callback.data or "").split(":", 1)[-1]
        async with session_factory() as session:
            price = await session.get(ManualProviderPrice, price_id)
        if price is None or not price.active or price.service_type != ServiceType.GIFT:
            await callback.answer("Gift mavjud emas", show_alert=True)
            return
        await state.update_data(service="GIFT", price_id=price.id)
        await _answer_callback(
            callback,
            f"🎁 {price.display_name}\nNarx: {format_som(price.sale_price_som)} so‘m",
            purchase_confirmation_keyboard(back_callback="menu:gifts"),
        )

    @router.callback_query(F.data == "purchase:confirm")
    async def confirm_purchase(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        async with session_factory() as session:
            user = await session.scalar(
                select(User).where(User.telegram_id == callback.from_user.id)
            )
            price = await session.get(ManualProviderPrice, data.get("price_id"))
        required = _purchase_total(price, data)
        available = user.available_balance_som if user is not None else 0
        if required is not None and available < required:
            await callback.answer()
            if callback.message is not None:
                await callback.message.answer(
                    "Buyurtma uchun balans yetarli emas. Balansni to‘ldiring.",
                    reply_markup=insufficient_balance_keyboard(),
                )
            return
        if not get_settings().direct_sales_enabled:
            await callback.answer("Xizmat xaridi vaqtincha yopiq", show_alert=True)
            return
        await callback.answer("Xarid oqimi hozircha mavjud emas", show_alert=True)

    @router.message(F.text == "👛 Hisobim")
    @router.callback_query(F.data == "menu:account")
    async def account(event: Message | CallbackQuery) -> None:
        telegram_id = event.from_user.id
        is_admin, is_superadmin = await _roles(telegram_id)
        if is_admin or is_superadmin:
            await _answer_event(
                event,
                "Admin akkauntlarida foydalanuvchi balansi bo‘limi mavjud emas.",
                simple_internal_keyboard(),
            )
            return
        async with session_factory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        balance = user.available_balance_som if user else 0
        text = (
            "👛 Hisobim\n\n"
            f"Asosiy balans: {format_som(balance)} so‘m\n"
            "Bonus balans: 0 so‘m\n"
            "Farm ballari: 0\n"
            "Reyting ballari: 0"
        )
        await _answer_event(event, text, account_menu())

    @router.callback_query(F.data == "account:history")
    async def balance_history(callback: CallbackQuery) -> None:
        async with session_factory() as session:
            user = await session.scalar(
                select(User).where(User.telegram_id == callback.from_user.id)
            )
            entries = []
            if user:
                entries = list(
                    await session.scalars(
                        select(LedgerEntry)
                        .where(LedgerEntry.user_id == user.id)
                        .order_by(LedgerEntry.created_at.desc())
                        .limit(10)
                    )
                )
        lines = [f"{entry.type.value}: {format_som(entry.amount_som)} so‘m" for entry in entries]
        await _answer_callback(
            callback,
            "📜 Balans tarixi\n\n" + ("\n".join(lines) if lines else "Tarix bo‘sh."),
            simple_internal_keyboard(back_callback="menu:account"),
        )

    @router.message(F.text == "🌱 Ferma")
    @router.callback_query(F.data == "menu:farm")
    async def farm(event: Message | CallbackQuery) -> None:
        await _answer_event(
            event,
            "🌱 Ferma — ichki o‘yin bo‘limi. Pul chiqarish yoki staking emas.",
            farm_menu(),
        )

    @router.callback_query(F.data.startswith("farm:"))
    async def farm_action(callback: CallbackQuery) -> None:
        labels = {
            "plant": "Ekin ekish",
            "water": "Sug‘orish",
            "harvest": "Hosilni yig‘ish",
            "shop": "Do‘kon",
            "energy": "Energiya",
            "tasks": "Vazifalar",
            "status": "Ferma holati",
            "points": "Farm ballari",
        }
        action = (callback.data or "").split(":", 1)[-1]
        await _answer_callback(
            callback,
            f"🌱 {labels.get(action, 'Ferma')} bo‘limi tayyorlanmoqda.",
            farm_menu(),
        )

    for text, title in (
        ("🎁 Konkursda ishtirok etish", "🎁 Konkurs"),
        ("🏆 Reyting", "🏆 Reyting"),
        ("🎯 Ballarim", "🎯 Ballarim"),
        ("🎁 Bonuslarim", "🎁 Bonuslarim"),
        ("👤 Profil", "👤 Profil"),
    ):
        router.message.register(_placeholder_handler(title), F.text == text)

    placeholder_callbacks = {
        "menu:contest": "🎁 Konkurs",
        "menu:rating": "🏆 Reyting",
        "menu:points": "🎯 Ballarim",
        "menu:bonuses": "🎁 Bonuslarim",
        "menu:profile": "👤 Profil",
    }

    @router.callback_query(F.data.in_(placeholder_callbacks))
    async def placeholder_callback(callback: CallbackQuery) -> None:
        title = placeholder_callbacks[callback.data or ""]
        await _answer_callback(
            callback,
            f"{title}\n\nBo‘lim tayyorlanmoqda.",
            simple_internal_keyboard(),
        )

    @router.message(F.text == "📦 Buyurtmalarim")
    @router.callback_query(F.data == "menu:orders")
    async def orders(event: Message | CallbackQuery) -> None:
        telegram_id = event.from_user.id
        async with session_factory() as session:
            rows = list(
                await session.scalars(
                    select(Order)
                    .join(User, User.id == Order.user_id)
                    .where(User.telegram_id == telegram_id)
                    .order_by(Order.created_at.desc())
                    .limit(10)
                )
            )
        text = (
            "\n".join(
                f"#{order.public_order_number} — {order.internal_status.value}" for order in rows
            )
            or "Buyurtmalar hali yo‘q."
        )
        await _answer_event(
            event,
            f"📦 Buyurtmalarim\n\n{text}",
            simple_internal_keyboard(),
        )

    admin_menu_items = {item for row in ADMIN_MENU_ROWS + SUPERADMIN_MENU_ROWS for item in row}

    @router.message(F.text.in_(admin_menu_items))
    async def admin_menu_action(message: Message) -> None:
        if message.from_user is None:
            return
        is_admin, is_superadmin = await _roles(message.from_user.id)
        if not can_open_admin_menu(is_admin=is_admin, is_superadmin=is_superadmin):
            await message.answer("Admin menyusiga ruxsat yo‘q.")
            return
        if message.text == "💳 Asosiy karta" and is_superadmin:
            await message.answer("Asosiy karta boshqaruvi:", reply_markup=_card_entry_keyboard())
            return
        if message.text == "💰 Narxlar":
            actor = await _pricing_actor(message.from_user.id)
            if not actor.can_manage_pricing:
                await message.answer("Narxlarni boshqarish huquqi berilmagan.")
                return
            await message.answer("💰 Narxlar boshqaruvi", reply_markup=pricing_menu())
            return
        if message.text == "🧾 To‘lov review":
            await _show_payment_review_summary(message)
            return
        if message.text == "📦 Buyurtmalar":
            await _show_admin_orders(message)
            return
        if message.text == "📜 Audit":
            await _show_recent_audit(message)
            return
        if message.text == "👥 Adminlar" and is_superadmin:
            await _show_admins(message)
            return
        await message.answer(
            "Kerakli bo‘limni menyudagi tugmalar orqali tanlang.",
            reply_markup=simple_internal_keyboard(),
        )

    admin_callbacks = {
        "admin:pricing": "pricing",
        "admin:payments": "payments",
        "admin:orders": "orders",
        "admin:audit": "audit",
        "admin:admins": "admins",
    }

    @router.callback_query(F.data.in_(admin_callbacks))
    async def admin_menu_callback(callback: CallbackQuery) -> None:
        is_admin, is_superadmin = await _roles(callback.from_user.id)
        if not can_open_admin_menu(is_admin=is_admin, is_superadmin=is_superadmin):
            await callback.answer("Admin menyusiga ruxsat yo‘q", show_alert=True)
            return
        action = admin_callbacks[callback.data or ""]
        if action == "admins" and not is_superadmin:
            await callback.answer("Ruxsat yo‘q", show_alert=True)
            return
        await callback.answer()
        if callback.message is None:
            return
        source_message = callback.message
        with suppress(TelegramBadRequest):
            await source_message.delete()
        if action == "pricing":
            actor = await _pricing_actor(callback.from_user.id)
            if not actor.can_manage_pricing:
                await source_message.answer("Narxlarni boshqarish huquqi berilmagan.")
            else:
                await source_message.answer("💰 Narxlar boshqaruvi", reply_markup=pricing_menu())
        elif action == "payments":
            await _show_payment_review_summary(source_message)
        elif action == "orders":
            await _show_admin_orders(source_message)
        elif action == "audit":
            await _show_recent_audit(source_message)
        elif action == "admins":
            await _show_admins(source_message)

    @router.callback_query(F.data == "admin:card")
    async def admin_card(callback: CallbackQuery) -> None:
        _is_admin, is_superadmin = await _roles(callback.from_user.id)
        if not is_superadmin:
            await callback.answer("Ruxsat yo‘q", show_alert=True)
            return
        await _answer_callback(callback, "Asosiy karta boshqaruvi:", _card_entry_keyboard())

    return router


async def _show_stars_quote(callback: CallbackQuery, state: FSMContext, quantity: int) -> None:
    price = await _active_manual_price("DIRECT:STARS")
    if price is None:
        await _answer_callback(callback, "Stars narxi vaqtincha mavjud emas.", stars_menu())
        return
    await state.update_data(service="STARS", quantity=quantity, price_id=price.id)
    await _answer_callback(
        callback,
        _stars_quote_text(quantity, price),
        purchase_confirmation_keyboard(back_callback="menu:stars"),
    )


def _stars_quote_text(quantity: int, price: ManualProviderPrice) -> str:
    return f"⭐ {quantity} Stars\nJoriy narx: {format_som(price.sale_price_som * quantity)} so‘m"


def _stars_button_text(quantity: int, unit_price_som: int | None) -> str:
    if unit_price_som is None:
        return f"⭐ {quantity} Stars"
    return f"⭐ {quantity} — {format_som(unit_price_som * quantity)} so‘m"


def _stars_catalog_text(price: ManualProviderPrice | None) -> str:
    if price is None:
        return "⭐ Stars narxi vaqtincha mavjud emas."
    return (
        "⭐ Telegram Stars buyurtma\n\n"
        f"Minimal: {price.min_quantity or 50}\n"
        f"Maksimal: {price.max_quantity or 10_000}\n\n"
        "Kerakli miqdorni tanlang yoki raqam bilan yuboring 👇"
    )


def _premium_button_text(months: int, price_som: int | None) -> str:
    if price_som is None:
        return f"💎 {months} oy"
    return f"💎 {months} oy — {format_som(price_som)} so‘m"


async def _premium_menu() -> InlineKeyboardMarkup:
    prices: dict[int, int] = {}
    for months in (3, 6, 12):
        price = await _active_manual_price(f"DIRECT:PREMIUM:{months}")
        if price is not None:
            prices[months] = price.sale_price_som
    return premium_menu(prices=prices)


async def _active_manual_price(service_key: str) -> ManualProviderPrice | None:
    prices = await _active_prices()
    return next((price for price in prices if price.service_key == service_key), None)


async def _active_gifts() -> list[ManualProviderPrice]:
    return [price for price in await _active_prices() if price.service_type == ServiceType.GIFT]


async def _active_prices() -> list[ManualProviderPrice]:
    async with session_factory() as session:
        return await list_active_manual_prices(session)


async def _roles(telegram_id: int) -> tuple[bool, bool]:
    settings = get_settings()
    is_superadmin = telegram_id in settings.superadmin_ids
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    return bool(user and user.is_admin and user.admin_active) or is_superadmin, is_superadmin


async def _pricing_actor(telegram_id: int):
    async with session_factory() as session:
        return await pricing_actor(
            session,
            telegram_id=telegram_id,
            superadmin_ids=get_settings().superadmin_ids,
        )


async def _show_payment_review_summary(message: Message) -> None:
    async with session_factory() as session:
        pending = await session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.status == PaymentStatus.REVIEW_PENDING.value)
        )
    await message.answer(
        f"🧾 Tekshiruv kutayotgan to‘lovlar: {pending or 0}\n\n"
        "Yangi chek kelganda tasdiqlash tugmalari shu bot orqali yuboriladi.",
        reply_markup=simple_internal_keyboard(),
    )


async def _show_admin_orders(message: Message) -> None:
    async with session_factory() as session:
        orders = list(
            await session.scalars(select(Order).order_by(Order.created_at.desc()).limit(10))
        )
    lines = [f"#{order.public_order_number} — {order.internal_status.value}" for order in orders]
    await message.answer(
        "📦 Oxirgi buyurtmalar\n\n" + ("\n".join(lines) if lines else "Buyurtma yo‘q."),
        reply_markup=simple_internal_keyboard(),
    )


async def _show_recent_audit(message: Message) -> None:
    async with session_factory() as session:
        rows = list(
            await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10))
        )
    lines = [f"{row.created_at:%d.%m %H:%M} — {row.action}" for row in rows]
    await message.answer(
        "📜 Oxirgi audit amallari\n\n" + ("\n".join(lines) if lines else "Audit bo‘sh."),
        reply_markup=simple_internal_keyboard(),
    )


async def _show_admins(message: Message) -> None:
    await message.answer("👥 Adminlar", reply_markup=admin_management_menu())


async def _answer_callback(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return
        source_message = callback.message
        with suppress(TelegramBadRequest):
            await source_message.delete()
        await source_message.answer(text, reply_markup=markup)


async def _answer_event(
    event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup
) -> None:
    if isinstance(event, CallbackQuery):
        await _answer_callback(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


def _placeholder_handler(title: str):
    async def handler(message: Message) -> None:
        await message.answer(
            f"{title}\n\nBo‘lim tayyorlanmoqda.",
            reply_markup=simple_internal_keyboard(),
        )

    return handler


def _card_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="💳 Asosiy karta",
                    callback_data="card:show",
                    style="primary",
                    emoji_key="card",
                )
            ],
            navigation_keyboard(),
        ]
    )


def _short_label(value: str, maximum: int = 22) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= maximum else normalized[: maximum - 1] + "…"


def _positive_int(value: str | None) -> int | None:
    text = (value or "").strip().replace(" ", "")
    return int(text) if text.isascii() and text.isdecimal() and int(text) > 0 else None


def _purchase_total(price: ManualProviderPrice | None, data: dict) -> int | None:
    if price is None or not price.active:
        return None
    if price.service_type == ServiceType.STARS:
        quantity = data.get("quantity")
        if not isinstance(quantity, int) or quantity <= 0:
            return None
        return price.sale_price_som * quantity
    return price.sale_price_som
