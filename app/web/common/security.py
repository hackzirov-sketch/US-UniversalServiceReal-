from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from redis.asyncio import from_url
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
            "connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
        )
        if get_settings().is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


async def rate_limit(request: Request, scope: str = "web") -> None:
    settings = get_settings()
    host = request.client.host if request.client else "unknown"
    key = f"rate:{scope}:{host}"
    client = from_url(settings.redis_url, decode_responses=True)
    try:
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 60)
        if count > settings.web_rate_limit_per_minute:
            raise TooManyRequests
    except TooManyRequests:
        raise
    except Exception:
        return
    finally:
        await client.aclose()


class TooManyRequests(Exception):
    pass


async def too_many_requests_handler(_request: Request, _exc: TooManyRequests) -> JSONResponse:
    return JSONResponse({"detail": "Juda ko‘p so‘rov"}, status_code=429)
