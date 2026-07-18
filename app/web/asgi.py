from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.wsgi import WSGIMiddleware

from app.api.main import app
from app.core.config import get_settings
from app.web import create_app
from app.web.admin.api import router as admin_router
from app.web.common.api import router as auth_router
from app.web.common.security import (
    SecurityHeadersMiddleware,
    TooManyRequests,
    too_many_requests_handler,
)
from app.web.user.api import router as user_router

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(admin_router)
app.add_exception_handler(TooManyRequests, too_many_requests_handler)
app.add_middleware(SecurityHeadersMiddleware)

settings = get_settings()
allowed_origins = [
    item.strip() for item in settings.cors_allowed_origins.split(",") if item.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

trusted_hosts = [item.strip() for item in settings.trusted_hosts.split(",") if item.strip()]
if trusted_hosts and trusted_hosts != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

app.mount("/", WSGIMiddleware(create_app()))
