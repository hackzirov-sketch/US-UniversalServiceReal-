"""Add production admin, audit, pricing and runtime sales controls.

Revision ID: 20260718_0009
Revises: 20260718_0008
"""

import sqlalchemy as sa
from alembic import op

revision = "20260718_0009"
down_revision = "20260718_0008"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    user_columns = _columns("users")
    additions = {
        "full_name": sa.Column("full_name", sa.String(length=128), nullable=True),
        "role": sa.Column("role", sa.String(length=32), nullable=False, server_default="USER"),
        "admin_active": sa.Column(
            "admin_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        "admin_added_by_telegram_id": sa.Column(
            "admin_added_by_telegram_id", sa.BigInteger(), nullable=True
        ),
        "admin_added_at": sa.Column("admin_added_at", sa.DateTime(timezone=True), nullable=True),
        "admin_disabled_at": sa.Column(
            "admin_disabled_at", sa.DateTime(timezone=True), nullable=True
        ),
        "last_activity_at": sa.Column(
            "last_activity_at", sa.DateTime(timezone=True), nullable=True
        ),
        "admin_session_version": sa.Column(
            "admin_session_version", sa.Integer(), nullable=False, server_default="1"
        ),
    }
    for name, column in additions.items():
        if name not in user_columns:
            op.add_column("users", column)
    op.execute("UPDATE users SET role='ADMIN', admin_active=true WHERE is_admin=true")
    inspector = sa.inspect(op.get_bind())
    if "ix_users_admin_active" not in {idx["name"] for idx in inspector.get_indexes("users")}:
        op.create_index("ix_users_admin_active", "users", ["admin_active"])

    audit_columns = _columns("audit_logs")
    audit_additions = {
        "actor_username_snapshot": sa.Column(
            "actor_username_snapshot", sa.String(length=32), nullable=True
        ),
        "actor_role": sa.Column("actor_role", sa.String(length=32), nullable=True),
        "human_summary": sa.Column("human_summary", sa.String(length=1000), nullable=True),
        "old_values": sa.Column("old_values", sa.JSON(), nullable=True),
        "new_values": sa.Column("new_values", sa.JSON(), nullable=True),
        "reason": sa.Column("reason", sa.String(length=500), nullable=True),
        "correlation_id": sa.Column("correlation_id", sa.String(length=64), nullable=True),
        "request_id": sa.Column("request_id", sa.String(length=64), nullable=True),
    }
    for name, column in audit_additions.items():
        if name not in audit_columns:
            op.add_column("audit_logs", column)
    inspector = sa.inspect(op.get_bind())
    audit_indexes = {idx["name"] for idx in inspector.get_indexes("audit_logs")}
    if "ix_audit_logs_correlation_id" not in audit_indexes:
        op.create_index("ix_audit_logs_correlation_id", "audit_logs", ["correlation_id"])
    if "ix_audit_logs_request_id" not in audit_indexes:
        op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])

    metadata = sa.MetaData()
    runtime_settings = sa.Table(
        "runtime_settings",
        metadata,
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("bool_value", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="runtime_setting_version_positive"),
    )
    preflight_results = sa.Table(
        "preflight_results",
        metadata,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("requested_by_telegram_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("success", sa.Boolean(), nullable=False, index=True),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Index("ix_preflight_valid", "success", "expires_at"),
    )
    runtime_settings.create(op.get_bind(), checkfirst=True)
    preflight_results.create(op.get_bind(), checkfirst=True)
    op.execute(
        sa.text(
            "INSERT INTO runtime_settings (key, bool_value, version, updated_at) "
            "VALUES ('real_sales_enabled', false, 1, CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO NOTHING"
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "preflight_results" in tables:
        op.drop_table("preflight_results")
    if "runtime_settings" in tables:
        op.drop_table("runtime_settings")

    audit_columns = _columns("audit_logs")
    for name in (
        "request_id",
        "correlation_id",
        "reason",
        "new_values",
        "old_values",
        "human_summary",
        "actor_role",
        "actor_username_snapshot",
    ):
        if name in audit_columns:
            op.drop_column("audit_logs", name)

    user_columns = _columns("users")
    for name in (
        "admin_session_version",
        "last_activity_at",
        "admin_disabled_at",
        "admin_added_at",
        "admin_added_by_telegram_id",
        "admin_active",
        "role",
        "full_name",
    ):
        if name in user_columns:
            op.drop_column("users", name)
