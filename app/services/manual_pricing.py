from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ManualPriceStatus, PriceSource, ServiceType
from app.db.models import (
    AdminPermission,
    ManualPriceSequence,
    ManualProviderPrice,
    PriceQuote,
    PricingRule,
    Provider,
    ProviderService,
    User,
)
from app.services.audit import write_audit
from app.services.pricing import calculate_quote

MANAGE_PRICING = "MANAGE_PRICING"
ALLOWED_DURATIONS_HOURS = {1, 3, 6, 12, 24, None}
PREMIUM_MONTHS = {3, 6, 12}


class ManualPricingError(ValueError):
    code = "MANUAL_PRICING_ERROR"


class ManualPriceBelowCostError(ManualPricingError):
    code = "MANUAL_PRICE_BELOW_COST"


class ManualPriceValidationError(ManualPricingError):
    code = "MANUAL_PRICE_VALIDATION"


class PricingPermissionError(ManualPricingError):
    code = "PRICING_PERMISSION_DENIED"


class PriceTemporarilyUnavailableError(ManualPricingError):
    code = "TEMPORARILY_UNAVAILABLE"


class PurchaseDisabledError(ManualPricingError):
    code = "MYXVEST_PURCHASE_DISABLED"


@dataclass(frozen=True, slots=True)
class PricingActor:
    telegram_id: int
    is_superadmin: bool
    can_manage_pricing: bool


@dataclass(frozen=True, slots=True)
class ManualPriceInput:
    service_type: ServiceType
    provider_cost_som: int
    sale_price_som: int
    display_name: str
    min_quantity: int | None = None
    max_quantity: int | None = None
    premium_months: int | None = None
    gift_name: str | None = None
    allow_comment: bool = False
    sort_order: int = 0
    active: bool = True
    duration_hours: int | None = 24
    source_note: str | None = None


@dataclass(frozen=True, slots=True)
class PricePreview:
    provider_cost_som: int
    sale_price_som: int
    profit_som: int
    profit_percent_bps: int
    low_profit: bool

    @property
    def profit_percent_text(self) -> str:
        return f"{self.profit_percent_bps // 100}.{self.profit_percent_bps % 100:02d}%"


async def pricing_actor(
    session: AsyncSession, *, telegram_id: int, superadmin_ids: frozenset[int]
) -> PricingActor:
    if telegram_id in superadmin_ids:
        return PricingActor(telegram_id, True, True)
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None or not user.is_admin or not user.admin_active:
        return PricingActor(telegram_id, False, False)
    permission = await session.scalar(
        select(AdminPermission.id).where(
            AdminPermission.user_id == user.id,
            AdminPermission.permission == MANAGE_PRICING,
        )
    )
    return PricingActor(telegram_id, False, permission is not None)


async def grant_manage_pricing(
    session: AsyncSession,
    *,
    target_telegram_id: int,
    actor: PricingActor,
) -> None:
    if not actor.is_superadmin:
        raise PricingPermissionError("Only a superadmin can grant pricing permission")
    user = await session.scalar(select(User).where(User.telegram_id == target_telegram_id))
    if user is None or not user.is_admin or not user.admin_active:
        raise ManualPriceValidationError("Target must be an existing admin")
    existing = await session.scalar(
        select(AdminPermission).where(
            AdminPermission.user_id == user.id,
            AdminPermission.permission == MANAGE_PRICING,
        )
    )
    if existing is None:
        session.add(
            AdminPermission(
                user_id=user.id,
                permission=MANAGE_PRICING,
                granted_by_telegram_id=actor.telegram_id,
            )
        )
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor.telegram_id),
        action="MANAGE_PRICING_GRANTED",
        entity_type="USER",
        entity_id=user.id,
        metadata={"target_telegram_id": target_telegram_id},
    )


def normalize_gift_name(value: str) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ManualPriceValidationError("gift_name must not be empty")
    if len(normalized) > 128:
        raise ManualPriceValidationError("gift_name is too long")
    return normalized


def gift_service_slug(value: str) -> str:
    normalized = normalize_gift_name(value).casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized, flags=re.ASCII).strip("_")
    if not slug:
        # Stable UTF-8 hex fallback avoids collisions caused by dropping non-ASCII names.
        slug = normalized.encode("utf-8").hex()
    return slug


def service_key_for(data: ManualPriceInput) -> str:
    if data.service_type == ServiceType.STARS:
        return "MYXVEST:STARS"
    if data.service_type == ServiceType.PREMIUM:
        return f"MYXVEST:PREMIUM:{data.premium_months}"
    return f"MYXVEST:GIFT:{gift_service_slug(data.gift_name or '')}"


