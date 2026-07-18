from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import AdminPermission, User, WebSession
from app.db.session import session_factory

SESSION_COOKIE = "us_session"
CSRF_COOKIE = "us_csrf"


@dataclass(frozen=True, slots=True)
class SessionContext:
    user: User
    web_session: WebSession
    permissions: frozenset[str]
    is_superadmin: bool


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def create_web_session(session: AsyncSession, user: User) -> tuple[str, str, WebSession]:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = datetime.now(UTC)
    row = WebSession(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        csrf_hash=token_hash(csrf),
        admin_session_version=user.admin_session_version,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.web_session_ttl_seconds),
        last_seen_at=now,
    )
    session.add(row)
    await session.flush()
    return raw_token, csrf, row


async def current_session(request: Request) -> SessionContext:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Telegram orqali kiring")
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = await session.scalar(
            select(WebSession).where(WebSession.token_hash == token_hash(raw_token))
        )
        if row is None or row.revoked_at is not None or _expired(row.expires_at, now):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessiya tugagan")
        user = await session.get(User, row.user_id)
        if user is None or row.admin_session_version != user.admin_session_version:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessiya bekor qilingan")
        permissions = frozenset(
            await session.scalars(
                select(AdminPermission.permission).where(AdminPermission.user_id == user.id)
            )
        )
        row.last_seen_at = now
        await session.commit()
        return SessionContext(
            user=user,
            web_session=row,
            permissions=permissions,
            is_superadmin=user.telegram_id in get_settings().superadmin_ids,
        )


async def require_admin(request: Request, permission: str | None = None) -> SessionContext:
    context = await current_session(request)
    if not context.is_superadmin and not (context.user.is_admin and context.user.admin_active):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bu bo‘limga ruxsat yo‘q")
    if permission and not context.is_superadmin and permission not in context.permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Kerakli huquq berilmagan")
    return context


def verify_csrf(request: Request, context: SessionContext) -> None:
    header = request.headers.get("x-csrf-token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or not secrets.compare_digest(header, cookie):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF tekshiruvi muvaffaqiyatsiz")
    if not secrets.compare_digest(token_hash(header), context.web_session.csrf_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token noto‘g‘ri")


def _expired(value: datetime, now: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= now
