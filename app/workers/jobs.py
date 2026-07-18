from datetime import UTC, datetime, timedelta

from aiogram import Bot
from sqlalchemy import select

from app.core.config import get_settings
from app.db.enums import ManualPriceStatus
from app.db.models import AuditLog, ManualProviderPrice
from app.db.session import session_factory
from app.integrations.providers.myxvest.client import MyxvestClient
from app.services.audit import write_audit
from app.services.manual_pricing import expire_manual_prices
from app.services.provider import ProviderWorkflow


async def startup(ctx: dict) -> None:
    settings = get_settings()
    if not settings.myxvest_enabled:
        ctx["workflow"] = None
        return
    client = MyxvestClient(
        base_url=settings.myxvest_base_url,
        api_key=settings.myxvest_api_key.get_secret_value(),
        timeout_seconds=settings.myxvest_timeout_seconds,
        max_retries=settings.myxvest_max_retries,
    )
    ctx["client"] = client
    ctx["workflow"] = ProviderWorkflow(
        session_factory,
        client,
        purchase_enabled=settings.myxvest_purchase_enabled,
        max_concurrency=settings.myxvest_max_concurrency,
        runtime_gate_required=True,
    )


async def shutdown(ctx: dict) -> None:
    client = ctx.get("client")
    if client is not None:
        await client.aclose()


async def balance_sync(ctx: dict) -> None:
    if workflow := ctx.get("workflow"):
        await workflow.sync_balance()
        await workflow.dispatch_pending()


async def pending_dispatch(ctx: dict) -> None:
    if workflow := ctx.get("workflow"):
        await workflow.dispatch_pending()


async def status_poll(ctx: dict) -> None:
    if workflow := ctx.get("workflow"):
        await workflow.poll_processing()


async def service_sync(ctx: dict) -> None:
    if workflow := ctx.get("workflow"):
        await workflow.sync_services()


async def reconciliation(ctx: dict) -> None:
    workflow = ctx.get("workflow")
    if workflow is None:
        return
    await workflow.reconcile_pending()


async def pricing_alerts(ctx: dict) -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None or not settings.superadmin_ids:
        return
    now = datetime.now(UTC)
    async with session_factory.begin() as session:
        expiring = list(
            await session.scalars(
                select(ManualProviderPrice).where(
                    ManualProviderPrice.active.is_(True),
                    ManualProviderPrice.valid_until.is_not(None),
                    ManualProviderPrice.valid_until <= now + timedelta(hours=1),
                )
            )
        )
        await expire_manual_prices(session, now=now)
        active_keys = set(
            await session.scalars(
                select(ManualProviderPrice.service_key).where(
                    ManualProviderPrice.active.is_(True),
                    ManualProviderPrice.status == ManualPriceStatus.ACTIVE,
                    (ManualProviderPrice.valid_until.is_(None))
                    | (ManualProviderPrice.valid_until > now),
                )
            )
        )
        required = {
            "MYXVEST:STARS",
            "MYXVEST:PREMIUM:3",
            "MYXVEST:PREMIUM:6",
            "MYXVEST:PREMIUM:12",
        }
        messages = [
            f"⚠️ Narx tugamoqda: {price.service_key}, v{price.version}." for price in expiring
        ]
        messages.extend(
            f"⚠️ Xizmat faol manual narxsiz: {service_key}."
            for service_key in sorted(required - active_keys)
        )
        recent = set(
            await session.scalars(
                select(AuditLog.entity_id).where(
                    AuditLog.action == "PRICING_ALERT_SENT",
                    AuditLog.created_at >= now - timedelta(minutes=55),
                )
            )
        )
        pending = [
            (str(index), message)
            for index, message in enumerate(messages)
            if str(index) not in recent
        ]
        for alert_id, message in pending:
            write_audit(
                session,
                actor_type="SYSTEM",
                actor_id=None,
                action="PRICING_ALERT_SENT",
                entity_type="PRICING_ALERT",
                entity_id=alert_id,
                metadata={"message": message},
            )
    if not pending:
        return
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        for _alert_id, message in pending:
            for telegram_id in settings.superadmin_ids:
                await bot.send_message(telegram_id, message)
    finally:
        await bot.session.close()
