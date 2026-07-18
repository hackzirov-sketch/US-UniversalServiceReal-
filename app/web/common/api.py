from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import TelegramAuthReplay, User, WebSession
from app.db.session import session_factory
from app.web.common.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_web_session,
    current_session,
    token_hash,
    verify_csrf,
)
from app.web.common.security import rate_limit
from app.web.common.telegram_auth import TelegramAuthError, verify_init_data

router = APIRouter(prefix="/web-api/auth", tags=["web-auth"])


class TelegramAuthBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=8192)


@router.post("/telegram")
async def telegram_login(body: TelegramAuthBody, request: Request, response: Response):
    await rate_limit(request, "telegram-auth")
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Bot sozlanmagan")
    try:
        identity = verify_init_data(
            body.init_data,
            bot_token=settings.telegram_bot_token.get_secret_value(),
            max_age_seconds=settings.telegram_auth_max_age_seconds,
        )
    except TelegramAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None
    now = datetime.now(UTC)
    async with session_factory.begin() as session:
        await session.execute(
            delete(TelegramAuthReplay).where(TelegramAuthReplay.expires_at <= now)
        )
        session.add(
            TelegramAuthReplay(
                init_data_hash=identity.init_data_hash,
                telegram_id=identity.telegram_id,
                created_at=now,
                expires_at=now + timedelta(seconds=settings.telegram_auth_max_age_seconds),
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Telegram kirish ma’lumoti ishlatilgan"
            ) from None
        user = await session.scalar(
            select(User).where(User.telegram_id == identity.telegram_id).with_for_update()
        )
        if user is None:
            user = User(
                telegram_id=identity.telegram_id,
                username=identity.username,
                full_name=identity.full_name,
                last_activity_at=now,
            )
            session.add(user)
            await session.flush()
        else:
            user.username = identity.username
            user.full_name = identity.full_name
            user.last_activity_at = now
        old = request.cookies.get(SESSION_COOKIE)
        if old:
            previous = await session.scalar(
                select(WebSession).where(WebSession.token_hash == token_hash(old)).with_for_update()
            )
            if previous and previous.revoked_at is None:
                previous.revoked_at = now
        raw_token, csrf, _ = await create_web_session(session, user)
    secure = settings.is_production
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.web_session_ttl_seconds,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=secure,
        samesite="lax",
        max_age=settings.web_session_ttl_seconds,
        path="/",
    )
    is_superadmin = user.telegram_id in settings.superadmin_ids
    return {
        "ok": True,
        "role": "SUPERADMIN" if is_superadmin else user.role,
        "is_admin": is_superadmin or (user.is_admin and user.admin_active),
    }


@router.get("/me")
async def me(request: Request):
    context = await current_session(request)
    return {
        "telegram_id": context.user.telegram_id,
        "username": context.user.username,
        "full_name": context.user.full_name,
        "role": "SUPERADMIN" if context.is_superadmin else context.user.role,
        "is_admin": context.is_superadmin or (context.user.is_admin and context.user.admin_active),
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    context = await current_session(request)
    verify_csrf(request, context)
    async with session_factory.begin() as session:
        row = await session.get(WebSession, context.web_session.id, with_for_update=True)
        if row:
            row.revoked_at = datetime.now(UTC)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}