def validate_price_input(data: ManualPriceInput) -> None:
    if data.provider_cost_som <= 0 or data.sale_price_som <= 0:
        raise ManualPriceValidationError("Prices must be positive integer UZS values")
    if data.sale_price_som < data.provider_cost_som:
        raise ManualPriceBelowCostError("MANUAL_PRICE_BELOW_COST")
    if data.duration_hours not in ALLOWED_DURATIONS_HOURS:
        raise ManualPriceValidationError("Duration must be 1, 3, 6, 12, 24 hours or unlimited")
    if not data.display_name.strip():
        raise ManualPriceValidationError("display_name must not be empty")
    if data.sort_order < 0:
        raise ManualPriceValidationError("sort_order must not be negative")
    if data.service_type == ServiceType.STARS:
        if data.min_quantity is None or data.min_quantity < 50:
            raise ManualPriceValidationError("Stars minimum must be at least 50")
        if data.max_quantity is None or data.max_quantity > 10_000:
            raise ManualPriceValidationError("Stars maximum must not exceed 10000")
        if data.min_quantity > data.max_quantity:
            raise ManualPriceValidationError("Minimum must not exceed maximum")
    elif data.service_type == ServiceType.PREMIUM:
        if data.premium_months not in PREMIUM_MONTHS:
            raise ManualPriceValidationError("Premium months must be 3, 6 or 12")
    elif data.service_type == ServiceType.GIFT:
        normalize_gift_name(data.gift_name or "")


def preview_price(
    data: ManualPriceInput,
    *,
    min_profit_percent: Decimal,
    min_profit_som: int,
) -> PricePreview:
    validate_price_input(data)
    profit = data.sale_price_som - data.provider_cost_som
    percent_bps = profit * 10_000 // data.provider_cost_som
    minimum_bps = int(min_profit_percent * 100)
    return PricePreview(
        provider_cost_som=data.provider_cost_som,
        sale_price_som=data.sale_price_som,
        profit_som=profit,
        profit_percent_bps=percent_bps,
        low_profit=profit < min_profit_som or percent_bps < minimum_bps,
    )


