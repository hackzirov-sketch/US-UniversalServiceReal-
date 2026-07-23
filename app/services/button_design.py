from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ButtonDesign
from app.services.audit import write_audit

ButtonStyleName = Literal["default", "primary", "success", "danger"]
ALLOWED_BUTTON_STYLES = frozenset({"default", "primary", "success", "danger"})


class ButtonDesignError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    key: str
    text: str
    unicode_fallback: str


BUTTON_SPECS = {
    spec.key: spec
    for spec in (
        ButtonSpec("contest", "🎁 Konkursda ishtirok etish", "🎁"),
        ButtonSpec("topup", "💳 Hisob to‘ldirish", "💳"),
        ButtonSpec("account", "👛 Hisobim", "👛"),
        ButtonSpec("stars", "⭐ Stars olish", "⭐"),
        ButtonSpec("premium", "💎 Premium olish", "💎"),
        ButtonSpec("gift", "🎁 Gift olish", "🎁"),
        ButtonSpec("farm", "🌱 Ferma", "🌱"),
        ButtonSpec("rating", "🏆 Reyting", "🏆"),
        ButtonSpec("points", "🎯 Ballarim", "🎯"),
        ButtonSpec("bonuses", "🎁 Bonuslarim", "🎁"),
        ButtonSpec("orders", "📦 Buyurtmalarim", "📦"),
        ButtonSpec("profile", "👤 Profil", "👤"),
        ButtonSpec("help", "ℹ️ Yordam", "ℹ️"),
        ButtonSpec("pricing", "💰 Narxlar", "💰"),
        ButtonSpec("payment_review", "🧾 To‘lov review", "🧾"),
        ButtonSpec("admin_orders", "📦 Buyurtmalar", "📦"),
        ButtonSpec("card", "💳 Asosiy karta", "💳"),
        ButtonSpec("admins", "👥 Adminlar", "👥"),
        ButtonSpec("audit", "📜 Audit", "📜"),
        ButtonSpec("button_design", "🎨 Tugmalar dizayni", "🎨"),
        ButtonSpec("confirm", "✅ Tasdiqlash", "✅"),
        ButtonSpec("reject", "❌ Rad etish", "❌"),
        ButtonSpec("cancel", "❌ Bekor qilish", "❌"),
        ButtonSpec("enable", "▶️ Faollashtirish", "▶️"),
        ButtonSpec("disable", "⏸ O‘chirish", "⏸"),
        ButtonSpec("home", "🏠 Bosh menyu", "🏠"),
        ButtonSpec("back", "◀️ Orqaga", "◀️"),
        ButtonSpec("save", "💾 Saqlash", "💾"),
        ButtonSpec("edit", "✏️ Tahrirlash", "✏️"),
        ButtonSpec("view", "👁 Ko‘rish", "👁"),
        ButtonSpec("search", "🔎 Qidirish", "🔎"),
        ButtonSpec("history", "📜 Tarix", "📜"),
        ButtonSpec("sync", "🔄 Yangilash", "🔄"),
        ButtonSpec("add_admin", "➕ Yangi admin", "➕"),
        ButtonSpec("admin_list", "📋 Adminlar ro‘yxati", "📋"),
        ButtonSpec("permissions", "🛡 Huquqlar", "🛡"),
        ButtonSpec("blocked_admins", "🚫 Bloklanganlar", "🚫"),
        ButtonSpec("admin_actions", "📜 Admin amallari", "📜"),
        ButtonSpec("remove_admin", "🗑 Adminlikdan olish", "🗑"),
        ButtonSpec("telegram_id", "🆔 Telegram ID orqali", "🆔"),
        ButtonSpec("username", "👤 Username orqali", "👤"),
        ButtonSpec("real_sales", "🚀 Real savdo", "🚀"),
        ButtonSpec("preflight", "🔍 Preflight tekshirish", "🔍"),
        ButtonSpec("controlled_test", "🧪 Nazoratli test buyurtma", "🧪"),
        ButtonSpec("active_prices", "📋 Faol narxlar", "📋"),
        ButtonSpec("price_history", "🕓 Narxlar tarixi", "🕓"),
        ButtonSpec("unpriced_services", "⚠️ Narxsiz xizmatlar", "⚠️"),
        ButtonSpec("quick_adjust", "➕ Tez o‘zgartirish", "➕"),
        ButtonSpec("deactivate", "⏸ Xizmatni yopish", "⏸"),
        ButtonSpec("unicode_fallback", "🙂 Unicode fallback", "🙂"),
        ButtonSpec("receipt", "📎 Chek yuborish", "📎"),
        ButtonSpec("check", "🔍 Tekshirish", "🔍"),
        ButtonSpec("adjust_amount", "✏️ Summani o‘zgartirish", "✏️"),
        ButtonSpec("info", "ℹ️ Qo‘shimcha ma’lumot", "ℹ️"),
        ButtonSpec("add_card", "➕ Asosiy kartani kiritish", "➕"),
        ButtonSpec("card_details", "👁 Karta ma’lumotlari", "👁"),
        ButtonSpec("card_number", "✏️ Karta raqami", "✏️"),
        ButtonSpec("card_holder", "👤 Karta egasi", "👤"),
        ButtonSpec("minimum", "📉 Minimal summa", "📉"),
        ButtonSpec("maximum", "📈 Maksimal summa", "📈"),
        ButtonSpec("open_webapp", "🚀 UniversalService’ni ochish", "🚀"),
        ButtonSpec("open_admin_webapp", "🛡 Admin panelni ochish", "🛡"),
    )
}

