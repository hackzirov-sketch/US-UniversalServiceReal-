from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import User
from app.db.session import session_factory


class IdentityMiddleware(BaseMiddleware):
    """Ensure a User row exists and keep its Telegram @username fresh on every update.

    Storing the username is what makes @username-based admin lookup and the support
    reply relay possible; a username is only ever available once the user has
    interacted with the bot.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is not None and not tg_user.is_bot:
            await self._sync(tg_user.id, tg_user.username, tg_user.full_name)
        return await handler(event, data)

    @staticmethod
    async def _sync(telegram_id: int, username: str | None, full_name: str | None) -> None:
        username = username or None
        async with session_factory() as session:
            row = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if row is not None and row.username == username and row.full_name == full_name:
                return
            if row is None:
                session.add(User(telegram_id=telegram_id, username=username, full_name=full_name))
            else:
                row.username = username
                row.full_name = full_name
                row.last_activity_at = datetime.now(UTC)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
