from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

LOCK_ID = 8_201_824_177


def asyncpg_dsn(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def migrate() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    connection = None
    for attempt in range(30):
        try:
            connection = await asyncpg.connect(asyncpg_dsn(database_url), timeout=5)
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 29:
                raise
            await asyncio.sleep(2)
    assert connection is not None
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "alembic", "upgrade", "head"
        )
        returncode = await process.wait()
        if returncode:
            raise RuntimeError(f"alembic upgrade head failed: {returncode}")
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)
        await connection.close()


def main() -> None:
    asyncio.run(migrate())
    port = os.environ.get("PORT", "10000")
    os.execv(  # noqa: S606
        sys.executable,
        [
            sys.executable,
            "-m",
            "gunicorn",
            "app.web.asgi:app",
            "--worker-class",
            "uvicorn.workers.UvicornWorker",
            "--bind",
            f"0.0.0.0:{port}",
            "--workers",
            "1",
            "--timeout",
            "120",
            "--access-logfile",
            "-",
            "--error-logfile",
            "-",
        ],
    )


if __name__ == "__main__":
    main()
