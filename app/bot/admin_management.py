from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.buttons import inline_button
from app.core.config import get_settings
from app.db.models import AdminPermission, User
from app.db.session import session_factory
from app.services.admin import (
    ADMIN_PERMISSIONS,
    AdminActionError,
    add_admin,
    list_admin_cards,
    preview_admin_candidate,
    remove_admin,
    replace_admin_permissions,
    set_admin_active,
)
from app.services.audit import audit_text, list_business_audit


class AdminManagementStates(StatesGroup):
    candidate_reference = State()
    candidate_confirm = State()
    search_reference = State()
    permissions = State()
    remove_confirm = State()


def admin_management_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="➕ Yangi admin",
                    callback_data="adm:add",
                    style="success",
                    emoji_key="add_admin",
                ),
                inline_button(
                    text="📋 Adminlar ro‘yxati",
                    callback_data="adm:list",
                    style="primary",
                    emoji_key="admin_list",
                ),
            ],
            [
                inline_button(
                    text="🔎 Admin qidirish",
                    callback_data="adm:search",
                    style="primary",
                    emoji_key="search",
                ),
                inline_button(
                    text="🛡 Huquqlar",
                    callback_data="adm:rights",
                    style="primary",
                    emoji_key="permissions",
                ),
            ],
            [
                inline_button(
                    text="🚫 Bloklanganlar",
                    callback_data="adm:blocked",
                    style="danger",
                    emoji_key="blocked_admins",
                ),
                inline_button(
                    text="📜 Admin amallari", callback_data="adm:audit", emoji_key="admin_actions"
                ),
            ],
            [
                inline_button(text="◀️ Orqaga", callback_data="nav:home", emoji_key="back"),
                inline_button(text="🏠 Bosh menyu", callback_data="nav:home", emoji_key="home"),
            ],
        ]
    )


