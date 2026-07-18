from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers.jobs import (
    balance_sync,
    pending_dispatch,
    pricing_alerts,
    reconciliation,
    service_sync,
    shutdown,
    startup,
    status_poll,
)

settings = get_settings()


def cron_seconds(function, interval_seconds: int):
    if interval_seconds < 60 and 60 % interval_seconds == 0:
        return cron(function, second=set(range(0, 60, interval_seconds)))
    if interval_seconds % 60 == 0:
        interval_minutes = interval_seconds // 60
        if interval_minutes <= 60 and 60 % interval_minutes == 0:
            return cron(function, minute=set(range(0, 60, interval_minutes)), second={0})
    raise ValueError(
        "ARQ interval must evenly divide one minute or be a whole-minute divisor of one hour"
    )


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    functions = [
        balance_sync,
        pending_dispatch,
        status_poll,
        service_sync,
        reconciliation,
        pricing_alerts,
    ]
    cron_jobs = [
        cron_seconds(balance_sync, settings.myxvest_balance_sync_seconds),
        cron(pending_dispatch, second={10, 40}),
        cron_seconds(status_poll, settings.myxvest_status_poll_seconds),
        cron(reconciliation, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(service_sync, minute={0, 10, 20, 30, 40, 50}, second={15}),
        cron(pricing_alerts, minute={0, 30}, second={20}),
    ]
    max_jobs = 10
    job_timeout = 120
    health_check_interval = 30
