from __future__ import annotations

import pytest

from app import render_start


def test_migration_failure_does_not_start_gunicorn(monkeypatch) -> None:
    started = False

    def fail(awaitable) -> None:
        awaitable.close()
        raise RuntimeError("migration failed")

    def execv(_executable, _arguments) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(render_start.asyncio, "run", fail)
    monkeypatch.setattr(render_start.os, "execv", execv)
    with pytest.raises(RuntimeError, match="migration failed"):
        render_start.main()
    assert not started


def test_gunicorn_binds_render_port_after_migration(monkeypatch) -> None:
    command: list[str] = []
    coroutine = None

    def migrated(awaitable) -> None:
        nonlocal coroutine
        coroutine = awaitable

    def capture(_executable, arguments) -> None:
        command.extend(arguments)

    monkeypatch.setenv("PORT", "12345")
    monkeypatch.setattr(render_start.asyncio, "run", migrated)
    monkeypatch.setattr(render_start.os, "execv", capture)
    render_start.main()
    assert "0.0.0.0:12345" in command
    assert "app.web.asgi:app" in command
    coroutine.close()