def calculate_sale_from_original(
    original_price_som: int,
    *,
    minimum_profit_per_unit_som: int = 0,
    percentage_markup: Decimal = Decimal("0"),
    fixed_markup_som: int = 0,
) -> int:
    if original_price_som <= 0:
        raise ManualPriceValidationError("Original narx musbat bo‘lishi kerak")
    if minimum_profit_per_unit_som < 0 or percentage_markup < 0 or fixed_markup_som < 0:
        raise ManualPriceValidationError("Ustama qiymatlari manfiy bo‘lmasligi kerak")
    percentage_value = (
        Decimal(original_price_som) * (Decimal("1") + percentage_markup / Decimal("100"))
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(
        original_price_som + minimum_profit_per_unit_som,
        int(percentage_value),
        original_price_som + fixed_markup_som,
    )


def quick_adjust_price(
    value_som: int, *, delta_som: int = 0, percent: Decimal = Decimal("0")
) -> int:
    if value_som <= 0:
        raise ManualPriceValidationError("Narx musbat bo‘lishi kerak")
    adjusted = (Decimal(value_som) * (Decimal("1") + percent / Decimal("100"))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    ) + delta_som
    if adjusted <= 0:
        raise ManualPriceValidationError("Natijaviy narx musbat bo‘lishi kerak")
    return int(adjusted)


async def create_manual_price(
    session: AsyncSession,
    *,
    data: ManualPriceInput,
    actor: PricingActor,
    requires_superadmin_approval: bool,
    min_profit_percent: Decimal,
    min_profit_som: int,
    now: datetime | None = None,
) -> ManualProviderPrice:
    if not actor.can_manage_pricing:
        raise PricingPermissionError("MANAGE_PRICING permission is required")
    preview = preview_price(
        data,
        min_profit_percent=min_profit_percent,
        min_profit_som=min_profit_som,
    )
    current_time = now or datetime.now(UTC)
    provider = await session.scalar(
        select(Provider).where(Provider.code == "MYXVEST").with_for_update()
    )
    if provider is None:
        raise ManualPriceValidationError("MYXVEST provider is not configured")
    service_key = service_key_for(data)
    previous = await session.scalar(
        select(ManualProviderPrice)
        .where(ManualProviderPrice.provider_id == provider.id)
        .where(ManualProviderPrice.service_key == service_key)
        .order_by(ManualProviderPrice.version.desc())
        .limit(1)
    )
    version = await _next_version(session, provider.id, service_key)
    needs_approval = not actor.is_superadmin and (
        requires_superadmin_approval or preview.low_profit
    )
    activate = data.active and not needs_approval
    if activate:
        await session.execute(
            update(ManualProviderPrice)
            .where(
                ManualProviderPrice.provider_id == provider.id,
                ManualProviderPrice.service_key == service_key,
                ManualProviderPrice.active.is_(True),
            )
            .values(active=False, status=ManualPriceStatus.INACTIVE)
        )
    gift_name = normalize_gift_name(data.gift_name) if data.gift_name else None
    record = ManualProviderPrice(
        provider_id=provider.id,
        service_type=data.service_type,
        service_key=service_key,
        display_name=data.display_name.strip(),
        provider_cost_som=data.provider_cost_som,
        sale_price_som=data.sale_price_som,
        unit_type="PER_STAR" if data.service_type == ServiceType.STARS else "FIXED",
        min_quantity=data.min_quantity,
        max_quantity=data.max_quantity,
        premium_months=data.premium_months,
        gift_name=gift_name,
        allow_comment=data.allow_comment,
        sort_order=data.sort_order,
        active=activate,
        status=ManualPriceStatus.ACTIVE
        if activate
        else (ManualPriceStatus.DRAFT if needs_approval else ManualPriceStatus.INACTIVE),
        version=version,
        valid_from=current_time,
        valid_until=current_time + timedelta(hours=data.duration_hours)
        if data.duration_hours is not None
        else None,
        source_note=data.source_note.strip() if data.source_note else None,
        created_by_admin_id=actor.telegram_id,
        approved_by_admin_id=actor.telegram_id if actor.is_superadmin and activate else None,
    )
    session.add(record)
    await session.flush()
    audit_action = (
        "GIFT_CREATED"
        if data.service_type == ServiceType.GIFT and previous is None
        else ("PRICE_CREATED" if previous is None else "PRICE_UPDATED")
    )
    write_audit(
        session,
        actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
        actor_id=str(actor.telegram_id),
        action=audit_action,
        entity_type="MANUAL_PROVIDER_PRICE",
        entity_id=record.id,
        summary=f"{data.display_name} narxi {'yaratildi' if previous is None else 'o‘zgartirildi'}",
        old_values=_price_audit(previous),
        new_values=_price_audit(record),
        reason=record.source_note,
        metadata={
            "service_key": service_key,
            "old_price": _price_audit(previous),
            "new_price": _price_audit(record),
            "source_note": record.source_note,
            "requires_approval": needs_approval,
            "low_profit": preview.low_profit,
        },
    )
    if previous is None:
        # Kept as a hidden compatibility event for existing operational queries.
        # User-facing audit lists only the canonical PRICE_CREATED/GIFT_CREATED event.
        write_audit(
            session,
            actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
            actor_id=str(actor.telegram_id),
            action="MANUAL_PRICE_CREATED",
            entity_type="MANUAL_PROVIDER_PRICE",
            entity_id=record.id,
            metadata={
                "service_key": service_key,
                "old_price": None,
                "new_price": _price_audit(record),
                "source_note": record.source_note,
                "requires_approval": needs_approval,
                "low_profit": preview.low_profit,
            },
        )
    return record


async def approve_manual_price(
    session: AsyncSession,
    *,
    price_id: str,
    actor: PricingActor,
    now: datetime | None = None,
) -> ManualProviderPrice:
    if not actor.is_superadmin:
        raise PricingPermissionError("Only a superadmin can approve a price")
    record = await session.scalar(
        select(ManualProviderPrice).where(ManualProviderPrice.id == price_id).with_for_update()
    )
    if record is None or record.status != ManualPriceStatus.DRAFT:
        raise ManualPriceValidationError("Draft price not found")
    current_time = now or datetime.now(UTC)
    if record.valid_until is not None and record.valid_until <= current_time:
        raise ManualPriceValidationError("Draft price has expired")
    await session.execute(
        update(ManualProviderPrice)
        .where(
            ManualProviderPrice.provider_id == record.provider_id,
            ManualProviderPrice.service_key == record.service_key,
            ManualProviderPrice.active.is_(True),
        )
        .values(active=False, status=ManualPriceStatus.INACTIVE)
    )
    record.active = True
    record.status = ManualPriceStatus.ACTIVE
    record.approved_by_admin_id = actor.telegram_id
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor.telegram_id),
        action="MANUAL_PRICE_APPROVED",
        entity_type="MANUAL_PROVIDER_PRICE",
        entity_id=record.id,
        metadata={"service_key": record.service_key, "version": record.version},
    )
    return record


