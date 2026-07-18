from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.enums import (
    AdminRole,
    FarmPlotState,
    LedgerType,
    ManualPriceStatus,
    OrderStatus,
    PaymentStatus,
    PriceSource,
    ProviderState,
    ServiceType,
    TransactionType,
)


def uuid_str() -> str:
    return str(uuid.uuid4())


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(32), index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default=AdminRole.USER.value, nullable=False)
    admin_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    admin_added_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admin_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admin_session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    available_balance_som: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_balance_som: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    bonus_balance_som: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    farm_points: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ranking_points: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        CheckConstraint("available_balance_som >= 0", name="available_balance_nonnegative"),
        CheckConstraint("reserved_balance_som >= 0", name="reserved_balance_nonnegative"),
        CheckConstraint("bonus_balance_som >= 0", name="bonus_balance_nonnegative"),
        CheckConstraint("farm_points >= 0", name="farm_points_nonnegative"),
        CheckConstraint("ranking_points >= 0", name="ranking_points_nonnegative"),
        CheckConstraint("admin_session_version > 0", name="admin_session_version_positive"),
        CheckConstraint("role IN ('USER', 'ADMIN', 'SUPERADMIN')", name="user_valid_role"),
    )


class AdminPermission(Base):
    __tablename__ = "admin_permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    permission: Mapped[str] = mapped_column(String(64))
    granted_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (UniqueConstraint("user_id", "permission"),)


class ButtonDesign(Base):
    __tablename__ = "button_designs"

    button_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    button_text: Mapped[str] = mapped_column(String(128), nullable=False)
    button_style: Mapped[str] = mapped_column(String(16), nullable=False, default="default")
    custom_emoji_id: Mapped[str | None] = mapped_column(String(32))
    unicode_emoji_fallback: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "button_style IN ('default', 'primary', 'success', 'danger')",
            name="button_design_valid_style",
        ),
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    payment_card_id: Mapped[str | None] = mapped_column(ForeignKey("payment_cards.id"), index=True)
    amount_som: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_amount_som: Mapped[int | None] = mapped_column(BigInteger)
    card_number_first4_snapshot: Mapped[str | None] = mapped_column(String(4))
    card_number_last4_snapshot: Mapped[str | None] = mapped_column(String(4))
    card_holder_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_file_unique_id: Mapped[str | None] = mapped_column(String(255), index=True)
    receipt_checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    receipt_file_type: Mapped[str | None] = mapped_column(String(32))
    receipt_mime_type: Mapped[str | None] = mapped_column(String(64))
    receipt_file_size: Mapped[int | None] = mapped_column(Integer)
    review_note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(32), default=PaymentStatus.AWAITING_RECEIPT.value, nullable=False, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("amount_som > 0", name="payment_amount_positive"),
        CheckConstraint(
            "approved_amount_som IS NULL OR approved_amount_som > 0",
            name="payment_approved_amount_positive",
        ),
        Index("ix_payments_review_queue", "status", "submitted_at"),
    )


class PaymentCard(TimestampMixin, Base):
    __tablename__ = "payment_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    singleton_key: Mapped[str] = mapped_column(String(16), default="PRIMARY", nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(128))
    card_number_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    card_number_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    card_holder_name: Mapped[str] = mapped_column(String(128), nullable=False)
    min_topup_som: Mapped[int] = mapped_column(BigInteger, default=5_000, nullable=False)
    max_topup_som: Mapped[int] = mapped_column(BigInteger, default=2_000_000, nullable=False)
    instructions: Mapped[str | None] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by_admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("singleton_key", name="uq_payment_cards_singleton_key"),
        CheckConstraint("singleton_key = 'PRIMARY'", name="payment_card_primary_only"),
        CheckConstraint("min_topup_som > 0", name="payment_card_min_positive"),
        CheckConstraint("max_topup_som >= min_topup_som", name="payment_card_valid_amount_range"),
    )


class Provider(TimestampMixin, Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[ProviderState] = mapped_column(
        Enum(ProviderState, native_enum=False), default=ProviderState.AVAILABLE
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_balance_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(255))


class ProviderBalanceSnapshot(Base):
    __tablename__ = "provider_balance_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    balance_som: Mapped[int | None] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_provider_balance_latest", "provider_id", "success", "fetched_at"),)


