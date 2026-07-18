from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from prometheus_client import make_asgi_app
from redis.asyncio import from_url
from sqlalchemy import text

from app.bot.webhook import TelegramWebhook
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import session_factory
from app.services.bootstrap import bootstrap_defaults


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging()
    async with session_factory.begin() as session:
        await bootstrap_defaults(
            session,
            initial_admin_ids=settings.initial_admin_ids,
            superadmin_ids=settings.superadmin_ids,
            myxvest_enabled=settings.myxvest_enabled,
        )
    telegram = None
    if settings.telegram_bot_token is not None:
        telegram = TelegramWebhook(settings)
        await telegram.start()
        _app.state.telegram_webhook = telegram
    try:
        yield
    finally:
        if telegram is not None:
            await telegram.close()


_settings = get_settings()
app = FastAPI(
    title="Universal Service",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _settings.is_production else "/docs",
    redoc_url=None if _settings.is_production else "/redoc",
    openapi_url=None if _settings.is_production else "/openapi.json",
)
app.mount("/metrics", make_asgi_app())


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    database = "error"
    redis_status = "error"
    try:
        async with session_factory() as session:
            database = "ok" if int(await session.scalar(text("SELECT 1"))) == 1 else "error"
    except Exception:
        database = "error"
    client = from_url(get_settings().redis_url)
    try:
        redis_status = "ok" if await client.ping() else "error"
    except Exception:
        redis_status = "error"
    finally:
        await client.aclose()
    return {
        "status": "ok" if database == redis_status == "ok" else "degraded",
        "web": "running",
        "database": database,
        "redis": redis_status,
    }


@app.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    update: Update,
    request: Request,
    telegram_secret: str = Header(default="", alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, bool]:
    runtime: TelegramWebhook | None = getattr(request.app.state, "telegram_webhook", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telegram bot is unavailable")
    try:
        await runtime.feed(update, telegram_secret)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Telegram webhook rejected") from exc
    return {"ok": True}