async def deactivate_manual_price(
    session: AsyncSession, *, price_id: str, actor: PricingActor
) -> ManualProviderPrice:
    if not actor.can_manage_pricing:
        raise PricingPermissionError("MANAGE_PRICING permission is required")
    record = await session.scalar(
        select(ManualProviderPrice).where(ManualProviderPrice.id == price_id).with_for_update()
    )
    if record is None:
        raise ManualPriceValidationError("Price not found")
    record.active = False
    record.status = ManualPriceStatus.INACTIVE
    write_audit(
        session,
        actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
        actor_id=str(actor.telegram_id),
        action="MANUAL_PRICE_DEACTIVATED",
        entity_type="MANUAL_PROVIDER_PRICE",
        entity_id=record.id,
        metadata={"service_key": record.service_key, "version": record.version},
    )
    return record


async def create_price_quote(
    session: AsyncSession,
    *,
    user_id: str,
    service_type: ServiceType,
    quote_ttl_seconds: int,
    quantity: int | None = None,
    premium_months: int | None = None,
    gift_name: str | None = None,
    now: datetime | None = None,
) -> PriceQuote:
    current_time = now or datetime.now(UTC)
    provider = await session.scalar(select(Provider).where(Provider.code == "MYXVEST"))
    if provider is None:
        raise PriceTemporarilyUnavailableError("Provider is unavailable")
    lookup = _lookup_service_key(service_type, premium_months, gift_name)
    manual = await session.scalar(
        select(ManualProviderPrice)
        .where(
            ManualProviderPrice.provider_id == provider.id,
            ManualProviderPrice.service_key == lookup,
            ManualProviderPrice.active.is_(True),
            ManualProviderPrice.status == ManualPriceStatus.ACTIVE,
            ManualProviderPrice.valid_from <= current_time,
            (ManualProviderPrice.valid_until.is_(None))
            | (ManualProviderPrice.valid_until > current_time),
        )
        .order_by(ManualProviderPrice.version.desc())
        .limit(1)
    )
    provider_service = await session.scalar(
        select(ProviderService).where(
            ProviderService.provider_id == provider.id,
            ProviderService.service_type == service_type,
            ProviderService.active.is_(True),
        )
    )
    if manual is not None:
        provider_cost, sale_price = _manual_totals(manual, quantity)
        source = (
            PriceSource.MANUAL_OVERRIDE
            if service_type == ServiceType.STARS
            and provider_service is not None
            and provider_service.provider_price_som is not None
            else PriceSource.MANUAL
        )
        quote = PriceQuote(
            user_id=user_id,
            provider_service_id=provider_service.id if provider_service else None,
            manual_price_id=manual.id,
            price_version=manual.version,
            price_source=source,
            provider_cost_som=provider_cost,
            markup_som=sale_price - provider_cost,
            risk_buffer_som=0,
            sale_price_som=sale_price,
            expected_profit_som=sale_price - provider_cost,
            expires_at=current_time + timedelta(seconds=quote_ttl_seconds),
            created_at=current_time,
        )
        session.add(quote)
        return quote
    if service_type != ServiceType.STARS or provider_service is None:
        raise PriceTemporarilyUnavailableError("No active price for this service")
    if provider_service.provider_price_som is None or quantity is None:
        raise PriceTemporarilyUnavailableError("No confirmed API price")
    _validate_stars_quantity(quantity, provider_service.min_quantity, provider_service.max_quantity)
    rule = await session.scalar(
        select(PricingRule).where(
            PricingRule.service_type == ServiceType.STARS,
            PricingRule.enabled.is_(True),
        )
    )
    if rule is None:
        raise PriceTemporarilyUnavailableError("Stars sale pricing rule is missing")
    calculation = calculate_quote(
        provider_cost_som=provider_service.provider_price_som * quantity,
        fixed_markup_som=rule.fixed_markup_som,
        percentage_markup_bps=rule.percentage_markup_bps,
        minimum_profit_som=rule.minimum_profit_som,
        risk_buffer_som=rule.risk_buffer_som,
        ttl_seconds=quote_ttl_seconds,
        now=current_time,
    )
    quote = PriceQuote(
        user_id=user_id,
        provider_service_id=provider_service.id,
        manual_price_id=None,
        price_version=None,
        price_source=PriceSource.API,
        provider_cost_som=calculation.provider_cost_som,
        markup_som=calculation.markup_som,
        risk_buffer_som=calculation.risk_buffer_som,
        sale_price_som=calculation.sale_price_som,
        expected_profit_som=calculation.sale_price_som - calculation.provider_cost_som,
        expires_at=calculation.expires_at,
        created_at=current_time,
    )
    session.add(quote)
    return quote


