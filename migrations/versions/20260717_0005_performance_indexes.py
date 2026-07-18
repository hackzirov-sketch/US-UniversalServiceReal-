"""Add indexes for provider queues and recurring worker lookups.

Revision ID: 20260717_0005
Revises: 20260717_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20260717_0005"
down_revision = "20260717_0004"
branch_labels = None
depends_on = None


INDEXES = (
    (
        "ix_provider_balance_latest",
        "provider_balance_snapshots",
        ["provider_id", "success", "fetched_at"],
    ),
    (
        "ix_provider_services_active_type",
        "provider_services",
        ["provider_id", "service_type", "active"],
    ),
    (
        "ix_orders_provider_funding_fifo",
        "orders",
        ["provider_id", "internal_status", "priority", "approved_at", "created_at"],
    ),
    (
        "ix_orders_status_submitted",
        "orders",
        ["internal_status", "submitted_at"],
    ),
    (
        "ix_orders_status_updated",
        "orders",
        ["internal_status", "updated_at"],
    ),
    (
        "ix_audit_action_created",
        "audit_logs",
        ["action", "created_at"],
    ),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, columns in INDEXES:
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, _columns in reversed(INDEXES):
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)
