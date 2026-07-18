from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.enums import MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.buttons import inline_button
from app.core.config import get_settings
from app.db.session import session_factory
from app.services.button_design import (
    ALLOWED_BUTTON_STYLES,
    BUTTON_SPECS,
    get_cached_button_design,
    reset_button_design,
    save_button_design,
)


class ButtonDesignStates(StatesGroup):
    custom_emoji = State()
    preview = State()


def build_button_design_router() -> Router:
    router = Router(name="button_design_admin")

    @router.callback_query(F.data.in_({"admin:button_design", "design:list"}))
    async def design_list(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin(callback):
            return
        await state.clear()
        await _edit_or_answer(
            callback,
            "🎨 Tugmalar dizayni\n\nSozlamoqchi bo‘lgan tugmani tanlang:",
            _button_list_keyboard(),
        )

    @router.callback_query(F.data.startswith("design:key:"))
    async def select_button(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin(callback):
            return
        button_key = (callback.data or "").removeprefix("design:key:")
        spec = BUTTON_SPECS.get(button_key)
        if spec is None:
            await callback.answer("Noma’lum tugma", show_alert=True)
            return
        current = get_cached_button_design(button_key)
        await state.clear()
        await state.update_data(button_key=button_key)
        current_style = current.button_style if current else "default"
        emoji_status = "sozlangan" if current and current.custom_emoji_id else "Unicode fallback"
        await _edit_or_answer(
            callback,
            f"{spec.text}\n\nJoriy rang: {current_style}\nEmoji: {emoji_status}\n\nRangni tanlang:",
            _style_keyboard(button_key),
        )

    @router.callback_query(F.data.startswith("design:style:"))
    async def select_style(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin(callback):
            return
        parts = (callback.data or "").split(":", 3)
        if len(parts) != 4 or parts[2] not in BUTTON_SPECS or parts[3] not in ALLOWED_BUTTON_STYLES:
            await callback.answer("Noto‘g‘ri dizayn tanlovi", show_alert=True)
            return
        await state.set_state(ButtonDesignStates.custom_emoji)
        await state.update_data(button_key=parts[2], button_style=parts[3])
        await _edit_or_answer(
            callback,
            "Animatsion custom emojini yuboring.\n\n"
            "Oddiy Unicode emoji qabul qilinmaydi. Emojisiz davom etsangiz fallback ishlaydi.",
            _emoji_prompt_keyboard(parts[2]),
        )

    @router.message(ButtonDesignStates.custom_emoji)
    async def receive_custom_emoji(message: Message, state: FSMContext) -> None:
        if not _is_superadmin(message.from_user.id if message.from_user else None):
            await message.answer("Bu amal faqat superadmin uchun.")
            await state.clear()
            return
        custom_emoji_id = extract_custom_emoji_id(message)
        if custom_emoji_id is None:
            await message.answer(
                "Custom animatsion emoji aniqlanmadi. Telegram Premium custom emojidan "
                "faqat bittasini yuboring. Oddiy emoji qabul qilinmaydi."
            )
            return
        with suppress(TelegramBadRequest):
            await message.delete()
        await state.update_data(custom_emoji_id=custom_emoji_id)
        await state.set_state(ButtonDesignStates.preview)
        data = await state.get_data()
        await message.answer(
            "Preview:",
            reply_markup=_preview_keyboard(
                data["button_key"], data["button_style"], custom_emoji_id
            ),
        )

    @router.callback_query(F.data == "design:emoji:none", ButtonDesignStates.custom_emoji)
    async def no_custom_emoji(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin(callback):
            return
        await state.update_data(custom_emoji_id=None)
        await state.set_state(ButtonDesignStates.preview)
        data = await state.get_data()
        await _edit_or_answer(
            callback,
            "Preview — Unicode emoji fallback ishlatiladi:",
            _preview_keyboard(data["button_key"], data["button_style"], None),
        )

    @router.callback_query(F.data == "design:save", ButtonDesignStates.preview)
    async def save_design(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin(callback):
            return
        data = await state.get_data()
        async with session_factory.begin() as session:
            await save_button_design(
                session,
                button_key=data["button_key"],
                button_style=data["button_style"],
                custom_emoji_id=data.get("custom_emoji_id"),
                actor_telegram_id=callback.from_user.id,
            )
        await state.clear()
        await _edit_or_answer(
            callback,
            "✅ Tugma dizayni saqlandi.",
            _button_list_keyboard(),
        )

    @router.callback_query(F.data.startswith("design:reset:"))
    async def reset_design(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin(callback):
            return
        button_key = (callback.data or "").removeprefix("design:reset:")
        if button_key not in BUTTON_SPECS:
            await callback.answer("Noma’lum tugma", show_alert=True)
            return
        async with session_factory.begin() as session:
            await reset_button_design(
                session,
                button_key=button_key,
                actor_telegram_id=callback.from_user.id,
            )
        await state.clear()
        await _edit_or_answer(
            callback,
            "Tugma default holatga qaytarildi.",
            _button_list_keyboard(),
        )

    @router.callback_query(F.data == "design:noop")
    async def preview_noop(callback: CallbackQuery) -> None:
        await callback.answer("Bu preview tugmasi")

    return router


def extract_custom_emoji_id(message: Message) -> str | None:
    entities = tuple(message.entities or ()) + tuple(message.caption_entities or ())
    custom_ids = [
        entity.custom_emoji_id
        for entity in entities
        if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id
    ]
    return custom_ids[0] if len(custom_ids) == 1 else None


def _button_list_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        inline_button(
            text=spec.text,
            callback_data=f"design:key:{spec.key}",
            emoji_key=spec.key,
        )
        for spec in BUTTON_SPECS.values()
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append([inline_button(text="🏠 Bosh menyu", callback_data="nav:home", emoji_key="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _style_keyboard(button_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="Default", callback_data=f"design:style:{button_key}:default"),
                inline_button(
                    text="🔵 Primary",
                    callback_data=f"design:style:{button_key}:primary",
                    style="primary",
                ),
            ],
            [
                inline_button(
                    text="🟢 Success",
                    callback_data=f"design:style:{button_key}:success",
                    style="success",
                ),
                inline_button(
                    text="🔴 Danger",
                    callback_data=f"design:style:{button_key}:danger",
                    style="danger",
                ),
            ],
            [
                inline_button(
                    text="♻️ Defaultga qaytarish",
                    callback_data=f"design:reset:{button_key}",
                    style="danger",
                )
            ],
            [inline_button(text="◀️ Orqaga", callback_data="design:list", emoji_key="back")],
        ]
    )


def _emoji_prompt_keyboard(button_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="Unicode fallback bilan davom etish",
                    callback_data="design:emoji:none",
                    emoji_key="unicode_fallback",
                )
            ],
            [
                inline_button(
                    text="♻️ Defaultga qaytarish",
                    callback_data=f"design:reset:{button_key}",
                    style="danger",
                )
            ],
            [
                inline_button(
                    text="◀️ Orqaga",
                    callback_data=f"design:key:{button_key}",
                    emoji_key="back",
                )
            ],
        ]
    )


def _preview_keyboard(
    button_key: str, style: str, custom_emoji_id: str | None
) -> InlineKeyboardMarkup:
    spec = BUTTON_SPECS[button_key]
    label = spec.text.partition(" ")[2] if custom_emoji_id else spec.text
    resolved_style = None if style == "default" else style
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data="design:noop",
                    style=resolved_style,
                    icon_custom_emoji_id=custom_emoji_id,
                )
            ],
            [
                inline_button(
                    text="✅ Saqlash",
                    callback_data="design:save",
                    style="success",
                    emoji_key="confirm",
                ),
                inline_button(
                    text="❌ Bekor qilish",
                    callback_data=f"design:key:{button_key}",
                    style="danger",
                    emoji_key="cancel",
                ),
            ],
            [
                inline_button(
                    text="♻️ Defaultga qaytarish",
                    callback_data=f"design:reset:{button_key}",
                    style="danger",
                )
            ],
        ]
    )


async def _require_superadmin(callback: CallbackQuery) -> bool:
    if not _is_superadmin(callback.from_user.id):
        await callback.answer("Bu bo‘lim faqat superadmin uchun", show_alert=True)
        return False
    return True


def _is_superadmin(telegram_id: int | None) -> bool:
    return telegram_id is not None and telegram_id in get_settings().superadmin_ids


async def _edit_or_answer(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            await callback.message.answer(text, reply_markup=keyboard)
