from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.bot.buttons import inline_button
from app.bot.payment_messages import (
    admin_card_message,
    admin_payment_message,
    format_som,
    payment_card_menu,
    payment_review_keyboard,
    receipt_request_message,
    receipt_upload_keyboard,
    topup_amount_prompt,
    topup_entry_keyboard,
)
from app.core.config import get_settings
from app.core.logging import logger
from app.db.models import Payment, PaymentCard, User
from app.db.session import session_factory
from app.services.audit import write_audit
from app.services.payments import (
    CardCipher,
    PaymentCardUnavailableError,
    PaymentError,
    PaymentValidationError,
    approve_payment,
    attach_payment_receipt,
    create_primary_card,
    create_topup_payment,
    get_admin_payment_card,
    get_user_payment_card,
    grant_review_payments,
    normalize_card_number,
    payment_actor,
    payment_review_recipients,
    reject_payment,
    replace_primary_card_number,
    set_primary_card_active,
    update_primary_card_holder,
    update_primary_card_limits,
)


class TopUpStates(StatesGroup):
    amount = State()
    receipt = State()


class PaymentCardStates(StatesGroup):
    number = State()
    holder = State()
    minimum = State()
    maximum = State()
    confirm_number = State()


class PaymentReviewStates(StatesGroup):
    adjusted_amount = State()


