from aiogram.types import InlineKeyboardMarkup

from app.bot.buttons import inline_button
from app.db.models import Payment
from app.services.payments import AdminPaymentCardView, UserPaymentCardView


def format_som(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def user_topup_card_message(card: UserPaymentCardView) -> str:
    instructions = f"\n\n{card.instructions}" if card.instructions else ""
    return (
        "💳 Balansni to‘ldirish\n\n"
        f"Karta: {card.formatted_card_number}\n"
        f"Karta egasi: {card.card_holder_name}\n"
        f"Minimal summa: {format_som(card.min_topup_som)} so‘m\n"
        f"Maksimal summa: {format_som(card.max_topup_som)} so‘m\n\n"
        f"To‘ldirmoqchi bo‘lgan summangizni kiriting.{instructions}"
    )


def topup_amount_prompt(card: UserPaymentCardView) -> str:
    return (
        "💳 Hisob to‘ldirish\n\n"
        f"Minimal summa: {format_som(card.min_topup_som)} so‘m\n"
        f"Maksimal summa: {format_som(card.max_topup_som)} so‘m\n\n"
        "To‘ldirmoqchi bo‘lgan summangizni kiriting."
    )


def receipt_request_message(payment: Payment, *, full_card_number: str) -> str:
    return (
        "💳 Hisob to‘ldirish\n\n"
        f"💳 {full_card_number}\n"
        f"👤 {payment.card_holder_name_snapshot}\n\n"
        f"Shu kartaga {format_som(payment.amount_som)} so‘m o‘tkazing.\n\n"
        "Pulni o‘tkazgach, chek rasmini yoki PDF faylini shu chatga yuboring.\n"
        "Bank ilovasidagi chekni oddiy rasm qilib ham yuborishingiz mumkin.\n\n"
        "Bot hech qachon PIN, CVV, SMS kod yoki bank parolini so‘ramaydi."
    )


def topup_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="❌ Bekor qilish",
                    callback_data="topup:cancel",
                    style="danger",
                    emoji_key="cancel",
                ),
                inline_button(text="🏠 Bosh menyu", callback_data="nav:home", emoji_key="home"),
            ]
        ]
    )


def receipt_upload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="📎 Chek yuborish",
                    callback_data="topup:receipt",
                    style="primary",
                    emoji_key="receipt",
                )
            ],
            [
                inline_button(
                    text="❌ Bekor qilish",
                    callback_data="topup:cancel",
                    style="danger",
                    emoji_key="cancel",
                ),
                inline_button(text="🏠 Bosh menyu", callback_data="nav:home", emoji_key="home"),
            ],
        ]
    )


def admin_payment_message(payment: Payment, *, user_telegram_id: int) -> str:
    return (
        "💳 Balans to‘ldirish tekshiruvi\n\n"
        f"Payment ID: {payment.id}\n"
        f"User: {user_telegram_id}\n"
        f"So‘ralgan summa: {format_som(payment.amount_som)} so‘m\n"
        f"Karta: {_payment_mask(payment)}\n"
        f"Karta egasi: {payment.card_holder_name_snapshot}\n"
        f"Holat: {payment.status}"
    )


def admin_card_message(card: AdminPaymentCardView) -> str:
    return (
        "💳 Asosiy karta\n\n"
        f"Karta: {card.masked_card_number}\n"
        f"Karta egasi: {card.card_holder_name}\n"
        f"Minimal summa: {format_som(card.min_topup_som)} so‘m\n"
        f"Maksimal summa: {format_som(card.max_topup_som)} so‘m\n"
        f"Holat: {'faol' if card.active else 'vaqtincha yopilgan'}"
    )


def payment_review_keyboard(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(
                    text="🔍 Tekshirish",
                    callback_data=f"pay:check:{payment_id}",
                    style="primary",
                    emoji_key="check",
                )
            ],
            [
                inline_button(
                    text="✅ Tasdiqlash",
                    callback_data=f"pay:ok:{payment_id}",
                    style="success",
                    emoji_key="confirm",
                )
            ],
            [
                inline_button(
                    text="✏️ Summani o‘zgartirib tasdiqlash",
                    callback_data=f"pay:amount:{payment_id}",
                    style="primary",
                    emoji_key="adjust_amount",
                )
            ],
            [
                inline_button(
                    text="❌ Rad etish",
                    callback_data=f"pay:no:{payment_id}",
                    style="danger",
                    emoji_key="reject",
                )
            ],
            [
                inline_button(
                    text="ℹ️ Qo‘shimcha ma’lumot",
                    callback_data=f"pay:info:{payment_id}",
                    emoji_key="info",
                )
            ],
        ]
    )


def payment_card_menu(*, card_exists: bool, active: bool = False) -> InlineKeyboardMarkup:
    if not card_exists:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    inline_button(
                        text="➕ Asosiy kartani kiritish",
                        callback_data="card:number",
                        style="success",
                        emoji_key="add_card",
                    )
                ]
            ]
        )
    rows = [
        [
            inline_button(
                text="👁 Karta ma’lumotlari", callback_data="card:show", emoji_key="card_details"
            )
        ],
        [
            inline_button(
                text="✏️ Karta raqamini almashtirish",
                callback_data="card:number",
                style="primary",
                emoji_key="card_number",
            )
        ],
        [
            inline_button(
                text="👤 Karta egasini o‘zgartirish",
                callback_data="card:holder",
                style="primary",
                emoji_key="card_holder",
            )
        ],
        [
            inline_button(
                text="📉 Minimal summani o‘zgartirish",
                callback_data="card:min",
                style="primary",
                emoji_key="minimum",
            )
        ],
        [
            inline_button(
                text="📈 Maksimal summani o‘zgartirish",
                callback_data="card:max",
                style="primary",
                emoji_key="maximum",
            )
        ],
        [
            inline_button(
                text="⏸ Kartani vaqtincha yopish" if active else "▶️ Kartani faollashtirish",
                callback_data="card:disable" if active else "card:enable",
                style="danger" if active else "success",
                emoji_key="disable" if active else "enable",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _payment_mask(payment: Payment) -> str:
    first4 = payment.card_number_first4_snapshot or "****"
    last4 = payment.card_number_last4_snapshot or "****"
    return f"{first4} **** **** {last4}"
