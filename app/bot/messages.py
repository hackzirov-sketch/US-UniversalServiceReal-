from app.db.enums import OrderStatus

USER_STATUS_MESSAGES = {
    OrderStatus.AWAITING_PROVIDER_FUNDING: (
        "✅ To‘lovingiz tasdiqlandi.\n\n"
        "Buyurtmangiz xizmat balansini to‘ldirish navbatida.\n"
        "Buyurtma raqami: #{number}\n\n"
        "Balans to‘ldirilgach, buyurtma avtomatik yuboriladi."
    ),
    OrderStatus.PROCESSING: "⏳ Buyurtmangiz bajarilmoqda.",
    OrderStatus.COMPLETED: "✅ Buyurtmangiz muvaffaqiyatli bajarildi.",
    OrderStatus.PRICE_CHANGED: (
        "⚠️ Xizmat tannarxi o‘zgardi. Buyurtma admin tekshiruviga yuborildi."
    ),
    OrderStatus.REFUNDED: (
        "↩️ Buyurtma bajarilmagani sababli mablag‘ ichki balansingizga qaytarildi."
    ),
}

PURCHASE_DISABLED_MESSAGE = "Xizmat narxlari tayyorlanmoqda. Xarid vaqtincha yopiq."


def user_order_message(status: OrderStatus, public_number: str) -> str:
    template = USER_STATUS_MESSAGES.get(status, f"Buyurtma holati: {status.value}")
    return template.format(number=public_number)