def build_payment_router() -> Router:
    router = Router(name="single_payment_card")

    @router.message(Command("topup"))
    @router.message(
        F.text.in_(
            {
                "💳 Hisob to‘ldirish",
                "💳 Hisob to'ldirish",
                "💳 Balansni to‘ldirish",
                "💳 Balansni to'ldirish",
            }
        )
    )
    async def start_topup(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await _begin_topup(message, telegram_id=message.from_user.id, state=state)

    @router.callback_query(F.data == "topup:start")
    async def start_topup_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message:
            source_message = callback.message
            with suppress(Exception):
                await source_message.delete()
            await _begin_topup(
                source_message,
                telegram_id=callback.from_user.id,
                state=state,
            )

    async def _begin_topup(message: Message, *, telegram_id: int, state: FSMContext) -> None:
        if await _is_admin(telegram_id):
            await state.clear()
            await message.answer("Admin akkauntlarida balans to‘ldirish mavjud emas.")
            return
        cipher = _card_cipher()
        if cipher is None:
            await _topup_unavailable(message)
            return
        try:
            async with session_factory.begin() as session:
                user = await _get_or_create_user(session, telegram_id)
                card = await get_user_payment_card(session, cipher=cipher)
            await state.clear()
            await state.update_data(user_id=user.id)
            await state.set_state(TopUpStates.amount)
            prompt = await message.answer(
                topup_amount_prompt(card), reply_markup=topup_entry_keyboard()
            )
            await state.update_data(prompt_message_id=prompt.message_id)
        except PaymentError:
            await _topup_unavailable(message)

    @router.callback_query(F.data == "topup:receipt")
    async def prompt_receipt(callback: CallbackQuery) -> None:
        await callback.answer("Chek rasmini yoki PDF faylini shu chatga yuboring.")

    @router.callback_query(F.data == "topup:cancel")
    async def cancel_topup(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Bekor qilindi")
        if callback.message:
            source_message = callback.message
            with suppress(Exception):
                await source_message.delete()
            await source_message.answer("Balans to‘ldirish bekor qilindi. /start")

    @router.message(TopUpStates.amount)
    async def accept_topup_amount(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        amount = _positive_amount(message.text)
        if amount is None:
            await message.answer("Summani musbat butun so‘m ko‘rinishida kiriting.")
            return
        cipher = _card_cipher()
        if cipher is None:
            await state.clear()
            await _topup_unavailable(message)
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                card = await get_user_payment_card(session, cipher=cipher)
                payment = await create_topup_payment(
                    session,
                    user_id=data["user_id"],
                    amount_som=amount,
                    cipher=cipher,
                )
            await state.update_data(payment_id=payment.id)
            await state.set_state(TopUpStates.receipt)
            prompt_message_id = data.get("prompt_message_id")
            if prompt_message_id:
                with suppress(Exception):
                    await message.bot.delete_message(message.chat.id, prompt_message_id)
            with suppress(Exception):
                await message.delete()
            await message.answer(
                receipt_request_message(
                    payment,
                    full_card_number=card.formatted_card_number,
                ),
                reply_markup=receipt_upload_keyboard(),
            )
        except PaymentValidationError:
            try:
                async with session_factory() as session:
                    card = await get_user_payment_card(session, cipher=cipher)
                await message.answer(
                    "Summa belgilangan chegaradan tashqarida. "
                    f"{format_som(card.min_topup_som)}–"
                    f"{format_som(card.max_topup_som)} so‘m kiriting."
                )
            except PaymentError:
                await state.clear()
                await _topup_unavailable(message)
        except PaymentError:
            await state.clear()
            await _topup_unavailable(message)

    @router.message(TopUpStates.receipt, F.photo | F.document)
    async def accept_receipt(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        receipt = _receipt_data(message)
        if receipt is None:
            await message.answer("Faqat JPEG, PNG rasm yoki PDF chek yuboring.")
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                payment = await attach_payment_receipt(
                    session,
                    payment_id=data["payment_id"],
                    user_id=data["user_id"],
                    **receipt,
                )
                user = await session.get(User, payment.user_id)
                recipients = await payment_review_recipients(
                    session, superadmin_ids=get_settings().superadmin_ids
                )
            await state.clear()
            await message.answer("Chek qabul qilindi va admin tekshiruviga yuborildi.")
            await _send_payment_review(
                message,
                payment=payment,
                user_telegram_id=user.telegram_id,
                recipients=recipients,
            )
        except PaymentValidationError as exc:
            await message.answer(f"Chek qabul qilinmadi: {exc}")

    @router.message(TopUpStates.receipt)
    async def reject_non_receipt(message: Message) -> None:
        await message.answer("Chek rasmini yoki PDF faylini yuboring.")

    @router.message(Command("payment_card"))
    @router.message(F.text == "💳 Asosiy karta")
    async def payment_card_command(message: Message) -> None:
        if message.from_user is None or not await _is_superadmin(message.from_user.id):
            await message.answer("Bu bo‘lim faqat superadmin uchun.")
            return
        await _show_card_menu_message(message)

    @router.message(Command("payment_review_grant"))
    async def grant_payment_review(message: Message) -> None:
        if message.from_user is None or not message.text:
            return
        try:
            target_telegram_id = int(message.text.split(maxsplit=1)[1])
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, message.from_user.id)
                await grant_review_payments(
                    session,
                    target_telegram_id=target_telegram_id,
                    actor=actor,
                )
            await message.answer("REVIEW_PAYMENTS huquqi berildi.")
        except (IndexError, ValueError, PaymentError) as exc:
            await message.answer(f"Foydalanish: /payment_review_grant TELEGRAM_ID\n{exc}")

    @router.callback_query(F.data == "card:show")
    async def show_card(callback: CallbackQuery) -> None:
        if not await _require_superadmin_callback(callback):
            return
        cipher = _card_cipher()
        if cipher is None:
            await callback.answer("Encryption key sozlanmagan", show_alert=True)
            return
        try:
            async with session_factory() as session:
                card = await get_admin_payment_card(session, cipher=cipher)
            await _callback_message(
                callback,
                admin_card_message(card),
                payment_card_menu(card_exists=True, active=card.active),
            )
        except PaymentError:
            await callback.answer("Asosiy karta sozlanmagan", show_alert=True)

    @router.callback_query(F.data == "card:number")
    async def request_card_number(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin_callback(callback):
            return
        async with session_factory() as session:
            exists = (
                await session.scalar(
                    select(PaymentCard.id).where(PaymentCard.singleton_key == "PRIMARY")
                )
                is not None
            )
        await state.clear()
        await state.update_data(card_mode="replace" if exists else "create")
        await state.set_state(PaymentCardStates.number)
        await _callback_message(
            callback,
            "Yangi karta raqamini yuboring. Xabar saqlangach o‘chirib tashlanadi.",
        )

    @router.message(PaymentCardStates.number)
    async def capture_card_number(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not await _is_superadmin(message.from_user.id):
            return
        try:
            number = normalize_card_number(message.text or "")
        except PaymentValidationError:
            await message.answer("Karta raqami yaroqsiz. Qayta kiriting.")
            return
        with suppress(Exception):
            await message.delete()
        data = await state.get_data()
        await state.update_data(card_number=number)
        if data["card_mode"] == "create":
            await state.update_data(holder_action="create")
            await state.set_state(PaymentCardStates.holder)
            await message.answer("Karta egasi nomini kiriting:")
            return
        await state.set_state(PaymentCardStates.confirm_number)
        await message.answer(
            f"Karta {number[:4]} **** **** {number[-4:]} raqamiga almashtirilsinmi?",
            reply_markup=_confirm_card_keyboard(),
        )

    @router.callback_query(F.data == "card:holder")
    async def request_card_holder(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin_callback(callback):
            return
        await state.clear()
        await state.update_data(holder_action="update")
        await state.set_state(PaymentCardStates.holder)
        await _callback_message(callback, "Yangi karta egasi nomini kiriting:")

    @router.message(PaymentCardStates.holder)
    async def capture_card_holder(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not await _is_superadmin(message.from_user.id):
            return
        holder = " ".join((message.text or "").split())
        if not 3 <= len(holder) <= 128:
            await message.answer("Karta egasi nomi yaroqsiz.")
            return
        data = await state.get_data()
        if data.get("holder_action") == "create":
            await state.update_data(card_holder_name=holder)
            await state.set_state(PaymentCardStates.minimum)
            await message.answer("Minimal to‘ldirish summasini kiriting:")
            return
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, message.from_user.id)
                await update_primary_card_holder(session, card_holder_name=holder, actor=actor)
            await state.clear()
            await message.answer("Karta egasi yangilandi.")
        except PaymentError as exc:
            await message.answer(f"Amal bajarilmadi: {exc}")

    @router.callback_query(F.data == "card:min")
    async def request_minimum(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin_callback(callback):
            return
        await state.clear()
        await state.update_data(limit_action="update_min")
        await state.set_state(PaymentCardStates.minimum)
        await _callback_message(callback, "Yangi minimal summani kiriting:")

    @router.message(PaymentCardStates.minimum)
    async def capture_minimum(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not await _is_superadmin(message.from_user.id):
            return
        amount = _positive_amount(message.text)
        if amount is None:
            await message.answer("Musbat butun summa kiriting.")
            return
        data = await state.get_data()
        if data.get("holder_action") == "create":
            await state.update_data(min_topup_som=amount)
            await state.set_state(PaymentCardStates.maximum)
            await message.answer("Maksimal to‘ldirish summasini kiriting:")
            return
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, message.from_user.id)
                await update_primary_card_limits(session, min_topup_som=amount, actor=actor)
            await state.clear()
            await message.answer("Minimal summa yangilandi.")
        except PaymentError as exc:
            await message.answer(f"Amal bajarilmadi: {exc}")

    @router.callback_query(F.data == "card:max")
    async def request_maximum(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin_callback(callback):
            return
        await state.clear()
        await state.update_data(limit_action="update_max")
        await state.set_state(PaymentCardStates.maximum)
        await _callback_message(callback, "Yangi maksimal summani kiriting:")

    @router.message(PaymentCardStates.maximum)
    async def capture_maximum(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not await _is_superadmin(message.from_user.id):
            return
        amount = _positive_amount(message.text)
        if amount is None:
            await message.answer("Musbat butun summa kiriting.")
            return
        data = await state.get_data()
        if data.get("holder_action") == "create":
            if amount < data["min_topup_som"]:
                await message.answer("Maksimal summa minimumdan kichik bo‘lmasin.")
                return
            await state.update_data(max_topup_som=amount)
            number = data["card_number"]
            await state.set_state(PaymentCardStates.confirm_number)
            await message.answer(
                f"Asosiy karta {number[:4]} **** **** {number[-4:]} sifatida yaratilsinmi?",
                reply_markup=_confirm_card_keyboard(),
            )
            return
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, message.from_user.id)
                await update_primary_card_limits(session, max_topup_som=amount, actor=actor)
            await state.clear()
            await message.answer("Maksimal summa yangilandi.")
        except PaymentError as exc:
            await message.answer(f"Amal bajarilmadi: {exc}")

    @router.callback_query(F.data == "card:confirm", PaymentCardStates.confirm_number)
    async def confirm_card_number(callback: CallbackQuery, state: FSMContext) -> None:
        if not await _require_superadmin_callback(callback):
            return
        cipher = _card_cipher()
        if cipher is None:
            await callback.answer("Encryption key sozlanmagan", show_alert=True)
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, callback.from_user.id)
                if data["card_mode"] == "create":
                    await create_primary_card(
                        session,
                        card_number=data["card_number"],
                        card_holder_name=data["card_holder_name"],
                        min_topup_som=data["min_topup_som"],
                        max_topup_som=data["max_topup_som"],
                        actor=actor,
                        cipher=cipher,
                    )
                else:
                    await replace_primary_card_number(
                        session,
                        new_card_number=data["card_number"],
                        actor=actor,
                        cipher=cipher,
                        confirmed=True,
                    )
            await state.clear()
            await _callback_message(callback, "Asosiy karta xavfsiz saqlandi.")
        except PaymentError as exc:
            await callback.answer(f"Saqlanmadi: {exc}", show_alert=True)

    @router.callback_query(F.data == "card:cancel")
    async def cancel_card_change(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _callback_message(callback, "Karta o‘zgarishi bekor qilindi.")

    @router.callback_query(F.data.in_({"card:disable", "card:enable"}))
    async def toggle_card(callback: CallbackQuery) -> None:
        if not await _require_superadmin_callback(callback):
            return
        active = callback.data == "card:enable"
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, callback.from_user.id)
                await set_primary_card_active(session, active=active, actor=actor)
            await _callback_message(
                callback, "Karta faollashtirildi." if active else "Karta vaqtincha yopildi."
            )
        except PaymentError as exc:
            await callback.answer(f"Amal bajarilmadi: {exc}", show_alert=True)

    @router.callback_query(F.data.startswith("pay:"))
    async def review_payment(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or not callback.data:
            return
        action, payment_id = _payment_callback(callback.data)
        if action is None:
            await callback.answer("Noto‘g‘ri amal", show_alert=True)
            return
        async with session_factory() as session:
            actor = await _payment_actor(session, callback.from_user.id)
        if not actor.can_review_payments:
            await callback.answer("REVIEW_PAYMENTS huquqi kerak", show_alert=True)
            return
        if action == "amount":
            await state.clear()
            await state.update_data(payment_id=payment_id)
            await state.set_state(PaymentReviewStates.adjusted_amount)
            await _callback_message(callback, "Tasdiqlanadigan aniq summani kiriting:")
            return
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, callback.from_user.id)
                payment = await session.get(Payment, payment_id)
                if payment is None:
                    raise PaymentValidationError("Payment not found")
                user = await session.get(User, payment.user_id)
                if action == "ok":
                    payment = await approve_payment(session, payment_id=payment_id, actor=actor)
                elif action == "no":
                    payment = await reject_payment(session, payment_id=payment_id, actor=actor)
                elif action == "info":
                    write_audit(
                        session,
                        actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
                        actor_id=str(actor.telegram_id),
                        action="PAYMENT_MORE_INFO_REQUESTED",
                        entity_type="PAYMENT",
                        entity_id=payment.id,
                        metadata={"card_last4": payment.card_number_last4_snapshot},
                    )
                elif action != "check":
                    raise PaymentValidationError("Unsupported payment action")
            if action == "ok":
                await callback.answer("To‘lov tasdiqlandi", show_alert=True)
                await callback.bot.send_message(
                    user.telegram_id,
                    f"Balansingiz {format_som(payment.approved_amount_som)} so‘mga to‘ldirildi.",
                )
            elif action == "no":
                await callback.answer("To‘lov rad etildi", show_alert=True)
                await callback.bot.send_message(user.telegram_id, "To‘lov cheki tasdiqlanmadi.")
            elif action == "info":
                await callback.answer("Userga so‘rov yuborildi", show_alert=True)
                await callback.bot.send_message(
                    user.telegram_id,
                    "To‘lov bo‘yicha qo‘shimcha ma’lumot kerak. Iltimos, support bilan bog‘laning.",
                )
            else:
                await callback.answer(
                    f"Holat: {payment.status}; summa: {format_som(payment.amount_som)} so‘m",
                    show_alert=True,
                )
        except PaymentError as exc:
            await callback.answer(f"Amal bajarilmadi: {exc}", show_alert=True)

    @router.message(PaymentReviewStates.adjusted_amount)
    async def approve_adjusted_amount(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        amount = _positive_amount(message.text)
        if amount is None:
            await message.answer("Musbat butun summa kiriting.")
            return
        data = await state.get_data()
        try:
            async with session_factory.begin() as session:
                actor = await _payment_actor(session, message.from_user.id)
                payment = await approve_payment(
                    session,
                    payment_id=data["payment_id"],
                    actor=actor,
                    approved_amount_som=amount,
                )
                user = await session.get(User, payment.user_id)
            await state.clear()
            await message.answer("To‘lov o‘zgartirilgan summa bilan tasdiqlandi.")
            await message.bot.send_message(
                user.telegram_id,
                f"Balansingiz {format_som(amount)} so‘mga to‘ldirildi.",
            )
        except PaymentError as exc:
            await message.answer(f"Amal bajarilmadi: {exc}")

    return router


def _card_cipher() -> CardCipher | None:
    secret = get_settings().payment_card_encryption_key
    if secret is None or not secret.get_secret_value():
        return None
    try:
        return CardCipher(secret.get_secret_value())
    except PaymentError:
        return None


async def _payment_actor(session, telegram_id: int):
    return await payment_actor(
        session,
        telegram_id=telegram_id,
        superadmin_ids=get_settings().superadmin_ids,
    )


async def _is_superadmin(telegram_id: int) -> bool:
    async with session_factory() as session:
        return (await _payment_actor(session, telegram_id)).is_superadmin


async def _is_admin(telegram_id: int) -> bool:
    if telegram_id in get_settings().superadmin_ids:
        return True
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    return bool(user and user.is_admin and user.admin_active)


async def _require_superadmin_callback(callback: CallbackQuery) -> bool:
    if callback.from_user is None or not await _is_superadmin(callback.from_user.id):
        await callback.answer("Bu amal faqat superadmin uchun", show_alert=True)
        return False
    return True


async def _get_or_create_user(session, telegram_id: int) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.flush()
    return user


async def _topup_unavailable(message: Message) -> None:
    await message.answer("Balans to‘ldirish vaqtincha mavjud emas")
    settings = get_settings()
    if not settings.superadmin_ids:
        return
    async with session_factory() as session:
        active_ids = set(
            await session.scalars(
                select(User.telegram_id).where(
                    User.telegram_id.in_(settings.superadmin_ids),
                    User.is_admin.is_(True),
                    User.admin_active.is_(True),
                )
            )
        )
    for telegram_id in active_ids:
        with suppress(Exception):
            await message.bot.send_message(
                telegram_id,
                "⚠️ Balans to‘ldirish uchun asosiy karta sozlanmagan yoki faol emas.",
            )


async def _send_payment_review(
    message: Message,
    *,
    payment: Payment,
    user_telegram_id: int,
    recipients: frozenset[int],
) -> None:
    settings = get_settings()
    targets = set(recipients)
    if settings.payment_review_chat_id is not None:
        targets.add(settings.payment_review_chat_id)
    text = admin_payment_message(payment, user_telegram_id=user_telegram_id)
    keyboard = payment_review_keyboard(payment.id)
    for target in targets:
        try:
            await message.bot.copy_message(
                chat_id=target,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            await message.bot.send_message(target, text, reply_markup=keyboard)
        except Exception:
            logger.warning(
                "payment_review_delivery_failed",
                payment_id=payment.id,
                recipient_id=target,
            )


async def _show_card_menu_message(message: Message) -> None:
    cipher = _card_cipher()
    if cipher is None:
        await message.answer(
            "Payment card encryption key sozlanmagan.",
            reply_markup=payment_card_menu(card_exists=False),
        )
        return
    try:
        async with session_factory() as session:
            card = await get_admin_payment_card(session, cipher=cipher)
        await message.answer(
            admin_card_message(card),
            reply_markup=payment_card_menu(card_exists=True, active=card.active),
        )
    except PaymentCardUnavailableError:
        await message.answer(
            "Asosiy karta hali kiritilmagan.",
            reply_markup=payment_card_menu(card_exists=False),
        )


async def _callback_message(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)


def _confirm_card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="✅ Tasdiqlash",
                    callback_data="card:confirm",
                    style="success",
                    emoji_key="confirm",
                ),
                inline_button(
                    text="❌ Bekor qilish",
                    callback_data="card:cancel",
                    style="danger",
                    emoji_key="cancel",
                ),
            ]
        ]
    )


def _positive_amount(value: str | None) -> int | None:
    text = (value or "").strip().replace(" ", "")
    return int(text) if text.isascii() and text.isdecimal() and int(text) > 0 else None


def _receipt_data(message: Message) -> dict | None:
    if message.photo:
        photo = message.photo[-1]
        return {
            "file_id": photo.file_id,
            "file_type": "PHOTO",
            "mime_type": "image/jpeg",
            "file_size": photo.file_size or 0,
        }
    document = message.document
    if document is None or document.mime_type not in {
        "application/pdf",
        "image/jpeg",
        "image/png",
    }:
        return None
    return {
        "file_id": document.file_id,
        "file_type": "PDF" if document.mime_type == "application/pdf" else "PHOTO",
        "mime_type": document.mime_type,
        "file_size": document.file_size or 0,
    }


def _payment_callback(data: str) -> tuple[str | None, str]:
    parts = data.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] != "pay"
        or parts[1]
        not in {
            "check",
            "ok",
            "amount",
            "no",
            "info",
        }
    ):
        return None, ""
    return parts[1], parts[2]
