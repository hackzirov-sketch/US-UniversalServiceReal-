from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.enums import ManualPriceStatus, OrderStatus, ProviderState
from app.db.models import (
    LedgerEntry,
    ManualProviderPrice,
    Order,
    PaymentCard,
    PreflightResult,
    Provider,
    RuntimeSetting,
    User,
)
from app.services.audit import write_audit

REAL_SALES_KEY = "real_sales_enabled"
PREFLIGHT_TTL = timedelta(minutes=15)


class SalesGateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreflightReport:
    result_id: str
    success: bool
    checks: dict[str, dict[str, Any]]
    expires_at: datetime

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(name for name, result in self.checks.items() if not result["ok"])


def confirmation_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def run_preflight(
    session: AsyncSession,
    *,
    settings: Settings,
    actor_telegram_id: int,
    infrastructure: dict[str, bool] | None = None,
    now: datetime | None = None,
) -> PreflightReport:
    current = now or datetime.now(UTC)
    probes = infrastructure or {}
    checks: dict[str, dict[str, Any]] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail[:300]}

    try:
        add("database", int(await session.scalar(select(1))) == 1, "PostgreSQL connection healthy")
    except Exception as exc:  # pragma: no cover - session failures vary by driver
        add("database", False, f"Database error: {type(exc).__name__}")

    migration = None
    try:
        migration = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    except Exception:
        migration = probes.get("migration_head") and settings.expected_alembic_head
    add(
        "migration_head",
        migration == settings.expected_alembic_head,
        f"current={migration or 'unknown'}, expected={settings.expected_alembic_head}",
    )
    add(
        "redis",
        probes.get("redis", False),
        "Redis PONG" if probes.get("redis") else "Redis unavailable",
    )
    add(
        "worker",
        probes.get("worker", False),
        "ARQ worker heartbeat" if probes.get("worker") else "Worker heartbeat missing",
    )
    add(
        "telegram",
        probes.get("telegram", False),
        "Bot token valid" if probes.get("telegram") else "Bot token not verified",
    )

    superadmin_count = (
        int(
            await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.telegram_id.in_(settings.superadmin_ids), User.is_admin.is_(True))
            )
            or 0
        )
        if settings.superadmin_ids
        else 0
    )
    add("superadmin", superadmin_count > 0, f"active bootstrap superadmins={superadmin_count}")
    card_count = int(
        await session.scalar(
            select(func.count()).select_from(PaymentCard).where(PaymentCard.active.is_(True))
        )
        or 0
    )
    add("payment_card", card_count > 0, f"active cards={card_count}")
    add(
        "payment_review_group",
        settings.payment_review_chat_id is not None,
        "configured" if settings.payment_review_chat_id else "missing",
    )
    add("topup_approval", probes.get("topup_approval", True), "approval service ready")

    active_keys = set(
        await session.scalars(
            select(ManualProviderPrice.service_key).where(
                ManualProviderPrice.active.is_(True),
                ManualProviderPrice.status == ManualPriceStatus.ACTIVE,
                ManualProviderPrice.valid_from <= current,
                (ManualProviderPrice.valid_until.is_(None))
                | (ManualProviderPrice.valid_until > current),
            )
        )
    )
    add("stars_price", "MYXVEST:STARS" in active_keys, "active manual Stars price")
    premium_ok = all(f"MYXVEST:PREMIUM:{months}" in active_keys for months in (3, 6, 12))
    add("premium_prices", premium_ok, "3/6/12 month package prices")
    add(
        "gift_prices",
        any(key.startswith("MYXVEST:GIFT:") for key in active_keys),
        "at least one active Gift",
    )

    provider = await session.scalar(select(Provider).where(Provider.code == "MYXVEST"))
    provider_ok = bool(provider and provider.enabled and provider.status == ProviderState.AVAILABLE)
    add(
        "provider", provider_ok, "Myxvest available" if provider_ok else "Myxvest disabled/degraded"
    )
    add(
        "provider_api_key",
        bool(settings.myxvest_api_key),
        "configured" if settings.myxvest_api_key else "missing",
    )
    add(
        "provider_balance",
        probes.get("provider_balance", False),
        "read-only balance verified" if probes.get("provider_balance") else "balance not verified",
    )
    purchase_client = bool(
        settings.myxvest_enabled and settings.myxvest_base_url.strip() and settings.myxvest_api_key
    )
    add("purchase_client", purchase_client, "configured" if purchase_client else "incomplete")
    add(
        "environment_gate",
        settings.myxvest_purchase_enabled,
        f"MYXVEST_PURCHASE_ENABLED={settings.myxvest_purchase_enabled}",
    )

    ledger_bad = int(
        await session.scalar(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.balance_after_som < 0)
        )
        or 0
    )
    add("ledger", ledger_bad == 0 and probes.get("ledger", True), f"invalid entries={ledger_bad}")
    unknown = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.internal_status.in_([OrderStatus.NEEDS_REVIEW, OrderStatus.PROVIDER_TIMEOUT])
            )
        )
        or 0
    )
    add("unknown_orders", unknown == 0, f"unknown/review orders={unknown}")
    reconcile = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.internal_status.in_([OrderStatus.SUBMITTING, OrderStatus.REFUND_PENDING]))
        )
        or 0
    )
    add("reconciliation", reconcile == 0, f"orders requiring reconciliation={reconcile}")
    backup_ok = bool(
        settings.backup_verified_at
        and settings.backup_verified_at.astimezone(UTC) >= current - timedelta(hours=24)
    )
    add(
        "backup",
        backup_ok,
        "backup verified in last 24h" if backup_ok else "fresh backup evidence missing",
    )
    add(
        "secrets_rotated",
        settings.secrets_rotated_after_compromise,
        "rotation confirmed"
        if settings.secrets_rotated_after_compromise
        else "rotation not confirmed",
    )
    add(
        "maintenance",
        not settings.maintenance_mode,
        "off" if not settings.maintenance_mode else "maintenance mode enabled",
    )
    add("circuit_breaker", provider_ok, "closed" if provider_ok else "provider circuit unavailable")

    success = all(result["ok"] for result in checks.values())
    expires_at = current + PREFLIGHT_TTL
    result = PreflightResult(
        requested_by_telegram_id=actor_telegram_id,
        success=success,
        checks=checks,
        created_at=current,
        expires_at=expires_at,
    )
    session.add(result)
    await session.flush()
    return PreflightReport(result.id, success, checks, expires_at)


