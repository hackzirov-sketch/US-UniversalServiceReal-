from __future__ import annotations

from flask import Flask, abort, render_template, request

from app.core.config import get_settings
from app.web.admin.routes import admin_blueprint
from app.web.user.routes import user_blueprint


def create_app() -> Flask:
    app = Flask(__name__, template_folder="common/templates")
    app.config.update(SECRET_KEY=None, MAX_CONTENT_LENGTH=10 * 1024 * 1024)
    app.register_blueprint(user_blueprint)
    app.register_blueprint(admin_blueprint)

    @app.before_request
    def maintenance_guard():
        if get_settings().maintenance_mode and not request.path.startswith(
            ("/app-static", "/admin-static")
        ):
            abort(503)

    for code in (401, 403, 404, 409, 429, 500, 503):
        app.register_error_handler(
            code,
            lambda error, status_code=code: (
                render_template(
                    "common/error.html",
                    code=status_code,
                    message=str(error),
                    admin=request.path.startswith("/admin"),
                ),
                status_code,
            ),
        )
    return app
