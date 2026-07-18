from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app
from redis.asyncio import from_url
from sqlalchemy import text

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
    yield


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