_design_cache: dict[str, ButtonDesign] = {}


def get_cached_button_design(button_key: str) -> ButtonDesign | None:
    return _design_cache.get(button_key.casefold())


def clear_button_design_cache() -> None:
    _design_cache.clear()


async def load_button_design_cache(session: AsyncSession) -> int:
    rows = list(await session.scalars(select(ButtonDesign)))
    _design_cache.clear()
    _design_cache.update({row.button_key: row for row in rows})
    return len(rows)


async def save_button_design(
    session: AsyncSession,
    *,
    button_key: str,
    button_style: str,
    custom_emoji_id: str | None,
    actor_telegram_id: int,
) -> ButtonDesign:
    spec = _spec(button_key)
    style = button_style.casefold()
    if style not in ALLOWED_BUTTON_STYLES:
        raise ButtonDesignError("Telegram tugma rangi noto‘g‘ri")
    emoji_id = _custom_emoji_id(custom_emoji_id)
    row = await session.get(ButtonDesign, spec.key)
    if row is None:
        row = ButtonDesign(button_key=spec.key)
        session.add(row)
    row.button_text = spec.text
    row.button_style = style
    row.custom_emoji_id = emoji_id
    row.unicode_emoji_fallback = spec.unicode_fallback
    row.updated_by = actor_telegram_id
    row.updated_at = datetime.now(UTC)
    await session.flush()
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="BUTTON_DESIGN_UPDATED",
        entity_type="BUTTON_DESIGN",
        entity_id=spec.key,
        metadata={"button_style": style, "custom_emoji_id": emoji_id},
    )
    _design_cache[spec.key] = row
    return row


async def reset_button_design(
    session: AsyncSession, *, button_key: str, actor_telegram_id: int
) -> None:
    spec = _spec(button_key)
    row = await session.get(ButtonDesign, spec.key)
    if row is not None:
        await session.delete(row)
        await session.flush()
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="BUTTON_DESIGN_RESET",
        entity_type="BUTTON_DESIGN",
        entity_id=spec.key,
        metadata={},
    )
    _design_cache.pop(spec.key, None)


def _spec(button_key: str) -> ButtonSpec:
    spec = BUTTON_SPECS.get(button_key.casefold())
    if spec is None:
        raise ButtonDesignError("Noma’lum tugma")
    return spec


def _custom_emoji_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized.isascii() or not normalized.isdecimal():
        raise ButtonDesignError("Custom emoji ID raqamlardan iborat bo‘lishi kerak")
    return normalized
