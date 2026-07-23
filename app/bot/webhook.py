from __future__ import annotations

import hashlib
import secrets
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from app.bot.admin_management import build_admin_management_router
from app.bot.button_design_admin import build_button_design_router
from app.bot.main import build_operational_router
from app.bot.menu import build_menu_router
from app.bot.middleware import IdentityMiddleware
from app.bot.payments import build_payment_router
from app.bot.pricing import build_pricing_router
from app.bot.production import build_production_router
from app.core.config import Settings
from app.db.session import session_factory
from app.services.button_design import load_button_design_cache


class TelegramWebhook:
    def __init__(self, settings: Settings) -> None:
        if settings.telegram_bot_token is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        token = settings.telegram_bot_token.get_secret_value()
        secret_key = settings.secret_key
        if secret_key is None:
            raise RuntimeError("SECRET_KEY is required for the Telegram webhook")

        self.secret_token = hashlib.sha256(
            f"{secret_key.get_secret_value()}:{token}".encode()
        ).hexdigest()
        self.webhook_url = _webhook_url(settings.user_webapp_url)
        self.bot = Bot(token)
        self.dispatcher = Dispatcher()
        identity = IdentityMiddleware()
        self.dispatcher.message.middleware(identity)
        self.dispatcher.callback_query.middleware(identity)

        self.dispatcher.include_router(build_menu_router())
        self.dispatcher.include_router(build_button_design_router())
        self.dispatcher.include_router(build_payment_router())
        self.dispatcher.include_router(build_pricing_router())
        self.dispatcher.include_router(build_admin_management_router())
        self.dispatcher.include_router(build_operational_router())
        self.dispatcher.include_router(build_production_router())

    async def start(self) -> None:
        async with session_factory() as session:
            await load_button_design_cache(session)
        await self.dispatcher.emit_startup(bot=self.bot)
        await self.bot.set_webhook(
            self.webhook_url,
            secret_token=self.secret_token,
            allowed_updates=self.dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )

    async def feed(self, update: Update, supplied_secret: str) -> None:
        if not secrets.compare_digest(supplied_secret, self.secret_token):
            raise PermissionError("Telegram webhook secret is invalid")
        await self.dispatcher.feed_update(self.bot, update)

    async def close(self) -> None:
        await self.dispatcher.emit_shutdown(bot=self.bot)
        await self.bot.session.close()


def _webhook_url(user_webapp_url: str) -> str:
    parsed = urlsplit(user_webapp_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("USER_WEBAPP_URL must be a public HTTPS URL for Telegram webhook mode")
    return f"{parsed.scheme}://{parsed.netloc}/telegram/webhook"