async def list_active_manual_prices(
    session: AsyncSession,
    *,
    service_type: ServiceType | None = None,
    now: datetime | None = None,
) -> list[ManualProviderPrice]:
    current_time = now or datetime.now(UTC)
    statement = select(ManualProviderPrice).where(
        ManualProviderPrice.active.is_(True),
        ManualProviderPrice.status == ManualPriceStatus.ACTIVE,
        ManualProviderPrice.valid_from <= current_time,
        (ManualProviderPrice.valid_until.is_(None))
        | (ManualProviderPrice.valid_until > current_time),
    )
    if service_type is not None:
        statement = statement.where(ManualProviderPrice.service_type == service_type)
    return list(
        await session.scalars(
            statement.order_by(
                ManualProviderPrice.service_type,
                ManualProviderPrice.sort_order,
                ManualProviderPrice.display_name,
            )
        )
    )


async def expire_manual_prices(session: AsyncSession, *, now: datetime | None = None) -> int:
    current_time = now or datetime.now(UTC)
    result = await session.execute(
        update(ManualProviderPrice)
        .where(
            ManualProviderPrice.active.is_(True),
            ManualProviderPrice.valid_until.is_not(None),
            ManualProviderPrice.valid_until <= current_time,
        )
        .values(active=False, status=ManualPriceStatus.EXPIRED)
    )
    return result.rowcount


def assert_purchase_enabled(enabled: bool) -> None:
    if not enabled:
        raise PurchaseDisabledError("Xizmat narxlari tayyorlanmoqda. Xarid vaqtincha yopiq.")


async def price_change_count_today(
    session: AsyncSession, *, service_key: str, now: datetime | None = None
) -> int:
    current_time = now or datetime.now(UTC)
    start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ManualProviderPrice)
            .where(
                ManualProviderPrice.service_key == service_key,
                ManualProviderPrice.created_at >= start,
            )
        )
        or 0
    )


async def _next_version(session: AsyncSession, provider_id: str, service_key: str) -> int:
    dialect = session.bind.dialect.name
    values = {"provider_id": provider_id, "service_key": service_key, "current_version": 1}
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        statement = insert(ManualPriceSequence).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["provider_id", "service_key"],
            set_={"current_version": ManualPriceSequence.current_version + 1},
        ).returning(ManualPriceSequence.current_version)
        return int(await session.scalar(statement))
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        statement = insert(ManualPriceSequence).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["provider_id", "service_key"],
            set_={"current_version": ManualPriceSequence.current_version + 1},
        ).returning(ManualPriceSequence.current_version)
        return int(await session.scalar(statement))
    sequence = await session.scalar(
        select(ManualPriceSequence)
        .where(
            ManualPriceSequence.provider_id == provider_id,
            ManualPriceSequence.service_key == service_key,
        )
        .with_for_update()
    )
    if sequence is None:
        session.add(ManualPriceSequence(**values))
        return 1
    sequence.current_version += 1
    return sequence.current_version


def _lookup_service_key(
    service_type: ServiceType, premium_months: int | None, gift_name: str | None
) -> str:
    if service_type == ServiceType.STARS:
        return "MYXVEST:STARS"
    if service_type == ServiceType.PREMIUM:
        if premium_months not in PREMIUM_MONTHS:
            raise ManualPriceValidationError("Premium months must be 3, 6 or 12")
        return f"MYXVEST:PREMIUM:{premium_months}"
    return f"MYXVEST:GIFT:{gift_service_slug(gift_name or '')}"


def _manual_totals(price: ManualProviderPrice, quantity: int | None) -> tuple[int, int]:
    if price.service_type == ServiceType.STARS:
        if quantity is None:
            raise ManualPriceValidationError("Stars quantity is required")
        _validate_stars_quantity(quantity, price.min_quantity, price.max_quantity)
        return price.provider_cost_som * quantity, price.sale_price_som * quantity
    return price.provider_cost_som, price.sale_price_som


def _validate_stars_quantity(quantity: int, minimum: int | None, maximum: int | None) -> None:
    if minimum is None or maximum is None or not minimum <= quantity <= maximum:
        raise ManualPriceValidationError("Stars quantity is outside the active price limits")


def _price_audit(price: ManualProviderPrice | None) -> dict | None:
    if price is None:
        return None
    return {
        "id": price.id,
        "service_key": price.service_key,
        "provider_cost_som": price.provider_cost_som,
        "sale_price_som": price.sale_price_som,
        "version": price.version,
        "status": price.status.value,
        "active": price.active,
        "valid_until": price.valid_until.isoformat() if price.valid_until else None,
    }