class ProviderService(TimestampMixin, Base):
    __tablename__ = "provider_services"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    external_service_id: Mapped[str] = mapped_column(String(128))
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType, native_enum=False))
    name: Mapped[str] = mapped_column(String(255))
    provider_price_som: Mapped[int | None] = mapped_column(BigInteger)
    min_quantity: Mapped[int | None] = mapped_column(Integer)
    max_quantity: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("provider_id", "external_service_id"),
        CheckConstraint(
            "provider_price_som IS NULL OR provider_price_som >= 0",
            name="service_price_nonnegative",
        ),
        Index("ix_provider_services_active_type", "provider_id", "service_type", "active"),
    )


class PricingRule(TimestampMixin, Base):
    __tablename__ = "pricing_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    service_type: Mapped[ServiceType] = mapped_column(
        Enum(ServiceType, native_enum=False), unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    fixed_markup_som: Mapped[int] = mapped_column(BigInteger, default=0)
    percentage_markup_bps: Mapped[int] = mapped_column(Integer, default=0)
    minimum_profit_som: Mapped[int] = mapped_column(BigInteger, default=0)
    risk_buffer_som: Mapped[int] = mapped_column(BigInteger, default=0)
    min_quantity: Mapped[int | None] = mapped_column(Integer)
    max_quantity: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("fixed_markup_som >= 0", name="fixed_markup_nonnegative"),
        CheckConstraint("percentage_markup_bps >= 0", name="percentage_markup_nonnegative"),
        CheckConstraint("minimum_profit_som >= 0", name="minimum_profit_nonnegative"),
        CheckConstraint("risk_buffer_som >= 0", name="risk_buffer_nonnegative"),
    )


class ManualPriceSequence(Base):
    __tablename__ = "manual_price_sequences"

    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), primary_key=True)
    service_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ManualProviderPrice(TimestampMixin, Base):
    __tablename__ = "manual_provider_prices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType, native_enum=False))
    service_key: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    provider_cost_som: Mapped[int] = mapped_column(BigInteger)
    sale_price_som: Mapped[int] = mapped_column(BigInteger)
    unit_type: Mapped[str] = mapped_column(String(32))
    min_quantity: Mapped[int | None] = mapped_column(Integer)
    max_quantity: Mapped[int | None] = mapped_column(Integer)
    premium_months: Mapped[int | None] = mapped_column(Integer)
    gift_name: Mapped[str | None] = mapped_column(String(128))
    allow_comment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[ManualPriceStatus] = mapped_column(
        Enum(ManualPriceStatus, native_enum=False), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_note: Mapped[str | None] = mapped_column(String(500))
    created_by_admin_id: Mapped[int] = mapped_column(BigInteger)
    approved_by_admin_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("provider_id", "service_key", "version"),
        CheckConstraint("provider_cost_som > 0", name="manual_provider_cost_positive"),
        CheckConstraint("sale_price_som > 0", name="manual_sale_price_positive"),
        CheckConstraint("sale_price_som >= provider_cost_som", name="manual_sale_not_below_cost"),
        CheckConstraint("sort_order >= 0", name="manual_price_sort_order_nonnegative"),
        CheckConstraint("version > 0", name="manual_price_version_positive"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="manual_price_valid_window",
        ),
        Index(
            "ix_manual_prices_lookup",
            "provider_id",
            "service_type",
            "service_key",
            "active",
            "valid_until",
        ),
    )