def build_admin_management_router() -> Router:
    router = Router(name="admin_management")

    async def require_superadmin(callback: CallbackQuery) -> bool:
        if callback.from_user.id not in get_settings().superadmin_ids:
            await callback.answer("Bu amal faqat superadmin uchun", show_alert=True)
            return False
        return True

    @router.callback_query(F.data == "adm:add")
    async def add_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        await state.clear()
        await state.set_state(AdminManagementStates.candidate_reference)
        await _answer(
            callback,
            "Yangi adminni qanday topamiz?",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        inline_button(
                            text="🆔 Telegram ID orqali",
                            callback_data="adm:add:id",
                            style="primary",
                            emoji_key="telegram_id",
                        )
                    ],
                    [
                        inline_button(
                            text="👤 Username orqali",
                            callback_data="adm:add:username",
                            style="primary",
                            emoji_key="username",
                        )
                    ],
                    [
                        inline_button(
                            text="❌ Bekor qilish",
                            callback_data="adm:cancel",
                            style="danger",
                            emoji_key="cancel",
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data.in_({"adm:add:id", "adm:add:username"}))
    async def add_method(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        mode = (callback.data or "").rsplit(":", 1)[1]
        await state.update_data(mode=mode)
        await state.set_state(AdminManagementStates.candidate_reference)
        await _answer(
            callback,
            "Musbat numeric Telegram ID yuboring:"
            if mode == "id"
            else "Bot bazasidagi @username ni yuboring:",
        )

    @router.message(AdminManagementStates.candidate_reference)
    async def candidate_input(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.from_user.id not in get_settings().superadmin_ids:
            return
        reference = (message.text or "").strip()
        data = await state.get_data()
        if data.get("mode") == "id" and (not reference.isascii() or not reference.isdecimal()):
            await message.answer("Faqat musbat numeric Telegram ID yuboring.")
            return
        try:
            async with session_factory.begin() as session:
                user = await preview_admin_candidate(session, reference=reference)
            await state.update_data(reference=reference, user_id=user.id, version=user.version)
            await state.set_state(AdminManagementStates.candidate_confirm)
            await message.answer(
                _candidate_text(user),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            inline_button(
                                text="✅ Admin qilish",
                                callback_data="adm:add:confirm",
                                style="success",
                                emoji_key="confirm",
                            ),
                            inline_button(
                                text="❌ Bekor qilish",
                                callback_data="adm:cancel",
                                style="danger",
                                emoji_key="cancel",
                            ),
                        ]
                    ]
                ),
            )
        except AdminActionError as exc:
            await message.answer(str(exc))

    @router.callback_query(F.data == "adm:add:confirm", AdminManagementStates.candidate_confirm)
    async def candidate_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                current = await session.get(User, data.get("user_id"), with_for_update=True)
                if current is None or current.version != data.get("version") or current.is_admin:
                    raise AdminActionError("Bu tasdiq eskirgan. Admin profilini qayta oching")
                user = await add_admin(
                    session,
                    reference=data["reference"],
                    actor_telegram_id=callback.from_user.id,
                    superadmin_ids=get_settings().superadmin_ids,
                )
            await state.clear()
            await _answer(
                callback, f"✅ {user.telegram_id} admin qilindi.", admin_management_menu()
            )
        except AdminActionError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.callback_query(F.data == "adm:list")
    async def admin_list(callback: CallbackQuery) -> None:
        if not await require_superadmin(callback):
            return
        async with session_factory() as session:
            cards = await list_admin_cards(session)
        rows = [
            [
                inline_button(
                    text=(
                        f"{'🟢' if card.user.admin_active else '⏸'} "
                        f"@{card.user.username or card.user.telegram_id}"
                    ),
                    callback_data=f"adm:view:{card.user.telegram_id}",
                    style="success" if card.user.admin_active else "danger",
                    emoji_key="view",
                )
            ]
            for card in cards
        ]
        rows.append([inline_button(text="◀️ Orqaga", callback_data="adm:home", emoji_key="back")])
        await _answer(
            callback,
            "📋 Adminlar ro‘yxati" if cards else "Adminlar ro‘yxati bo‘sh.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.startswith("adm:view:"))
    async def admin_view(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        target = _callback_id(callback.data)
        if target is None:
            await callback.answer("Noto‘g‘ri admin ID", show_alert=True)
            return
        async with session_factory() as session:
            cards = await list_admin_cards(session)
        card = next((item for item in cards if item.user.telegram_id == target), None)
        if card is None:
            await callback.answer("Admin topilmadi yoki holati o‘zgargan", show_alert=True)
            return
        await state.update_data(target_id=target, session_version=card.user.admin_session_version)
        toggle_text = "▶️ Faollashtirish" if not card.user.admin_active else "⏸ Faolsizlantirish"
        toggle_action = "enable" if not card.user.admin_active else "disable"
        await _answer(
            callback,
            _admin_card_text(card),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        inline_button(
                            text="👁 Ko‘rish", callback_data=f"adm:view:{target}", emoji_key="view"
                        ),
                        inline_button(
                            text="🛡 Huquqlar",
                            callback_data=f"adm:perm:{target}",
                            style="primary",
                            emoji_key="permissions",
                        ),
                    ],
                    [
                        inline_button(
                            text=toggle_text,
                            callback_data=f"adm:{toggle_action}:{target}",
                            style="success" if toggle_action == "enable" else "danger",
                            emoji_key=toggle_action,
                        ),
                        inline_button(
                            text="🗑 Adminlikdan olish",
                            callback_data=f"adm:remove:{target}",
                            style="danger",
                            emoji_key="remove_admin",
                        ),
                    ],
                    [
                        inline_button(
                            text="📜 Amallari",
                            callback_data=f"adm:actions:{target}",
                            emoji_key="admin_actions",
                        ),
                        inline_button(text="◀️ Orqaga", callback_data="adm:list", emoji_key="back"),
                    ],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("adm:disable:") | F.data.startswith("adm:enable:"))
    async def toggle_admin(callback: CallbackQuery) -> None:
        if not await require_superadmin(callback):
            return
        target = _callback_id(callback.data)
        active = (callback.data or "").startswith("adm:enable:")
        try:
            async with session_factory.begin() as session:
                await set_admin_active(
                    session,
                    target_telegram_id=target or 0,
                    active=active,
                    actor_telegram_id=callback.from_user.id,
                    superadmin_ids=get_settings().superadmin_ids,
                )
            await _answer(callback, "Admin holati yangilandi.", admin_management_menu())
        except AdminActionError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("adm:remove:") & (F.data != "adm:remove:confirm"))
    async def remove_preview(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        target = _callback_id(callback.data)
        async with session_factory() as session:
            cards = await list_admin_cards(session)
        card = next((item for item in cards if item.user.telegram_id == target), None)
        if card is None:
            await callback.answer("Admin topilmadi", show_alert=True)
            return
        await state.set_state(AdminManagementStates.remove_confirm)
        await state.update_data(target_id=target, session_version=card.user.admin_session_version)
        last = card.last_action.human_summary if card.last_action else "yo‘q"
        await _answer(
            callback,
            "⚠️ Adminlikdan olish\n\n"
            f"Admin: @{card.user.username or 'username_yoq'}\n"
            f"ID: {card.user.telegram_id}\n"
            f"Faol reviewlar: {card.active_review_count}\n"
            f"Oxirgi amal: {last}\n\n"
            "Barcha admin huquqlari bekor qilinadi. Audit tarixi o‘chmaydi.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        inline_button(
                            text="✅ Adminlikdan olish",
                            callback_data="adm:remove:confirm",
                            style="danger",
                            emoji_key="remove_admin",
                        )
                    ],
                    [
                        inline_button(
                            text="❌ Bekor qilish",
                            callback_data="adm:cancel",
                            style="danger",
                            emoji_key="cancel",
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data == "adm:remove:confirm", AdminManagementStates.remove_confirm)
    async def remove_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                user = await session.scalar(
                    select(User).where(User.telegram_id == data.get("target_id")).with_for_update()
                )
                if user is None or user.admin_session_version != data.get("session_version"):
                    raise AdminActionError("Bu tasdiq eskirgan. Admin profilini qayta oching")
                await remove_admin(
                    session,
                    reference=str(user.telegram_id),
                    actor_telegram_id=callback.from_user.id,
                    superadmin_ids=get_settings().superadmin_ids,
                )
            await state.clear()
            await _answer(callback, "✅ Adminlik bekor qilindi.", admin_management_menu())
        except AdminActionError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.callback_query(F.data.startswith("adm:perm:"))
    async def permissions_open(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        target = _callback_id(callback.data)
        async with session_factory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == target))
            if user is None or not user.is_admin:
                await callback.answer("Admin topilmadi", show_alert=True)
                return
            selected = set(
                await session.scalars(
                    select(AdminPermission.permission).where(AdminPermission.user_id == user.id)
                )
            )
        ordered = sorted(ADMIN_PERMISSIONS)
        await state.set_state(AdminManagementStates.permissions)
        await state.update_data(
            target_id=target,
            session_version=user.admin_session_version,
            selected=sorted(selected),
        )
        await _answer(callback, "🛡 Admin huquqlari", _permissions_keyboard(ordered, selected))

    @router.callback_query(F.data.startswith("adm:pt:"), AdminManagementStates.permissions)
    async def permission_toggle(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        index = _callback_id(callback.data)
        ordered = sorted(ADMIN_PERMISSIONS)
        if index is None or not 0 <= index < len(ordered):
            await callback.answer("Eskirgan permission tugmasi", show_alert=True)
            return
        data = await state.get_data()
        selected = set(data.get("selected", []))
        permission = ordered[index]
        selected.symmetric_difference_update({permission})
        await state.update_data(selected=sorted(selected))
        await _answer(callback, "🛡 Admin huquqlari", _permissions_keyboard(ordered, selected))

    @router.callback_query(F.data == "adm:perm:save", AdminManagementStates.permissions)
    async def permissions_save(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                user = await session.scalar(
                    select(User).where(User.telegram_id == data.get("target_id")).with_for_update()
                )
                if user is None or user.admin_session_version != data.get("session_version"):
                    raise AdminActionError("Admin holati o‘zgargan. Huquqlarni qayta oching")
                await replace_admin_permissions(
                    session,
                    target_telegram_id=user.telegram_id,
                    permissions=set(data.get("selected", [])),
                    actor_telegram_id=callback.from_user.id,
                    superadmin_ids=get_settings().superadmin_ids,
                )
            await state.clear()
            await _answer(callback, "💾 Huquqlar saqlandi.", admin_management_menu())
        except AdminActionError as exc:
            await callback.answer(str(exc), show_alert=True)

    @router.callback_query(F.data == "adm:blocked")
    async def blocked(callback: CallbackQuery) -> None:
        if not await require_superadmin(callback):
            return
        async with session_factory() as session:
            users = list(
                await session.scalars(
                    select(User).where(User.is_admin.is_(True), User.admin_active.is_(False))
                )
            )
        text = "🚫 Bloklangan adminlar\n\n" + (
            "\n".join(f"• @{u.username or '-'} — {u.telegram_id}" for u in users)
            if users
            else "Bloklangan admin yo‘q."
        )
        await _answer(callback, text, admin_management_menu())

    @router.callback_query(F.data.in_({"adm:audit", "adm:rights"}))
    async def admin_audit(callback: CallbackQuery) -> None:
        if not await require_superadmin(callback):
            return
        if callback.data == "adm:rights":
            await _answer(
                callback,
                "🛡 Huquqlar admin kartasidan inline toggle orqali boshqariladi. "
                "Superadmin-only huquqlar oddiy adminga berilmaydi.",
                admin_management_menu(),
            )
            return
        async with session_factory() as session:
            rows = await list_business_audit(session, category="admins", limit=20)
        await _answer(
            callback,
            "\n\n".join(map(audit_text, rows)) or "Admin amallari yo‘q.",
            admin_management_menu(),
        )

    @router.callback_query(F.data.startswith("adm:actions:"))
    async def admin_actions(callback: CallbackQuery) -> None:
        if not await require_superadmin(callback):
            return
        target = _callback_id(callback.data)
        async with session_factory() as session:
            rows = await list_business_audit(
                session, category="admins", query=str(target), limit=20
            )
        await _answer(
            callback,
            "\n\n".join(map(audit_text, rows)) or "Muhim amal topilmadi.",
            admin_management_menu(),
        )

    @router.callback_query(F.data == "adm:search")
    async def search_start(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        await state.set_state(AdminManagementStates.search_reference)
        await _answer(callback, "Admin numeric ID yoki @username yuboring:")

    @router.message(AdminManagementStates.search_reference)
    async def search_input(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.from_user.id not in get_settings().superadmin_ids:
            return
        reference = (message.text or "").strip().lstrip("@").casefold()
        async with session_factory() as session:
            statement = select(User).where(User.is_admin.is_(True))
            statement = (
                statement.where(User.telegram_id == int(reference))
                if reference.isdecimal()
                else statement.where(User.username.ilike(reference))
            )
            user = await session.scalar(statement)
        await state.clear()
        if user is None:
            await message.answer("Admin topilmadi.", reply_markup=admin_management_menu())
            return
        await message.answer(
            f"Admin topildi: @{user.username or '-'} / {user.telegram_id}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        inline_button(
                            text="👁 Ko‘rish",
                            callback_data=f"adm:view:{user.telegram_id}",
                            emoji_key="view",
                        )
                    ]
                ]
            ),
        )

    @router.callback_query(F.data.in_({"adm:home", "adm:cancel"}))
    async def home(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_superadmin(callback):
            return
        await state.clear()
        await _answer(callback, "👥 Adminlar", admin_management_menu())

    return router


def _candidate_text(user: User) -> str:
    return (
        "Yangi admin:\n\n"
        f"Ism: {user.full_name or '-'}\n"
        f"Username: @{user.username or '-'}\n"
        f"Telegram ID: {user.telegram_id}\n"
        "Rol: ADMIN"
    )


def _admin_card_text(card) -> str:
    user = card.user
    added = user.admin_added_at.strftime("%d.%m.%Y %H:%M") if user.admin_added_at else "-"
    activity = user.last_activity_at.strftime("%d.%m.%Y %H:%M") if user.last_activity_at else "-"
    permissions = ", ".join(card.permissions) or "berilmagan"
    last = card.last_action.human_summary if card.last_action else "yo‘q"
    return (
        f"👤 @{user.username or 'username_yoq'}\n"
        f"ID: {user.telegram_id}\n"
        f"Ism: {user.full_name or '-'}\n"
        f"Rol: {user.role}\n"
        f"Holat: {'🟢 Faol' if user.admin_active else '⏸ Faolsiz'}\n"
        f"Huquqlar: {permissions}\n"
        f"Qo‘shgan: {user.admin_added_by_telegram_id or '-'}\n"
        f"Sana: {added}\n"
        f"Oxirgi faollik: {activity}\n"
        f"Ko‘rilayotgan cheklar: {card.active_review_count}\n"
        f"Oxirgi muhim amal: {last}"
    )


def _permissions_keyboard(ordered: list[str], selected: set[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            inline_button(
                text=f"{'✅' if item in selected else '❌'} {item}",
                callback_data=f"adm:pt:{index}",
                style="success" if item in selected else "danger",
                emoji_key="permissions",
            )
        ]
        for index, item in enumerate(ordered)
    ]
    rows.extend(
        [
            [
                inline_button(
                    text="💾 Saqlash",
                    callback_data="adm:perm:save",
                    style="success",
                    emoji_key="save",
                ),
                inline_button(
                    text="♻️ Bekor qilish",
                    callback_data="adm:cancel",
                    style="danger",
                    emoji_key="cancel",
                ),
            ]
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _callback_id(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return None


async def _answer(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=markup)