async def runtime_sales_enabled(session: AsyncSession) -> bool:
    setting = await session.get(RuntimeSetting, REAL_SALES_KEY)
    return bool(setting and setting.bool_value)


async def set_runtime_sales(
    session: AsyncSession,
    *,
    enabled: bool,
    actor_telegram_id: int,
    superadmin_ids: frozenset[int],
    environment_enabled: bool,
    now: datetime | None = None,
) -> RuntimeSetting:
    if actor_telegram_id not in superadmin_ids:
        raise SalesGateError("Faqat superadmin real savdoni boshqara oladi")
    current = now or datetime.now(UTC)
    setting = await session.scalar(
        select(RuntimeSetting).where(RuntimeSetting.key == REAL_SALES_KEY).with_for_update()
    )
    if setting is None:
        setting = RuntimeSetting(key=REAL_SALES_KEY, bool_value=False)
        session.add(setting)
        await session.flush()
    if enabled:
        if not environment_enabled:
            raise SalesGateError("MYXVEST_PURCHASE_ENABLED=false")
        valid = await session.scalar(
            select(PreflightResult.id)
            .where(PreflightResult.success.is_(True), PreflightResult.expires_at > current)
            .order_by(PreflightResult.created_at.desc())
            .limit(1)
        )
        if valid is None:
            raise SalesGateError("Oxirgi 15 daqiqada muvaffaqiyatli preflight yo‘q")
    if setting.bool_value == enabled:
        return setting
    setting.bool_value = enabled
    setting.version += 1
    setting.updated_by_telegram_id = actor_telegram_id
    setting.updated_at = current
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor_telegram_id),
        action="REAL_SALES_ENABLED" if enabled else "REAL_SALES_DISABLED",
        entity_type="RUNTIME_SETTING",
        entity_id=REAL_SALES_KEY,
        summary=f"Real savdo {'yoqildi' if enabled else 'o‘chirildi'}",
        old_values={"enabled": not enabled},
        new_values={"enabled": enabled},
    )
    return setting


async def assert_purchase_gates(
    session: AsyncSession,
    *,
    environment_enabled: bool,
    now: datetime | None = None,
) -> None:
    if not environment_enabled:
        raise SalesGateError("Environment purchase gate yopiq")
    if not await runtime_sales_enabled(session):
        raise SalesGateError("Database runtime sales gate yopiq")
    current = now or datetime.now(UTC)
    valid = await session.scalar(
        select(PreflightResult.id)
        .where(PreflightResult.success.is_(True), PreflightResult.expires_at > current)
        .order_by(PreflightResult.created_at.desc())
        .limit(1)
    )
    if valid is None:
        raise SalesGateError("Muvaffaqiyatli preflight eskirgan yoki mavjud emas")