class PriceQuote(Base):
    __tablename__ = "price_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider_service_id: Mapped[str | None] = mapped_column(ForeignKey("provider_services.id"))
    manual_price_id: Mapped[str | None] = mapped_column(
        ForeignKey("manual_provider_prices.id"), index=True
    )
    price_version: Mapped[int | None] = mapped_column(Integer)
    price_source: Mapped[PriceSource] = mapped_column(Enum(PriceSource, native_enum=False))
    provider_cost_som: Mapped[int] = mapped_column(BigInteger)
    markup_som: Mapped[int] = mapped_column(BigInteger)
    risk_buffer_som: Mapped[int] = mapped_column(BigInteger)
    sale_price_som: Mapped[int] = mapped_column(BigInteger)
    expected_profit_som: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        CheckConstraint("sale_price_som >= provider_cost_som", name="quote_not_below_cost"),
    )


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    public_order_number: Mapped[str] = mapped_column(String(32), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    service_type: Mapped[ServiceType] = mapped_column(Enum(ServiceType, native_enum=False))
    target_username_original: Mapped[str] = mapped_column(String(64))
    target_username: Mapped[str] = mapped_column(String(32))
    external_service_id: Mapped[str | None] = mapped_column(String(128))
    quantity: Mapped[int | None] = mapped_column(Integer)
    premium_months: Mapped[int | None] = mapped_column(Integer)
    gift_id: Mapped[str | None] = mapped_column(String(128))
    provider_cost_som: Mapped[int] = mapped_column(BigInteger)
    sale_price_som: Mapped[int] = mapped_column(BigInteger)
    reserved_amount_som: Mapped[int] = mapped_column(BigInteger, default=0)
    expected_profit_som: Mapped[int] = mapped_column(BigInteger)
    actual_profit_som: Mapped[int | None] = mapped_column(BigInteger)
    quote_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    internal_status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False), index=True
    )
    provider_status: Mapped[str | None] = mapped_column(String(32))
    provider_order_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    provider_request_attempts: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()
    provider: Mapped[Provider] = relationship()

    __table_args__ = (
        UniqueConstraint("provider_id", "provider_order_id"),
        CheckConstraint("provider_cost_som >= 0", name="order_cost_nonnegative"),
        CheckConstraint("sale_price_som >= provider_cost_som", name="order_not_below_cost"),
        CheckConstraint("reserved_amount_som >= 0", name="reserved_amount_nonnegative"),
        Index("ix_orders_funding_fifo", "internal_status", "priority", "approved_at"),
        Index(
            "ix_orders_provider_funding_fifo",
            "provider_id",
            "internal_status",
            "priority",
            "approved_at",
            "created_at",
        ),
        Index("ix_orders_status_submitted", "internal_status", "submitted_at"),
        Index("ix_orders_status_updated", "internal_status", "updated_at"),
    )


class ProviderTransaction(Base):
    __tablename__ = "provider_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, native_enum=False)
    )
    amount_som: Mapped[int] = mapped_column(BigInteger)
    balance_before_som: Mapped[int | None] = mapped_column(BigInteger)
    balance_after_som: Mapped[int | None] = mapped_column(BigInteger)
    external_reference: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"), index=True)
    type: Mapped[LedgerType] = mapped_column(Enum(LedgerType, native_enum=False))
    amount_som: Mapped[int] = mapped_column(BigInteger)
    balance_before_som: Mapped[int] = mapped_column(BigInteger)
    balance_after_som: Mapped[int] = mapped_column(BigInteger)
    reference: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (UniqueConstraint("payment_id", name="uq_ledger_entries_payment_id"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_username_snapshot: Mapped[str | None] = mapped_column(String(32))
    actor_role: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    human_summary: Mapped[str | None] = mapped_column(String(1000))
    old_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    new_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(500))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    sanitized_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (Index("ix_audit_action_created", "action", "created_at"),)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    bool_value: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (CheckConstraint("version > 0", name="runtime_setting_version_positive"),)


class PreflightResult(Base):
    __tablename__ = "preflight_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    requested_by_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (Index("ix_preflight_valid", "success", "expires_at"),)


class WebSession(Base):
    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    admin_session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class TelegramAuthReplay(Base):
    __tablename__ = "telegram_auth_replays"

    init_data_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class FarmProfile(Base):
    __tablename__ = "farm_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    energy: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    water: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    seeds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    xp: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        CheckConstraint("energy >= 0", name="farm_energy_nonnegative"),
        CheckConstraint("water >= 0", name="farm_water_nonnegative"),
        CheckConstraint("seeds >= 0", name="farm_seeds_nonnegative"),
        CheckConstraint("xp >= 0", name="farm_xp_nonnegative"),
        CheckConstraint("level > 0", name="farm_level_positive"),
    )


class FarmPlot(Base):
    __tablename__ = "farm_plots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[FarmPlotState] = mapped_column(
        Enum(FarmPlotState, native_enum=False), default=FarmPlotState.EMPTY, nullable=False
    )
    crop: Mapped[str | None] = mapped_column(String(32))
    planted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    harvested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "slot", name="uq_farm_plot_user_slot"),
        CheckConstraint("slot >= 0", name="farm_plot_slot_nonnegative"),
        CheckConstraint("version > 0", name="farm_plot_version_positive"),
    )


class FarmReward(Base):
    __tablename__ = "farm_rewards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    plot_id: Mapped[str] = mapped_column(ForeignKey("farm_plots.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (CheckConstraint("amount > 0", name="farm_reward_amount_positive"),)
